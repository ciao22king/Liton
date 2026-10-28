"""Small helpers: paths, hashing, arch names, logging."""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

# ELF e_machine -> friendly architecture name
MACHINE_NAMES = {
    0x03: "i386",
    0x08: "mips",
    0x14: "ppc",
    0x15: "ppc64",
    0x16: "s390x",
    0x28: "arm",
    0x2A: "sh",
    0x32: "ia64",
    0x3E: "x86-64",
    0xB7: "aarch64",
    0xF3: "riscv",
}

# Debian/Ubuntu architecture names, for dependency hints.
DEBIAN_ARCH = {
    "x86-64": "amd64",
    "i386": "i386",
    "aarch64": "arm64",
    "arm": "armhf",
    "riscv": "riscv64",
    "ppc64": "ppc64el",
    "s390x": "s390x",
}


def machine_name(e_machine: int) -> str:
    return MACHINE_NAMES.get(e_machine, f"machine-0x{e_machine:x}")


def debian_arch(arch: str) -> str:
    return DEBIAN_ARCH.get(arch, arch or "unknown")


def native_arch() -> str:
    import platform

    m = platform.machine().lower()
    return {
        "x86_64": "x86-64",
        "amd64": "x86-64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
        "i686": "i386",
        "i386": "i386",
        "armv7l": "arm",
        "riscv64": "riscv",
    }.get(m, m)


def sha256_file(path: str | os.PathLike[str], chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip()).strip("-").lower()
    return slug or "app"


def data_home() -> Path:
    """Root of the persistent Liton store (the 'memory')."""
    env = os.environ.get("LITON_HOME")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return base / "liton"


def profiles_dir() -> Path:
    return data_home() / "profiles"


def cache_dir() -> Path:
    return data_home() / "cache"


def logs_dir() -> Path:
    return data_home() / "logs"


def ensure_dirs() -> None:
    for d in (data_home(), profiles_dir(), cache_dir(), logs_dir()):
        d.mkdir(parents=True, exist_ok=True)


class Log:
    """Minimal levelled logger writing to stderr."""

    LEVELS = {"quiet": 0, "error": 1, "warn": 2, "info": 3, "debug": 4}

    def __init__(self, level: str = "info", stream=None):
        self.level = self.LEVELS.get(level, 3)
        self.stream = stream or sys.stderr

    def _emit(self, tag: str, msg: str) -> None:
        print(f"[liton] {tag} {msg}", file=self.stream, flush=True)

    def error(self, msg: str) -> None:
        if self.level >= 1:
            self._emit("error:", msg)

    def warn(self, msg: str) -> None:
        if self.level >= 2:
            self._emit("warn: ", msg)

    def info(self, msg: str) -> None:
        if self.level >= 3:
            self._emit("info: ", msg)

    def debug(self, msg: str) -> None:
        if self.level >= 4:
            self._emit("debug:", msg)


def is_executable(path: str | os.PathLike[str]) -> bool:
    try:
        return os.path.isfile(path) and os.access(path, os.X_OK)
    except OSError:
        return False
