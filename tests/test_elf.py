"""ELF parser tests, cross-checked against the system's own tools when present."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from liton.elf import Elf, NotElfError, looks_like_elf, parse_elf

READELF = shutil.which("readelf")


def test_rejects_non_elf(tmp_path):
    f = tmp_path / "nope.bin"
    f.write_bytes(b"not an elf at all")
    assert not looks_like_elf(str(f))
    with pytest.raises(NotElfError):
        parse_elf(str(f))


def test_parses_system_binary():
    info = parse_elf("/bin/ls").info
    assert info.valid
    assert info.machine == "x86-64"
    assert info.e_type == "DYN"
    assert "libc.so.6" in info.needed
    assert info.interpreter.endswith("ld-linux-x86-64.so.2")


def test_imported_symbols_present():
    symbols = parse_elf("/bin/ls").imported_symbols()
    assert len(symbols) > 20
    assert "getenv" in symbols


@pytest.mark.skipif(READELF is None, reason="readelf not installed")
def test_needed_matches_readelf(simple_app, bundled_app):
    for path in (str(simple_app), str(bundled_app), "/bin/ls"):
        mine = parse_elf(path).info.needed
        out = subprocess.run([READELF, "-d", path], capture_output=True, text=True).stdout
        ref = [ln.split("[")[1].rstrip("]") for ln in out.splitlines() if "(NEEDED)" in ln]
        assert mine == ref, path


@pytest.mark.skipif(READELF is None, reason="readelf not installed")
def test_symbols_match_readelf(simple_app):
    path = str(simple_app)
    mine = set(parse_elf(path).imported_symbols())
    out = subprocess.run([READELF, "--dyn-syms", "-W", path],
                         capture_output=True, text=True).stdout
    ref = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[6] == "UND":
            ref.add(parts[7].split("@")[0])
    assert mine == ref


def test_rpath_is_split_on_colon(tmp_path):
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("gcc not installed")
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    src = tmp_path / "x.c"
    src.write_text("int main(void){return 0;}\n")
    out = tmp_path / "x"
    subprocess.run([gcc, "-O2", str(src), "-o", str(out),
                    f"-Wl,-rpath,{tmp_path}/a:{tmp_path}/b"], check=True)
    info = parse_elf(str(out)).info
    joined = info.rpath + info.runpath
    assert f"{tmp_path}/a" in joined
    assert f"{tmp_path}/b" in joined


def test_bundled_app_runpath_uses_origin(bundled_app):
    info = parse_elf(str(bundled_app)).info
    assert "./lib" in info.runpath or "./lib" in info.rpath


def test_bad_class_raises(tmp_path):
    f = tmp_path / "weird"
    f.write_bytes(b"\x7fELF\x09" + b"\x00" * 60)
    with pytest.raises(NotElfError):
        parse_elf(str(f))


def test_version_needs_reports_glibc(simple_app):
    needs = parse_elf(str(simple_app)).version_needs()
    assert "libc.so.6" in needs
    assert any(v.startswith("GLIBC_") for v in needs["libc.so.6"])


@pytest.mark.skipif(READELF is None, reason="readelf not installed")
def test_version_needs_matches_readelf(simple_app):
    import re

    path = str(simple_app)
    mine = {k: set(v) for k, v in parse_elf(path).version_needs().items()}
    out = subprocess.run([READELF, "-V", "-W", path], capture_output=True, text=True).stdout
    ref: dict[str, set[str]] = {}
    cur = None
    inside = False
    for line in out.splitlines():
        if line.startswith("Version needs section"):
            inside = True
            continue
        if line.startswith(("Version definition", "Version symbols")):
            inside = False
            continue
        if not inside:
            continue
        m = re.search(r"File:\s*(\S+)", line)
        if m:
            cur = m.group(1)
            ref.setdefault(cur, set())
            continue
        m2 = re.search(r"Name:\s*(\S+)", line)
        if m2 and cur:
            ref[cur].add(m2.group(1))
    assert mine == ref


@pytest.mark.skipif(READELF is None, reason="readelf not installed")
def test_version_definitions_matches_readelf():
    import re

    path = "/lib/x86_64-linux-gnu/libc.so.6"
    if not os.path.exists(path):
        pytest.skip("libc not at the expected path")
    mine = set(parse_elf(path).version_definitions())
    out = subprocess.run([READELF, "-V", "-W", path], capture_output=True, text=True).stdout
    ref = set()
    inside = False
    for line in out.splitlines():
        if line.startswith("Version definition"):
            inside = True
            continue
        if line.startswith(("Version needs", "Version symbols")):
            inside = False
            continue
        if inside:
            m = re.search(r"Name:\s*(\S+)", line)
            if m:
                ref.add(m.group(1))
    assert mine == ref
    assert "GLIBC_2.2.5" in mine
