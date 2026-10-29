"""Static analysis of an application: format, architecture, dependencies, imports.

Everything here is a pure function of the files on disk plus a read-only scan of
the host library directories.  Nothing is executed, so analysing an untrusted
binary is safe.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import tarfile
from pathlib import Path

from . import elf as elfmod
from .models import Analysis, ElfInfo, ImportSurface, LibResolution
from .util import Log, debian_arch, native_arch, sha256_file

APPIMAGE_MAGIC_OFFSET = 8

# Symbol prefixes -> capability groups.  Used to explain *why* a library is
# needed and to choose environment fixes (e.g. audio needs a socket/pulse).
SYMBOL_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("X11", ("XOpenDisplay", "XCreateWindow", "XMapWindow", "XDefaultScreen", "XNextEvent",
             "XrmGetStringDatabase", "XInternAtom")),
    ("Wayland", ("wl_display_connect", "wl_registry_add_listener", "wl_surface_create")),
    ("OpenGL", ("glXCreateContext", "glXChooseVisual", "eglGetDisplay", "eglInitialize",
                "glCreateShader", "glGenTextures", "glXSwapBuffers")),
    ("Vulkan", ("vkCreateInstance", "vkEnumeratePhysicalDevices", "vkCreateDevice")),
    ("audio-alsa", ("snd_pcm_open", "snd_pcm_writei", "snd_mixer_open", "snd_card_next")),
    ("audio-pulse", ("pa_context_connect", "pa_stream_connect_playback", "pa_mainloop_new")),
    ("audio-pipewire", ("pw_init", "pw_main_loop_new", "pw_stream_new")),
    ("gstreamer", ("gst_init", "gst_element_factory_make", "gst_pipeline_new")),
    ("dbus", ("dbus_bus_get", "dbus_message_new_method_call", "dbus_connection_open")),
    ("gtk", ("gtk_init", "gtk_main", "gtk_window_new", "gtk_widget_show")),
    ("qt", ("_ZN7QWidgetC1EP7QWidget6QFlagsIN2Qt10WindowTypeEE", "_ZN12QApplicationC1ERiPPci",
            "_ZN7QWidget4showEv")),
    ("glib", ("g_malloc", "g_object_new", "g_main_loop_run", "g_signal_connect_data")),
    ("pthreads", ("pthread_create", "pthread_join", "pthread_mutex_lock")),
    ("openssl", ("SSL_new", "OPENSSL_init_ssl", "EVP_EncryptInit_ex", "SSL_CTX_new")),
    ("curl", ("curl_easy_init", "curl_easy_perform", "curl_easy_setopt")),
    ("crypto-nettle", ("nettle_sha256_init", "gnutls_init", "gnutls_handshake")),
    ("ssl-gnutls", ("gnutls_init", "gnutls_certificate_allocate_credentials")),
    ("zlib", ("inflateInit_", "deflateInit_", "compress2", "uncompress")),
    ("fontconfig", ("FcInit", "FcFontMatch", "FcConfigGetCurrent")),
    ("freetype", ("FT_Init_FreeType", "FT_New_Face", "FT_Load_Glyph")),
    ("xcb", ("xcb_connect", "xcb_create_window", "xcb_poll_for_event")),
    ("SDL", ("SDL_Init", "SDL_CreateWindow", "SDL_PollEvent")),
    ("libnotify", ("notify_init", "notify_notification_show")),
    ("udev", ("udev_new", "udev_enumerate_new")),
    ("dlopen", ("dlopen", "dlsym", "dlclose")),
]

# Curated soname -> Debian/Ubuntu package map.  Covers the dependencies that
# actually show up in third-party builds; `dpkg -S` fills in the rest at runtime.
SONAME_PACKAGES = {
    "libc.so.6": "libc6",
    "libm.so.6": "libc6",
    "libdl.so.2": "libc6",
    "libpthread.so.0": "libc6",
    "librt.so.1": "libc6",
    "libutil.so.1": "libc6",
    "libnsl.so.1": "libc6",
    "libresolv.so.2": "libc6",
    "ld-linux-x86-64.so.2": "libc6",
    "ld-linux-aarch64.so.1": "libc6",
    "libstdc++.so.6": "libstdc++6",
    "libgcc_s.so.1": "libgcc-s1",
    "libatomic.so.1": "libatomic1",
    "libgomp.so.1": "libgomp1",
    "libz.so.1": "zlib1g",
    "libbz2.so.1.0": "libbz2-1.0",
    "liblzma.so.5": "liblzma5",
    "libzstd.so.1": "libzstd1",
    "libssl.so.3": "libssl3",
    "libcrypto.so.3": "libssl3",
    "libssl.so.1.1": "libssl1.1",
    "libcrypto.so.1.1": "libssl1.1",
    "libcurl.so.4": "libcurl4",
    "libgnutls.so.30": "libgnutls30",
    "libnettle.so.8": "libnettle8",
    "libhogweed.so.6": "libhogweed6",
    "libX11.so.6": "libx11-6",
    "libXext.so.6": "libxext6",
    "libXrender.so.1": "libxrender1",
    "libXrandr.so.2": "libxrandr2",
    "libXinerama.so.1": "libxinerama1",
    "libXcursor.so.1": "libxcursor1",
    "libXi.so.6": "libxi6",
    "libXtst.so.6": "libxtst6",
    "libXfixes.so.3": "libxfixes3",
    "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libxdamage1",
    "libXxf86vm.so.1": "libxxf86vm1",
    "libxcb.so.1": "libxcb1",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libwayland-client.so.0": "libwayland-client0",
    "libwayland-cursor.so.0": "libwayland-cursor0",
    "libwayland-egl.so.1": "libwayland-egl1",
    "libGL.so.1": "libgl1",
    "libGLX.so.0": "libglx0",
    "libGLdispatch.so.0": "libgldispatch0",
    "libEGL.so.1": "libegl1",
    "libOpenGL.so.0": "libopengl0",
    "libGLU.so.1": "libglu1-mesa",
    "libgbm.so.1": "libgbm1",
    "libvulkan.so.1": "libvulkan1",
    "libasound.so.2": "libasound2",
    "libpulse.so.0": "libpulse0",
    "libpulse-simple.so.0": "libpulse0",
    "libpipewire-0.3.so.0": "libpipewire-0.3-0",
    "libjack.so.0": "libjack-jackd2-0",
    "libdbus-1.so.3": "libdbus-1-3",
    "libsystemd.so.0": "libsystemd0",
    "libudev.so.1": "libudev1",
    "libgtk-3.so.0": "libgtk-3-0",
    "libgdk-3.so.0": "libgtk-3-0",
    "libgdk_pixbuf-2.0.so.0": "libgdk-pixbuf-2.0-0",
    "libcairo.so.2": "libcairo2",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libpangocairo-1.0.so.0": "libpango-1.0-0",
    "libatk-1.0.so.0": "libatk1.0-0",
    "libgio-2.0.so.0": "libglib2.0-0",
    "libgobject-2.0.so.0": "libglib2.0-0",
    "libglib-2.0.so.0": "libglib2.0-0",
    "libgmodule-2.0.so.0": "libglib2.0-0",
    "libQt5Core.so.5": "libqt5core5a",
    "libQt5Gui.so.5": "libqt5gui5",
    "libQt5Widgets.so.5": "libqt5widgets5",
    "libQt6Core.so.6": "libqt6core6",
    "libQt6Gui.so.6": "libqt6gui6",
    "libQt6Widgets.so.6": "libqt6widgets6",
    "libSDL2-2.0.so.0": "libsdl2-2.0-0",
    "libfreetype.so.6": "libfreetype6",
    "libfontconfig.so.1": "libfontconfig1",
    "libexpat.so.1": "libexpat1",
    "libpng16.so.16": "libpng16-16",
    "libjpeg.so.8": "libjpeg-turbo8",
    "libjpeg.so.62": "libjpeg62-turbo",
    "libtiff.so.5": "libtiff5",
    "libtiff.so.6": "libtiff6",
    "libwebp.so.7": "libwebp7",
    "libwebpdemux.so.2": "libwebpdemux2",
    "libharfbuzz.so.0": "libharfbuzz0b",
    "libnspr4.so": "libnspr4",
    "libnss3.so": "libnss3",
    "libsmime3.so": "libnss3",
    "libffi.so.8": "libffi8",
    "libpcre.so.3": "libpcre3",
    "libpcre2-8.so.0": "libpcre2-8-0",
    "libreadline.so.8": "libreadline8",
    "libncurses.so.6": "libncurses6",
    "libtinfo.so.6": "libtinfo6",
    "libncursesw.so.6": "libncursesw6",
    "libsqlite3.so.0": "libsqlite3-0",
    "libxml2.so.2": "libxml2",
    "libgstreamer-1.0.so.0": "libgstreamer1.0-0",
    "libgstreamer-plugins-base-1.0.so.0": "libgstreamer-plugins-base1.0-0",
    "libnotify.so.4": "libnotify4",
    "libsecret-1.so.0": "libsecret-1-0",
    "libappindicator3.so.1": "libappindicator3-1",
    "libjsoncpp.so.25": "libjsoncpp25",
    "libboost_system.so.1.74.0": "libboost-system1.74.0",
    "liblz4.so.1": "liblz4-1",
    "libsodium.so.23": "libsodium23",
    "libevdev.so.2": "libevdev2",
    "libinput.so.10": "libinput10",
    "libdrm.so.2": "libdrm2",
    "libexif.so.12": "libexif12",
    "liblcms2.so.2": "liblcms2-2",
    "libopenjp2.so.7": "libopenjp2-7",
    "libgif.so.7": "libgif7",
    "libx264.so.163": "libx264-163",
    "libavcodec.so.60": "libavcodec60",
    "libavformat.so.60": "libavformat60",
    "libavutil.so.58": "libavutil58",
    "libswscale.so.7": "libswscale7",
}


def _search_dirs() -> list[str]:
    dirs = ["/lib", "/usr/lib", "/lib64", "/usr/lib64", "/usr/local/lib"]
    for base in ("/lib", "/usr/lib"):
        p = Path(base)
        if p.is_dir():
            for child in p.iterdir():
                if child.is_dir() and ("linux-gnu" in child.name or child.name.startswith("x86_64")):
                    dirs.append(str(child))
    out: list[str] = []
    for d in dirs:
        if d not in out and os.path.isdir(d):
            out.append(d)
    return out


class HostLibraryIndex:
    """Lazily-built soname -> path index of the host's library directories."""

    def __init__(self, extra_dirs: list[str] | None = None, log: Log | None = None,
                 only_extra: bool = False):
        base = [] if only_extra else _search_dirs()
        self.dirs = base + list(extra_dirs or [])
        self.log = log or Log("warn")
        self._index: dict[str, str] | None = None

    def _build(self) -> dict[str, str]:
        index: dict[str, str] = {}
        for d in self.dirs:
            try:
                names = os.listdir(d)
            except OSError:
                continue
            for name in names:
                if ".so" not in name:
                    continue
                path = os.path.join(d, name)
                if os.path.islink(path) or os.path.isfile(path):
                    index.setdefault(name, path)
        return index

    @property
    def index(self) -> dict[str, str]:
        if self._index is None:
            self._index = self._build()
        return self._index

    def find(self, soname: str) -> str | None:
        if "/" in soname:
            return soname if os.path.exists(soname) else None
        return self.index.get(soname)


