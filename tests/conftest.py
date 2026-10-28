"""Shared fixtures: real compiled binaries built with the system C compiler."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

GCC = shutil.which("gcc")
needs_gcc = pytest.mark.skipif(GCC is None, reason="gcc not installed")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own profile store."""
    home = tmp_path / "liton-home"
    monkeypatch.setenv("LITON_HOME", str(home))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITON_LLM_API_KEY", raising=False)
    yield home


def _cc(source: str, out: Path, *flags: str) -> Path:
    src = out.with_suffix(".c")
    src.write_text(source)
    # Library flags must come after the source, or the linker drops the symbol.
    subprocess.run([GCC, str(src), "-o", str(out), *flags], check=True,
                   capture_output=True)
    return out


@pytest.fixture(scope="session")
def simple_app(tmp_path_factory) -> Path:
    """A plain dynamically linked executable."""
    if GCC is None:
        pytest.skip("gcc not installed")
    d = tmp_path_factory.mktemp("simple")
    out = d / "hello"
    _cc('#include <stdio.h>\n#include <math.h>\n'
        'int main(int c,char**v){printf("value=%g\\n", sqrt(2.0));return 0;}\n',
        out, "-O2", "-lm")
    return out


@pytest.fixture(scope="session")
def bundled_app(tmp_path_factory) -> Path:
    """An app that ships its own private shared library and an $ORIGIN rpath."""
    if GCC is None:
        pytest.skip("gcc not installed")
    d = tmp_path_factory.mktemp("bundled")
    libdir = d / "app" / "lib"
    libdir.mkdir(parents=True)
    _cc('const char* greet(void){return "from bundled lib";}\n',
        libdir / "libgreet.so", "-shared", "-fPIC",
        "-Wl,-soname,libgreet.so")
    out = d / "app" / "greeter"
    _cc('#include <stdio.h>\nconst char* greet(void);\n'
        'int main(void){printf("%s\\n", greet());return 0;}\n',
        out, "-O2", "-L" + str(libdir), "-lgreet", "-Wl,-rpath,$ORIGIN/lib")
    return out


@pytest.fixture(scope="session")
def missing_dep_app(tmp_path_factory) -> Path:
    """An app linked against a library that is then deleted from the tree."""
    if GCC is None:
        pytest.skip("gcc not installed")
    d = tmp_path_factory.mktemp("missing")
    fake = d / "fake"
    fake.mkdir()
    _cc('void SDL_Init(void){}\nvoid SDL_CreateWindow(void){}\n'
        'void SDL_PollEvent(void){}\n',
        fake / "libSDL2-2.0.so.0", "-shared", "-fPIC",
        "-Wl,-soname,libSDL2-2.0.so.0")
    out = d / "game"
    _cc('#include <stdio.h>\nvoid SDL_Init(void);\n'
        'int main(void){SDL_Init();printf("game ran\\n");return 0;}\n',
        out, "-O2", "-L" + str(fake), "-l:libSDL2-2.0.so.0",
        "-Wl,-rpath," + str(fake))
    shutil.rmtree(fake)
    return out


@pytest.fixture(scope="session")
def appimage_like(tmp_path_factory) -> Path:
    """A file carrying the AppImage magic, enough to exercise the unpack path."""
    if GCC is None:
        pytest.skip("gcc not installed")
    d = tmp_path_factory.mktemp("appimage")
    base = d / "true"
    _cc("int main(void){return 0;}\n", base, "-O2")
    data = bytearray(base.read_bytes())
    data[8:11] = b"AI\x02"
    out = d / "My.AppImage"
    out.write_bytes(bytes(data))
    return out


@pytest.fixture(scope="session")
def pe_like(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("pe")
    out = d / "setup.exe"
    out.write_bytes(b"MZ" + b"\x00" * 512)
    return out


@pytest.fixture(scope="session")
def tar_bundle(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("tar")
    payload = d / "payload"
    payload.write_text("data")
    out = d / "bundle.tar"
    with tarfile.open(out, "w") as tf:
        tf.add(payload, arcname="payload")
    return out
