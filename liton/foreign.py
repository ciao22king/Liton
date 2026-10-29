"""Foreign-architecture translation backends.

This is where Liton stops merely *describing* how to run an application and
actually makes another architecture's code execute on this machine.

Four backends are supported, in order of preference for each guest
architecture:

* **FEX** - x86-64 and i386 on aarch64.  The fast choice on ARM; it translates
  the guest instruction stream and keeps a code cache, so the cost is paid once.
* **box64 / box86** - x86-64 on aarch64/riscv64 and i386 on arm.  The usual
  choice on low-end ARM devices and the Raspberry Pi.
* **QEMU user-mode** - any guest on any host.  The universal fallback; slower,
  but it always works and needs only the target sysroot.

Choosing a translator is a decision worth remembering, so the chosen backend and
its environment are recorded in the profile.  The environment deliberately pins
every cache into the profile directory: FEX's JIT code cache in particular is
what turns "translate it again" into "translate it once".
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Callable

from .util import Log, native_arch

# Guest architecture -> QEMU user-mode binary basename (without the host prefix).
QEMU_GUEST: dict[str, str] = {
    "x86-64": "qemu-x86_64",
    "i386": "qemu-i386",
    "aarch64": "qemu-aarch64",
    "arm": "qemu-arm",
    "riscv": "qemu-riscv64",
    "ppc64": "qemu-ppc64le",
    "ppc": "qemu-ppc",
    "s390x": "qemu-s390x",
    "mips": "qemu-mips",
    "ia64": "qemu-ia64",
}

# Guest architecture -> GNU triplet, used to locate a cross sysroot.
ARCH_TRIPLET: dict[str, str] = {
    "x86-64": "x86_64-linux-gnu",
    "i386": "i386-linux-gnu",
    "aarch64": "aarch64-linux-gnu",
    "arm": "arm-linux-gnueabihf",
    "riscv": "riscv64-linux-gnu",
    "ppc64": "powerpc64le-linux-gnu",
    "s390x": "s390x-linux-gnu",
}

FEX_GUESTS = ("x86-64", "i386")
BOX64_GUESTS = ("x86-64",)
BOX86_GUESTS = ("i386",)

# FEX accepts a config file named after the program, and reads these
# environment overrides (see FEXCore Config.cpp and Source/Common/Config.cpp).
FEX_BINARIES = ("FEXInterpreter", "FEX", "FEX-x86_64", "FEX-x86")

# Where a QEMU cross sysroot normally lives, keyed by triplet.
SYSROOT_ROOTS = ("/usr", "/usr/local", "/opt")


@dataclass
class Translator:
    """A concrete, remembered decision about how to translate an application."""

    name: str = ""            # "fex", "box64", "qemu"
    kind: str = ""            # fex | box64 | box86 | qemu
    path: str = ""            # resolved executable
    host_arch: str = ""
    guest_arch: str = ""
    argv_prefix: list[str] = field(default_factory=list)
    sysroot: str = ""
    env: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        base = f"{self.name} ({self.kind}): {self.guest_arch} on {self.host_arch}"
        return f"{base} via {self.path}" if self.path else base

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": self.path,
            "host_arch": self.host_arch,
            "guest_arch": self.guest_arch,
            "argv_prefix": list(self.argv_prefix),
            "sysroot": self.sysroot,
            "env": dict(self.env),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Translator":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in (data or {}).items() if k in known})


def qemu_names(guest_arch: str) -> list[str]:
    """Candidate QEMU binaries for *guest_arch*, static builds first."""
    base = QEMU_GUEST.get(guest_arch)
    if not base:
        return []
    return [f"{base}-static", base]


def find_sysroot(guest_arch: str, roots: tuple[str, ...] = SYSROOT_ROOTS) -> str:
    """Locate a cross sysroot providing the guest architecture's libc.

    QEMU needs this: it is the guest's ``/``, so its dynamic loader and
    libraries are found without a container.  An explicit ``LITON_SYSROOT``
    wins over the standard locations.
    """
    explicit = os.environ.get("LITON_SYSROOT")
    if explicit and os.path.isdir(explicit):
        return explicit
    triplet = ARCH_TRIPLET.get(guest_arch)
    if not triplet:
        return ""
    for root in roots:
        for candidate in (os.path.join(root, triplet), os.path.join(root, "lib", triplet)):
            if os.path.isdir(os.path.join(candidate, "lib")):
                return candidate
    return ""


def candidate_order(guest_arch: str, host_arch: str) -> list[str]:
    """Which backend kinds are worth trying, best first."""
    order: list[str] = []
    if guest_arch == host_arch:
        return order
    if host_arch == "aarch64" and guest_arch in FEX_GUESTS:
        order.append("fex")
    if guest_arch in BOX64_GUESTS and host_arch in ("aarch64", "riscv"):
        order.append("box64")
    if guest_arch in BOX86_GUESTS and host_arch in ("arm", "aarch64"):
        order.append("box86")
    if guest_arch in QEMU_GUEST:
        order.append("qemu")
    return order


def choose(guest_arch: str, host_arch: str | None = None,
           probe: Callable[[str], str | None] | None = None,
           log: Log | None = None) -> Translator | None:
    """Pick the best available translator, or ``None`` when none can work.

    ``probe`` resolves a binary name to a path; it is injectable so the choice
    logic can be tested without the translators installed.
    """
    log = log or Log("warn")
    host_arch = host_arch or native_arch()
    if not guest_arch or guest_arch == host_arch:
        return None
    find = probe or shutil.which

    for kind in candidate_order(guest_arch, host_arch):
        if kind == "fex":
            found = _first(find, FEX_BINARIES)
            if found:
                t = Translator(name=os.path.basename(found), kind="fex", path=found,
                               host_arch=host_arch, guest_arch=guest_arch)
                rootfs = find_fex_rootfs()
                if rootfs:
                    t.sysroot = rootfs
                    t.notes.append(f"x86-64 rootfs: {rootfs}")
                else:
                    t.notes.append("no FEX rootfs found; the guest libc will be missing "
                                   "until one is prepared (liton prepare)")
                return t
        elif kind == "box64":
            found = _first(find, ("box64",))
            if found:
                return Translator(name="box64", kind="box64", path=found,
                                  host_arch=host_arch, guest_arch=guest_arch,
                                  env={"BOX64_LD_LIBRARY_PATH": ""})
        elif kind == "box86":
            found = _first(find, ("box86",))
            if found:
                return Translator(name="box86", kind="box86", path=found,
                                  host_arch=host_arch, guest_arch=guest_arch,
                                  env={"BOX86_LD_LIBRARY_PATH": ""})
        elif kind == "qemu":
            found = _first(find, qemu_names(guest_arch))
            if found:
                sysroot = find_sysroot(guest_arch)
                t = Translator(name=os.path.basename(found), kind="qemu", path=found,
                               host_arch=host_arch, guest_arch=guest_arch)
                if sysroot:
                    t.sysroot = sysroot
                    t.argv_prefix = ["-L", sysroot]
                    t.env["QEMU_LD_PREFIX"] = sysroot
                else:
                    from .util import debian_arch

                    t.notes.append("no cross sysroot found; install libc6-dev-"
                                   f"{debian_arch(guest_arch)}-cross or set LITON_SYSROOT")
                return t

    log.debug(f"no translator available for {guest_arch} on {host_arch}")
    return None


def _first(find: Callable[[str], str | None], names: tuple[str, ...]) -> str:
    for name in names:
        found = find(name)
        if found:
            return found
    return ""


# ---------------------------------------------------------------------------
# FEX specifics
# ---------------------------------------------------------------------------

def fex_data_dirs() -> list[str]:
    """FEX data directories that may already hold a downloaded rootfs."""
    out: list[str] = []
    override = os.environ.get("FEX_APP_DATA_LOCATION")
    if override:
        out.append(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    home = os.path.expanduser("~")
    out.append(os.path.join(xdg, "fex-emu") if xdg else os.path.join(home, ".local", "share", "fex-emu"))
    out.append(os.path.join(home, ".fex-emu"))
    return out


def find_fex_rootfs() -> str:
    """Find an existing x86-64 rootfs that FEX can use.

    An explicit ``LITON_FEX_ROOTFS`` or ``FEX_ROOTFS`` wins; otherwise the usual
    FEX locations are searched for a directory that looks like a guest system
    (it has a libc).
    """
    for var in ("LITON_FEX_ROOTFS", "FEX_ROOTFS"):
        value = os.environ.get(var)
        if value and _looks_like_rootfs(value):
            return value

    for data_dir in fex_data_dirs():
        rootfs_dir = os.path.join(data_dir, "RootFS")
        if not os.path.isdir(rootfs_dir):
            continue
        for entry in sorted(os.listdir(rootfs_dir)):
            candidate = os.path.join(rootfs_dir, entry)
            if _looks_like_rootfs(candidate):
                return candidate
    return ""


def _looks_like_rootfs(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    for rel in ("lib/x86_64-linux-gnu/libc.so.6", "usr/lib/x86_64-linux-gnu/libc.so.6",
                "lib64/ld-linux-x86-64.so.2", "lib/libc.so.6"):
        if os.path.exists(os.path.join(path, rel)):
            return True
    return False


def fex_env(profile_dir: str, rootfs: str = "") -> dict[str, str]:
    """FEX environment with every cache pinned inside the profile.

    Pinning ``FEX_APP_CACHE_LOCATION`` is the point of the whole exercise: FEX's
    translated code cache survives between runs, so the application is JIT-ed on
    first launch and reused afterwards instead of being retranslated each time.
    """
    base = os.path.abspath(profile_dir)
    data = os.path.join(base, "fex-emu")
    cache = os.path.join(base, "cache", "fex-emu")
    env = {
        "FEX_APP_DATA_LOCATION": data + os.sep,
        "FEX_APP_CACHE_LOCATION": cache + os.sep,
    }
    if rootfs:
        env["FEX_ROOTFS"] = rootfs
    return env


def fex_app_config(program: str, rootfs: str = "") -> dict:
    """The per-application FEX config (``AppConfig/<program>.json``)."""
    config: dict[str, str] = {}
    if rootfs:
        config["RootFS"] = rootfs
    return {"Config": config}


# ---------------------------------------------------------------------------
# Turning a translator into a runnable command line
# ---------------------------------------------------------------------------

def translator_env(translator: Translator, profile_dir: str) -> dict[str, str]:
    """Full environment for launching through *translator*, caches pinned."""
    env = dict(translator.env)
    if translator.kind == "fex":
        env.update(fex_env(profile_dir, translator.sysroot))
    return env


def translator_argv(translator: Translator, target: str) -> list[str]:
    """The command that runs *target* through *translator*."""
    return [translator.path, *translator.argv_prefix, target]


def ensure_fex_layout(profile_dir: str, translator: Translator) -> None:
    """Create the FEX data/cache directories a profile will use."""
    for rel in ("fex-emu", os.path.join("fex-emu", "AppConfig"),
                os.path.join("cache", "fex-emu")):
        os.makedirs(os.path.join(profile_dir, rel), exist_ok=True)