def detect_format(path: str) -> str:
    """Classify what kind of thing *path* is."""
    p = Path(path)
    if p.is_dir():
        return "directory"
    if not p.is_file():
        return "unknown"
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        return "unknown"

    if head[:4] == b"\x7fELF":
        if len(head) >= 11 and head[APPIMAGE_MAGIC_OFFSET:APPIMAGE_MAGIC_OFFSET + 3] in (b"AI\x01", b"AI\x02"):
            return "appimage"
        return "elf"
    if head[:2] == b"MZ":
        return "pe"
    if head[:2] == b"#!":
        return "script"
    if head[:4] in (b"\x1f\x8b\x08\x00",) or head[:2] == b"\x1f\x8b":
        return "tar" if _is_tar(path) else "archive"
    if head[:6] == b"\xfd7zXZ\x00" or head[:3] == b"BZh":
        return "archive"
    if head[:4] == b"PK\x03\x04":
        return "zip"
    if b"ustar" in head[:300] or _is_tar(path):
        return "tar"
    return "unknown"


def _is_tar(path: str) -> bool:
    try:
        return tarfile.is_tarfile(path)
    except Exception:
        return False


def analyze(path: str, log: Log | None = None, host: HostLibraryIndex | None = None,
            guest_sysroot: str = "") -> Analysis:
    """Analyse *path* and return a populated :class:`Analysis`."""
    log = log or Log("warn")
    path = os.path.abspath(path)
    fmt = detect_format(path)
    a = Analysis(path=path, format=fmt)

    if fmt in ("elf", "appimage"):
        a.size = os.path.getsize(path)
        elf = elfmod.parse_elf(path)
        a.elf = elf.info
        a.arch = elf.info.machine
        a.imports = _import_surface(elf)

        foreign = bool(a.arch) and a.arch != native_arch()
        sysroot = guest_sysroot
        if foreign and not sysroot:
            from .foreign import find_sysroot

            sysroot = find_sysroot(a.arch)
        a.sysroot = sysroot

        if foreign:
            # A foreign binary's NEEDED names refer to the *guest* libraries, so
            # resolve them in the guest sysroot; the host's libraries are the
            # wrong architecture and cannot satisfy them.
            index = HostLibraryIndex(extra_dirs=_sysroot_lib_dirs(sysroot), log=log,
                                     only_extra=bool(sysroot))
            if not sysroot:
                index = None
            _resolve(a, elf, index, log)
            if sysroot:
                a.notes.append(f"resolved against guest sysroot {sysroot}")
            else:
                a.notes.append(
                    f"{a.arch} libraries cannot be resolved on this {native_arch()} host; "
                    f"install a cross sysroot (libc6-dev-{debian_arch(a.arch)}-cross) "
                    f"or use a container")
        else:
            _resolve(a, elf, host or HostLibraryIndex(log=log), log)
            _check_symbol_versions(a, elf, log)
    elif fmt == "directory":
        a.notes.append("directory input: analysis limited to the entry executable")
    elif fmt == "pe":
        a.notes.append("PE/Windows binary: run it with Wine or a container runtime, not Liton native mode")
    elif fmt == "script":
        a.notes.append("interpreted script: no native translation required")
    elif fmt in ("tar", "archive", "zip"):
        a.size = os.path.getsize(path)
        a.notes.append(f"{fmt} archive: unpack first, then analyse the payload")
    else:
        a.notes.append("unrecognised format: no ELF translation possible")

    if a.elf and a.elf.machine and a.elf.machine != native_arch():
        from .foreign import choose

        backend = choose(a.elf.machine, log=log)
        if backend:
            a.notes.append(f"{a.elf.machine} binary: translated by {backend.describe()}")
        else:
            a.notes.append(
                f"foreign architecture {a.elf.machine} on a {native_arch()} host: no "
                f"translator installed (FEX, box64, qemu-user-static)"
            )
    return a


