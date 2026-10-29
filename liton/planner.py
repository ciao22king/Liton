"""Planner: turn an :class:`Analysis` into a reusable :class:`StrategyPlan`.

The rule engine is the primary path and always works offline.  An optional
LLM planner (any OpenAI-compatible endpoint) can refine the plan for cases the
rules do not recognise; it is strictly a bonus and any failure falls back to the
rules, so Liton never needs a network or a key to function.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .analysis import HostLibraryIndex
from .foreign import Translator, choose
from .models import Analysis, Step, StrategyPlan
from .util import Log, native_arch

# Capability groups that imply a runtime environment tweak.
GROUP_ENV: dict[str, dict[str, str]] = {
    "audio-pulse": {"PULSE_SERVER": "unix:${XDG_RUNTIME_DIR}/pulse/native"},
    "wayland": {"QT_QPA_PLATFORM": "wayland;xcb", "GDK_BACKEND": "wayland,x11"},
    "X11": {"GDK_BACKEND": "x11"},
}

# Groups that need host sockets / devices mounted when running in a container.
GROUP_CONTAINER_MOUNTS: dict[str, list[str]] = {
    "X11": ["/tmp/.X11-unix", "/tmp/.Xauthority"],
    "wayland": ["/run/user/1000/wayland-0"],
    "audio-pulse": ["/run/user/1000/pulse"],
    "audio-alsa": ["/dev/snd"],
    "dbus": ["/run/dbus/system_bus_socket"],
    "Vulkan": ["/dev/dri"],
    "OpenGL": ["/dev/dri"],
}


# glibc version each base image ships, so a symbol-version conflict can pick a
# base image new enough instead of guessing "latest".
GLIBC_BASE_IMAGES: list[tuple[tuple[int, ...], str]] = [
    ((2, 36), "debian:bookworm-slim"),   # glibc 2.36
    ((2, 39), "ubuntu:24.04"),           # glibc 2.39
    ((2, 41), "debian:trixie-slim"),     # glibc 2.41
]


def plan_rules(a: Analysis, log: Log | None = None) -> StrategyPlan:
    log = log or Log("warn")
    p = StrategyPlan(source="rules")

    if a.format == "pe":
        p.strategy = "container"
        p.runtime = "docker"
        p.steps.append(Step("use_runtime", "docker", {"image": "scottyhardy/docker-wine"},
                            reason="Windows binary: needs a Wine-capable runtime"))
        p.steps.append(Step("exec", os.path.basename(a.path), {"argv": ["wine", a.path]},
                            reason="run the PE through Wine"))
        p.notes.append("PE input: Liton treats it like Wine rather than recompiling it")
        p.confidence = 0.6
        return p

    if a.format in ("tar", "archive", "zip"):
        p.strategy = "unpack"
        p.steps.append(Step("unpack", a.path, {"tool": _unpack_tool(a.format)},
                            reason=f"{a.format} payload must be extracted before analysis"))
        p.notes.append("re-run `liton translate` on the unpacked payload")
        p.confidence = 0.4
        return p

    if a.format == "script":
        p.strategy = "native"
        p.steps.append(Step("exec", os.path.basename(a.path), {"argv": [a.path]},
                            reason="interpreted script runs directly"))
        p.confidence = 0.9
        return p

    if a.format == "directory":
        p.strategy = "native"
        p.steps.append(Step("exec", "AppRun", {"argv": [os.path.join(a.path, "AppRun")]},
                            reason="directory bundle: prefer its AppRun entrypoint"))
        p.confidence = 0.5
        return p

    if a.format not in ("elf", "appimage"):
        p.strategy = "native"
        p.steps.append(Step("exec", os.path.basename(a.path), {"argv": [a.path]},
                            reason="unknown format: attempt a direct exec"))
        p.confidence = 0.2
        return p

    # -- ELF / AppImage ----------------------------------------------------
    target = a.path
    if a.format == "appimage":
        p.strategy = "unpack"
        p.steps.append(Step("unpack", a.path,
                            {"tool": "appimage-extract", "source": a.path},
                            reason="AppImage: extract the squashfs root so the payload is a normal tree"))
        target = "${LITON_ROOTFS}/AppRun"
        p.notes.append("the AppImage rootfs is cached in the profile and reused on every run")
        p.confidence = 0.7

    # Foreign architecture: this is real translation, not a container fallback.
    if a.elf and a.elf.machine and a.elf.machine != native_arch():
        tr = choose(a.elf.machine, log=log)
        if tr:
            p.strategy = "translate"
            p.translator = tr.to_dict()
            p.steps.append(Step("translate", tr.describe(),
                                {"translator": tr.to_dict()},
                                reason=f"{a.elf.machine} code runs on {native_arch()} "
                                       f"through {tr.name}"))
            p.notes.append(f"translation backend: {tr.describe()}")
            for note in tr.notes:
                p.notes.append(note)
            p.confidence = max(p.confidence, 0.7)
        else:
            p.strategy = "container"
            p.runtime = "docker"
            p.steps.append(Step("use_runtime", "docker",
                                {"image": f"multiarch/qemu-user-static:latest"},
                                reason=f"no translator installed for {a.elf.machine}; "
                                       f"a container with binfmt is the remaining option"))
            p.notes.append(
                f"no translation backend for {a.elf.machine} on {native_arch()}; "
                f"install FEX, box64 or qemu-user-static for real translation")
            p.confidence = 0.4

    # Missing libraries -> install them, then expose them.
    if a.missing:
        pkgs = sorted(set(a.package_hints.values()))
        p.packages = pkgs
        p.steps.append(Step("install_packages", ", ".join(pkgs) or "(manual)",
                            {"packages": pkgs, "missing": a.missing},
                            reason=f"{len(a.missing)} NEEDED libraries are absent on this host"))
        p.notes.append("missing: " + ", ".join(a.missing))
        p.confidence = max(p.confidence, 0.55)
    else:
        p.confidence = max(p.confidence, 0.85)

    # Present-but-too-old libraries cannot be fixed by installing packages, so
    # the only reliable answer is a newer base image in a container.
    if a.version_conflicts:
        p.strategy = "container"
        p.runtime = "docker"
        base = _suggest_base_image(a)
        p.steps.insert(0, Step("use_runtime", "docker", {"image": base},
                               reason="host libraries are too old for this build; "
                                      "a newer base image provides the required symbol versions"))
        p.notes.append(
            "symbol-version conflicts: " + ", ".join(
                f"{c['library']} needs {c['required']}" for c in a.version_conflicts)
        )
        p.confidence = min(p.confidence, 0.6)

    # Private libraries shipped next to the binary (common for games / AppImages).
    local_libdirs = _local_lib_dirs(a.path)
    if local_libdirs:
        p.ld_library_path.extend(local_libdirs)
        p.steps.append(Step("ld_library_path", ":".join(local_libdirs),
                            {"dirs": local_libdirs},
                            reason="the bundle ships its own shared libraries"))

    # Bundled loader: use the app's own interpreter so it sees its own libc.
    if a.elf and a.elf.interpreter and _ships_own_loader(a.path, a.elf.interpreter):
        p.steps.append(Step("use_loader", a.elf.interpreter,
                            {"loader": a.elf.interpreter, "target": target},
                            reason="the bundle ships its own dynamic loader"))
        if p.runtime == "host":
            p.strategy = "shim"
            p.notes.append("launched through the loader the application ships with")

    if a.elf and a.elf.has_exec_stack:
        p.notes.append("GNU_STACK is executable (hardened kernels may refuse to run it)")

    # Capability-driven environment.
    for group, env in GROUP_ENV.items():
        if a.imports and group in a.imports.groups:
            p.env.update(env)

    if p.runtime == "host":
        p.steps.append(Step("exec", os.path.basename(target), {"argv": [target]},
                            reason="launch the translated application"))

    p.confidence = round(min(1.0, p.confidence), 2)
    return p


def _suggest_base_image(a: Analysis) -> str:
    """Pick the smallest distro image whose glibc satisfies the build."""
    required: tuple[int, ...] = ()
    for conflict in a.version_conflicts:
        if conflict.get("library", "").startswith("libc.so"):
            required = max(required, _version_tuple(conflict.get("required", "")))
    if not required:
        return "debian:stable-slim"
    for version, image in GLIBC_BASE_IMAGES:
        if required <= version:
            return image
    return "debian:stable-slim"


def _version_tuple(version: str) -> tuple[int, ...]:
    from .analysis import _version_key

    return _version_key(version)


def _local_lib_dirs(path: str) -> list[str]:
    base = os.path.dirname(path)
    out = []
    for name in ("lib", "lib64", "usr/lib", "usr/lib64", "libs"):
        d = os.path.join(base, name)
        if os.path.isdir(d):
            out.append(d)
    return out


def _ships_own_loader(path: str, interpreter: str) -> bool:
    base = os.path.dirname(path)
    candidate = os.path.join(base, interpreter.lstrip("/"))
    return os.path.exists(candidate)


def _unpack_tool(fmt: str) -> str:
    return {"tar": "tar", "archive": "tar", "zip": "unzip"}.get(fmt, "tar")


def container_run_flags(a: Analysis, p: StrategyPlan) -> list[str]:
    """Docker/podman flags to expose the host devices a GUI/media app needs."""
    flags: list[str] = ["--rm", "-i", "-t"]
    if os.environ.get("DISPLAY"):
        flags += ["-e", "DISPLAY", "-v", "/tmp/.X11-unix:/tmp/.X11-unix"]
    if os.path.exists("/dev/dri"):
        flags += ["--device", "/dev/dri"]
    if os.path.exists("/dev/snd"):
        flags += ["--device", "/dev/snd"]
    seen: set[str] = set()
    for group in (a.imports.groups if a.imports else {}):
        for mount in GROUP_CONTAINER_MOUNTS.get(group, []):
            if mount in seen or not os.path.exists(mount):
                continue
            seen.add(mount)
            flags += ["-v", f"{mount}:{mount}"]
    for k, v in p.env.items():
        flags += ["-e", f"{k}={v}"]
    return flags


# ---------------------------------------------------------------------------
# Optional LLM planner
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """You are Liton's translation planner. Given a static analysis of a
Linux application, produce a JSON recipe that makes it run on this host and can be
replayed without re-planning.

