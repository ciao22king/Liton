"""Self-update: check for a newer Liton and install it.

Liton is a command you install once and then want to keep current, so it can
update itself.  Two things matter for this to be trustworthy:

* it must know **how** it was installed, because a git checkout updates with
  ``git pull`` while a pip install updates with ``pip install``;
* it must never do network work behind your back.  Automatic updating is
  **opt-in** (``LITON_AUTO_UPDATE=1``) and throttled, and it is only ever
  attempted for cheap, informational commands - never on the ``run`` path,
  where a sudden update would be a surprise.

Every external effect (network fetch, command execution) is an injectable
callable, so the whole module is testable without a network or a pip.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .util import Log, data_home

#: The repository Liton updates from; override with ``LITON_REPO``.
DEFAULT_REPO = "ciao22king/Liton"

#: Environment variable that opts into automatic updating.
AUTO_ENV = "LITON_AUTO_UPDATE"

#: How often an automatic check may run.
AUTO_INTERVAL_HOURS = 24.0

#: Commands safe to run an automatic update before: they report information and
#: change nothing the user asked for, so a new version cannot surprise them.
AUTO_UPDATE_COMMANDS = frozenset({"list", "show", "doctor", "home", "analyze"})

#: How long any single network or install step may take.
NETWORK_TIMEOUT = 10.0
INSTALL_TIMEOUT = 300.0


def repo_slug() -> str:
    return os.environ.get("LITON_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

def _split_version(text: str) -> tuple[str, str]:
    """Split a version into its numeric core and any suffix after it."""
    s = (text or "").strip().lstrip("vV")
    core = ""
    for ch in s:
        if ch.isdigit() or ch == ".":
            core += ch
        else:
            break
    return core, s[len(core):]


def parse_version(text: str) -> tuple[int, ...]:
    """The numeric components of a version string.

    ``v0.2.0``, ``0.2.0`` and ``0.2.0-rc1`` all yield ``(0, 2, 0)``; the
    prerelease suffix is handled separately by :func:`is_prerelease`.
    """
    core, _ = _split_version(text)
    parts = tuple(int(p) for p in core.split(".") if p.isdigit())
    return parts or (0,)


def is_prerelease(text: str) -> bool:
    """True for versions carrying a prerelease or build suffix (``0.3.0-rc1``)."""
    _, rest = _split_version(text)
    return bool(rest) and rest[0] in "-+"


def is_newer(candidate: str, current: str) -> bool:
    """Is *candidate* a newer version than *current*?"""
    a, b = parse_version(candidate), parse_version(current)
    width = max(len(a), len(b))
    a = a + (0,) * (width - len(a))
    b = b + (0,) * (width - len(b))
    if a != b:
        return a > b
    # Same numbers: a final release outranks a prerelease of itself.
    return is_prerelease(current) and not is_prerelease(candidate)


# ---------------------------------------------------------------------------
# How was this copy installed?
# ---------------------------------------------------------------------------

@dataclass
class InstallInfo:
    kind: str = "unknown"    # git | pip | unknown
    root: str = ""           # git checkout root
    package_dir: str = ""
    spec: str = ""           # pip requirement to upgrade to

    def describe(self) -> str:
        if self.kind == "git":
            return f"git checkout at {self.root}"
        if self.kind == "pip":
            return "pip install"
        return "unknown installation"


def _find_git_root(start: Path, levels: int = 6) -> str:
    """Walk up from *start* looking for a ``.git`` directory."""
    current = start.resolve()
    for _ in range(levels):
        if (current / ".git").exists():
            return str(current)
        if current.parent == current:
            break
        current = current.parent
    return ""


def detect_install() -> InstallInfo:
    """Work out whether this Liton is a git checkout or a pip install."""
    package_dir = Path(__file__).resolve().parent
    root = _find_git_root(package_dir)
    if root:
        return InstallInfo(kind="git", root=root, package_dir=str(package_dir))
    return InstallInfo(
        kind="pip",
        package_dir=str(package_dir),
        spec=f"git+https://github.com/{repo_slug()}.git",
    )


# ---------------------------------------------------------------------------
# Finding the latest version
# ---------------------------------------------------------------------------

@dataclass
class UpdateInfo:
    current: str = ""
    latest: str = ""
    available: bool = False
    source: str = ""          # "tags" | "releases" | ""
    install: InstallInfo = field(default_factory=InstallInfo)
    url: str = ""
    notes: list[str] = field(default_factory=list)
    applied: bool = False
    error: str = ""

    def describe(self) -> str:
        if self.error:
            return f"update check failed: {self.error}"
        if not self.available:
            return f"up to date (version {self.current})"
        return (f"version {self.latest} is available "
                f"(you have {self.current}; {self.install.describe()})")


def _default_run(cmd: Sequence[str], cwd: str | None = None,
                 timeout: float = NETWORK_TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(list(cmd), cwd=cwd, capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _git_tags(run: Callable) -> list[str]:
    """Tag names straight from the remote - no API, no rate limit."""
    url = f"https://github.com/{repo_slug()}.git"
    rc, out, _ = run(["git", "ls-remote", "--tags", "--refs", url])
    if rc != 0:
        return []
    names: list[str] = []
    for line in out.splitlines():
        parts = line.split("refs/tags/")
        if len(parts) == 2 and parts[1].strip():
            names.append(parts[1].strip())
    return names


def _api_tags() -> list[str]:
    """Fallback: the GitHub tags API (unauthenticated, may rate-limit)."""
    url = f"https://api.github.com/repos/{repo_slug()}/tags"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "liton-self-update",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    if not isinstance(payload, list):
        return []
    return [str(item.get("name", "")) for item in payload if isinstance(item, dict)]


def fetch_versions(tag_fetcher: Callable[[], list[str]] | None = None,
                   api_fetcher: Callable[[], list[str]] | None = None) -> tuple[list[str], str]:
    """All known versions, preferring the remote's tags over the API."""
    if tag_fetcher is None:
        tag_fetcher = lambda: _git_tags(_default_run)  # noqa: E731
    if api_fetcher is None:
        api_fetcher = _api_tags
    try:
        names = tag_fetcher()
        if names:
            return names, "tags"
    except Exception:
        pass
    try:
        names = api_fetcher()
        if names:
            return names, "releases"
    except Exception:
        pass
    return [], ""