def _version_key(version: str) -> tuple[int, ...]:
    """Turn a symbol version like GLIBC_2.34 into a comparable tuple."""
    tail = version.split("_", 1)[-1]
    parts: list[int] = []
    for piece in tail.split("."):
        num = ""
        for ch in piece:
            if ch.isdigit():
                num += ch
            else:
                break
        if num:
            parts.append(int(num))
    return tuple(parts)


def host_symbol_versions(library_path: str) -> set[str]:
    """Symbol versions a shared object on this host exports."""
    try:
        elf = elfmod.parse_elf(library_path)
    except (elfmod.NotElfError, OSError):
        return set()
    return set(elf.version_definitions())


def _check_symbol_versions(a: Analysis, elf: elfmod.Elf, log: Log) -> None:
    """Flag libraries that are present but too old for this build.

    This is the failure mode where every ``NEEDED`` library resolves, yet the
    program still dies with ``version `GLIBC_2.38' not found``: the file exists
    but does not export the required symbol version.
    """
    needs = elf.version_needs()
    if not needs:
        return
    for lib, required in needs.items():
        resolved = next((r for r in a.resolutions if r.soname == lib and r.resolved), None)
        if not resolved or not resolved.path:
            continue
        provided = host_symbol_versions(resolved.path)
        if not provided:
            continue
        too_new = sorted((v for v in required if v not in provided),
                         key=_version_key)
        if too_new:
            highest = too_new[-1]
            a.version_conflicts.append({
                "library": lib,
                "path": resolved.path,
                "required": highest,
                "missing_versions": too_new,
            })
            a.notes.append(
                f"{lib} on this host is too old for this build "
                f"(needs {highest}); a newer base image or container is required"
            )