Return ONLY a JSON object with these keys:
  "strategy": one of "native", "shim", "container", "unpack"
  "runtime": "host", "docker" or "podman"
  "packages": array of Debian package names to install
  "ld_library_path": array of directories to prepend to LD_LIBRARY_PATH
  "preload": array of libraries to LD_PRELOAD
  "env": object of environment variables
  "steps": array of objects {"kind": str, "detail": str, "params": object, "reason": str}
           where kind is one of: install_packages, unpack, set_env, ld_library_path,
           preload, use_loader, use_runtime, exec
  "confidence": number between 0 and 1
  "notes": array of strings

Analysis:
<<ANALYSIS>>

Host architecture: <<ARCH>>
Return JSON only, no prose."""


def plan_llm(a: Analysis, log: Log | None = None, client: Any | None = None) -> StrategyPlan:
    """Ask an OpenAI-compatible model to plan; raise on any problem."""
    log = log or Log("warn")
    if client is None:
        from .llm import LLMClient

        client = LLMClient.from_env(log=log)
    prompt = PROMPT_TEMPLATE.replace("<<ANALYSIS>>", json.dumps(_analysis_digest(a), indent=2)) \
                           .replace("<<ARCH>>", native_arch())
    text = client.complete(prompt)
    data = _extract_json(text)
    return plan_from_dict(data)


def _analysis_digest(a: Analysis) -> dict[str, Any]:
    return {
        "path": a.path,
        "format": a.format,
        "arch": a.arch,
        "size": a.size,
        "needed": a.elf.needed if a.elf else [],
        "missing": a.missing,
        "interpreter": a.elf.interpreter if a.elf else "",
        "rpath": (a.elf.rpath + a.elf.runpath) if a.elf else [],
        "capability_groups": a.imports.summary() if a.imports else [],
        "package_hints": a.package_hints,
        "notes": a.notes,
    }


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start:end + 1])


VALID_KINDS = {"install_packages", "unpack", "set_env", "ld_library_path",
               "preload", "use_loader", "use_runtime", "exec", "translate"}
VALID_STRATEGIES = {"native", "shim", "container", "unpack", "translate"}


def plan_from_dict(data: dict[str, Any]) -> StrategyPlan:
    """Validate and coerce an untrusted plan mapping into a :class:`StrategyPlan`."""
    p = StrategyPlan(source="llm")
    strategy = str(data.get("strategy", "native"))
    p.strategy = strategy if strategy in VALID_STRATEGIES else "native"
    runtime = str(data.get("runtime", "host"))
    p.runtime = runtime if runtime in ("host", "docker", "podman") else "host"
    p.packages = [str(x) for x in data.get("packages", []) if isinstance(x, (str, int))]
    p.ld_library_path = [str(x) for x in data.get("ld_library_path", [])]
    p.preload = [str(x) for x in data.get("preload", [])]
    env = data.get("env", {})
    if isinstance(env, dict):
        p.env = {str(k): str(v) for k, v in env.items()}
    for raw in data.get("steps", []):
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", ""))
        if kind not in VALID_KINDS:
            continue
        params = raw.get("params", {})
        p.steps.append(Step(kind, str(raw.get("detail", "")),
                            params if isinstance(params, dict) else {},
                            str(raw.get("reason", ""))))
    try:
        p.confidence = float(data.get("confidence", 0.5))
    except (TypeError, ValueError):
        p.confidence = 0.5
    p.confidence = max(0.0, min(1.0, p.confidence))
    p.notes = [str(n) for n in data.get("notes", [])]
    return p


def make_plan(a: Analysis, use_llm: bool = False, log: Log | None = None,
              client: Any | None = None) -> StrategyPlan:
    """Plan *a*, using rules and optionally refining with an LLM."""
    log = log or Log("warn")
    base = plan_rules(a, log=log)
    if not use_llm:
        return base
    try:
        refined = plan_llm(a, log=log, client=client)
        if refined.steps:
            log.info(f"LLM planner produced a {refined.strategy} plan "
                     f"(confidence {refined.confidence:.2f})")
            # Keep any rule-discovered packages the model omitted.
            for pkg in base.packages:
                if pkg not in refined.packages:
                    refined.packages.append(pkg)
            for lib in base.ld_library_path:
                if lib not in refined.ld_library_path:
                    refined.ld_library_path.append(lib)
            return refined
        log.warn("LLM planner returned an empty plan; using rules")
    except Exception as exc:  # never fatal: rules always stand
        log.warn(f"LLM planner unavailable ({exc}); using rules")
    return base