def latest_version(versions: Sequence[str]) -> str:
    """The highest version among *versions*, ignoring unparseable tags."""
    best = ""
    for name in versions:
        if not name or not parse_version(name) or parse_version(name) == (0,):
            continue
        if not best or is_newer(name, best):
            best = name
    return best


def check_update(current: str, install: InstallInfo | None = None,
                 tag_fetcher: Callable[[], list[str]] | None = None,
                 api_fetcher: Callable[[], list[str]] | None = None) -> UpdateInfo:
    """Compare *current* against the newest published version."""
    install = install or detect_install()
    info = UpdateInfo(current=current, install=install)
    versions, source = fetch_versions(tag_fetcher, api_fetcher)
    info.source = source
    if not versions:
        info.error = ("could not reach the update source "
                      "(no tags and no API response)")
        return info
    latest = latest_version(versions)
    if not latest:
        info.error = "no version tags found"
        return info
    info.latest = latest
    info.available = is_newer(latest, current)
    info.url = f"https://github.com/{repo_slug()}/releases/tag/{latest}"
    if not info.available:
        return info
    if install.kind == "git" and not install.root:
        info.notes.append("git checkout not located; falling back to pip")
        install.kind = "pip"
        install.spec = f"git+https://github.com/{repo_slug()}.git@{latest}"
    elif install.kind == "pip":
        install.spec = f"git+https://github.com/{repo_slug()}.git@{latest}"
    return info


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------

def _pip_commands(spec: str) -> list[list[str]]:
    """Ways to run pip, from least to most invasive."""
    base = [os.environ.get("PYTHON", "python3"), "-m", "pip", "install", "--upgrade"]
    return [
        base + [spec],
        base + ["--user", spec],
        base + ["--break-system-packages", spec],
    ]


def apply_update(info: UpdateInfo, run: Callable | None = None,
                 log: Log | None = None) -> UpdateInfo:
    """Install the newer version, then report what happened."""
    log = log or Log("info")
    run = run or _default_run
    if not info.available:
        return info

    install = info.install
    if install.kind == "git" and install.root:
        rc, out, err = run(["git", "-C", install.root, "pull", "--ff-only"],
                           cwd=install.root, timeout=INSTALL_TIMEOUT)
        if rc == 0:
            info.applied = True
            return info
        info.notes.append(f"git pull failed: {(err or out).strip().splitlines()[-1] if (err or out).strip() else 'unknown error'}")
        install.kind = "pip"
        install.spec = f"git+https://github.com/{repo_slug()}.git@{info.latest}"

    if install.kind == "pip":
        spec = install.spec or f"git+https://github.com/{repo_slug()}.git@{info.latest}"
        for cmd in _pip_commands(spec):
            rc, out, err = run(cmd, timeout=INSTALL_TIMEOUT)
            if rc == 0:
                info.applied = True
                return info
            text = (err or out).strip()
            last = text.splitlines()[-1] if text else f"exit {rc}"
            info.notes.append(f"{' '.join(cmd[-2:])} failed: {last}")
        info.error = "every install method failed; update by hand"
        return info

    info.error = "do not know how this copy was installed; update by hand"
    return info


# ---------------------------------------------------------------------------
# Opt-in automatic updating
# ---------------------------------------------------------------------------

def auto_enabled() -> bool:
    return os.environ.get(AUTO_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _stamp_path() -> Path:
    return data_home() / "update-check.json"


def _last_check() -> float:
    try:
        data = json.loads(_stamp_path().read_text())
        return float(data.get("checked", 0))
    except (OSError, ValueError, TypeError):
        return 0.0


def _record_check(when: float | None = None) -> None:
    try:
        path = _stamp_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked": when if when is not None else time.time()}))
    except OSError:
        pass


def maybe_auto_update(current: str, interval_hours: float = AUTO_INTERVAL_HOURS,
                      now: float | None = None, install: InstallInfo | None = None,
                      tag_fetcher: Callable[[], list[str]] | None = None,
                      api_fetcher: Callable[[], list[str]] | None = None,
                      run: Callable | None = None, log: Log | None = None) -> UpdateInfo | None:
    """Update automatically, if the user opted in and enough time has passed.

    Returns ``None`` when nothing was attempted.  The check is stamped *before*
    the network work, so a failing or slow source cannot cause a check on every
    single invocation.
    """
    log = log or Log("warn")
    if not auto_enabled():
        return None
    now = time.time() if now is None else now
    if now - _last_check() < interval_hours * 3600.0:
        return None
    _record_check(now)

    info = check_update(current, install=install, tag_fetcher=tag_fetcher,
                        api_fetcher=api_fetcher)
    if info.error:
        log.debug(f"auto-update check: {info.error}")
        return info
    if not info.available:
        log.debug(f"auto-update: already current ({current})")
        return info
    log.info(f"auto-update: {info.describe()}")
    return apply_update(info, run=run, log=log)