def _import_surface(elf: elfmod.Elf) -> ImportSurface:
    libs = list(elf.info.needed)
    symbols = elf.imported_symbols()
    groups: dict[str, list[str]] = {}
    symset = set(symbols)
    for group, names in SYMBOL_GROUPS:
        hits = sorted(symset.intersection(names))
        if hits:
            groups[group] = hits
    if any(s.startswith(("_ZN", "_Z")) for s in symbols):
        groups.setdefault("c++", []).append("_Z*")
    return ImportSurface(libraries=libs, symbols=symbols, groups=groups)


def _sysroot_lib_dirs(sysroot: str) -> list[str]:
    """Library directories inside a guest sysroot, in loader search order."""
    if not sysroot:
        return []
    out: list[str] = []
    for rel in ("lib", "usr/lib", "lib64", "usr/lib64", "lib/x86_64-linux-gnu",
                "usr/lib/x86_64-linux-gnu", "lib/aarch64-linux-gnu",
                "usr/lib/aarch64-linux-gnu", "lib/arm-linux-gnueabihf",
                "usr/lib/arm-linux-gnueabihf", "lib/i386-linux-gnu",
                "usr/lib/i386-linux-gnu", "lib/riscv64-linux-gnu",
                "usr/lib/riscv64-linux-gnu"):
        d = os.path.join(sysroot, rel)
        if os.path.isdir(d):
            out.append(d)
    return out


