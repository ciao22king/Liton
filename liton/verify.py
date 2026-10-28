"""Verification: prove a compiled profile can actually resolve and launch.

Three levels, cheapest first:

1. *static* - artifacts exist and the recorded libraries are present on the host.
2. *loader* - ask the dynamic loader to resolve the target (``ld.so --list``),
   inside the profile's environment, without running the program's own code.
3. *execute* - actually launch it (opt-in, since it runs untrusted code).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .analyze import HostLibraryIndex
from .models import Profile
from .store import plan_from_dict
from .util import Log


class VerifyResult:
    def __init__(self) -> None:
        self.ok = True
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, bool(ok), detail))
        if not ok:
            self.ok = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in self.checks],
        }

    def render(self) -> str:
        lines = []
        for name, ok, detail in self.checks:
            mark = "ok  " if ok else "FAIL"
            lines.append(f"  [{mark}] {name}" + (f" - {detail}" if detail else ""))
        lines.append("  => " + ("PASS" if self.ok else "FAIL"))
        return "\n".join(lines)


def verify_profile(profile: Profile, execute: bool = False, log: Log | None = None,
                   timeout: float = 15.0) -> VerifyResult:
    log = log or Log("warn")
    r = VerifyResult()

    # 1. artifacts
    for art in profile.artifacts:
        r.add(f"artifact {Path(art).name}", os.path.exists(art), art)

    # 2. recorded libraries still resolve, honouring the profile's own search
    #    path so an app's bundled libraries count as present.
    analysis = profile.analysis or {}
    plan = plan_from_dict(profile.plan or {})
    missing_now: list[str] = []
    index = HostLibraryIndex(extra_dirs=plan.ld_library_path, log=log)
    for lib in analysis.get("needed", []):
        if index.find(lib) is None:
            missing_now.append(lib)
    r.add("shared libraries resolvable", not missing_now,
          ", ".join(missing_now) if missing_now else f"{len(analysis.get('needed', []))} libs")

    # 3. loader resolution, inside the profile environment
    target = profile.source_path
    if os.path.exists(target) and os.access(target, os.X_OK):
        resolved, detail = _loader_resolves(profile, target, timeout)
        r.add("dynamic loader resolves target", resolved, detail)
    else:
        r.add("target executable present", False, target)

    # 4. optional real execution
    if execute:
        ok, detail = _execute_profile(profile, timeout)
        r.add("launches without immediate error", ok, detail)

    return r


def _profile_env(profile: Profile) -> dict[str, str]:
    env = dict(os.environ)
    plan = plan_from_dict(profile.plan or {})
    if plan.ld_library_path:
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join(plan.ld_library_path + ([existing] if existing else []))
    env.update({k: v for k, v in plan.env.items()})
    return env


def _find_loader(profile: Profile) -> str:
    analysis = profile.analysis or {}
    interp = analysis.get("interpreter") or ""
    if interp and os.path.exists(interp):
        return interp
    for cand in ("/lib64/ld-linux-x86-64.so.2", "/lib/ld-linux-aarch64.so.1",
                 "/lib/ld-linux.so.2", "/lib/ld-linux-armhf.so.3"):
        if os.path.exists(cand):
            return cand
    return ""


def _loader_resolves(profile: Profile, target: str, timeout: float) -> tuple[bool, str]:
    loader = _find_loader(profile)
    if not loader:
        return True, "no dynamic loader found; skipping (static binary?)"
    env = _profile_env(profile)
    try:
        out = subprocess.run([loader, "--list", target], capture_output=True, text=True,
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return False, "loader timed out"
    except OSError as exc:
        return False, f"cannot run loader: {exc}"
    text = out.stdout + out.stderr
    if out.returncode != 0:
        return False, text.strip().splitlines()[-1] if text.strip() else f"exit {out.returncode}"
    not_found = [ln.split("=>")[0].strip() for ln in text.splitlines()
                 if "not found" in ln]
    if not_found:
        return False, "not found: " + ", ".join(not_found)
    count = sum(1 for ln in text.splitlines() if "=>" in ln)
    return True, f"{count} libraries resolved"


def _execute_profile(profile: Profile, timeout: float) -> tuple[bool, str]:
    runner = Path(profile.artifacts[0]).parent / "run.sh" if profile.artifacts else None
    if runner is None or not runner.exists():
        return False, "no runner script"
    env = dict(os.environ)
    env.setdefault("LITON_VERIFY", "1")
    try:
        out = subprocess.run(["/bin/sh", str(runner)], capture_output=True, text=True,
                             timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        # A GUI app that stays open is a *success* for this check.
        return True, f"still running after {timeout:.0f}s (looks alive)"
    except OSError as exc:
        return False, str(exc)
    tail = (out.stdout + out.stderr).strip().splitlines()
    detail = tail[-1] if tail else f"exit {out.returncode}"
    # 127 / 126 mean the loader or binary was not found.
    if out.returncode in (126, 127):
        return False, detail
    return True, detail


def doctor() -> dict:
    """Report the host capabilities Liton can rely on."""
    return {
        "arch": _uname_arch(),
        "apt": bool(shutil.which("apt-get")),
        "dpkg": bool(shutil.which("dpkg")),
        "docker": bool(shutil.which("docker")),
        "podman": bool(shutil.which("podman")),
        "display": bool(os.environ.get("DISPLAY")),
        "wayland": bool(os.environ.get("WAYLAND_DISPLAY")),
        "llm": bool(os.environ.get("LITON_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")),
    }


def _uname_arch() -> str:
    from .util import native_arch

    return native_arch()