def _resolve(a: Analysis, elf: elfmod.Elf, host: HostLibraryIndex | None, log: Log) -> None:
    search: list[str] = []
    for d in (elf.info.rpath + elf.info.runpath):
        cand = os.path.normpath(os.path.join(os.path.dirname(a.path), d))
        if os.path.isdir(cand):
            search.append(cand)
    local = HostLibraryIndex(extra_dirs=search, log=log, only_extra=True)

    for soname in elf.info.needed:
        res = LibResolution(soname=soname)
        # Prefer a library the application itself ships next to it.
        found = local.find(soname)
        source = "profile" if found else ""
        if not found and host is not None:
            found = host.find(soname)
            source = "host" if found else ""
        if found:
            res.resolved = True
            res.path = found
            res.source = source
        elif host is not None:
            a.missing.append(soname)
        else:
            # No index at all: the library's presence is simply unknown, which is
            # different from knowing it is absent.
            res.source = "unknown"
        pkg = SONAME_PACKAGES.get(soname)
        if not pkg and found:
            pkg = package_for_path(found)
        res.provider = pkg or ""
        a.resolutions.append(res)
        if pkg and not res.resolved:
            a.package_hints[soname] = pkg


_dpkg_cache: dict[str, str] = {}


def package_for_path(path: str) -> str:
    """Best-effort: which Debian package owns *path* (uses ``dpkg -S``)."""
    if path in _dpkg_cache:
        return _dpkg_cache[path]
    result = ""
    dpkg = shutil.which("dpkg")
    if dpkg:
        try:
            out = subprocess.run([dpkg, "-S", path], capture_output=True, text=True, timeout=10)
            if out.returncode == 0 and out.stdout:
                # format: "pkg:arch: /path"
                head = out.stdout.splitlines()[0]
                result = head.split(":", 1)[0].strip()
        except Exception:
            result = ""
    _dpkg_cache[path] = result
    return result


def is_appimage(path: str) -> bool:
    return detect_format(path) == "appimage"


def file_digest(path: str) -> str:
    return sha256_file(path)
