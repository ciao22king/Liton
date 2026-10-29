"""End-to-end proof that Liton really translates and runs a foreign binary.

This builds a genuine binary for another architecture and runs it through the
full pipeline - analyse, plan, compile, remember, launch.  It skips when no
cross-compiler or translator is present, since neither is a Liton dependency.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from liton.foreign import choose
from liton.runner import Engine
from liton.store import fingerprint
from liton.util import Log, native_arch

SOURCE = r"""
#include <stdio.h>
#include <math.h>
int main(int argc, char **argv) {
    printf("translated argc=%d sqrt=%.3f\n", argc, sqrt(2.0));
    return 0;
}
"""


def _cross_compilers() -> list[tuple[str, str]]:
    """(guest arch, compiler) pairs available on this host."""
    candidates = [
        ("aarch64", "aarch64-linux-gnu-gcc"),
        ("arm", "arm-linux-gnueabihf-gcc"),
        ("riscv", "riscv64-linux-gnu-gcc"),
        ("i386", "i686-linux-gnu-gcc"),
        ("x86-64", "x86_64-linux-gnu-gcc"),
    ]
    return [(arch, cc) for arch, cc in candidates if shutil.which(cc)]


def _build(tmp_path: Path, compiler: str) -> Path:
    src = tmp_path / "guest.c"
    src.write_text(SOURCE)
    out = tmp_path / "guest"
    subprocess.run([compiler, str(src), "-o", str(out), "-lm"], check=True)
    return out


@pytest.mark.parametrize("arch,compiler", _cross_compilers())
def test_foreign_binary_runs_through_liton(tmp_path, arch, compiler):
    if arch == native_arch():
        pytest.skip("native architecture needs no translation")
    if choose(arch, log=Log("error")) is None:
        pytest.skip(f"no translator installed for {arch}")

    binary = _build(tmp_path, compiler)
    engine = Engine(log=Log("error"))
    profile = engine.translate(str(binary), force=True)

    assert profile.plan["strategy"] == "translate"
    assert profile.plan["translator"]["kind"], "a translator must be recorded"

    rc = engine.run(str(binary), args=["hello", "world"])
    assert rc == 0, "the translated binary should exit 0"

    # A second run must reuse the remembered profile, not re-analyse.
    profile2 = engine.store.load(fingerprint(str(binary), arch=arch))
    assert profile2 is not None
    assert profile2.runs >= 1


def test_remembered_lookup_skips_analysis(tmp_path, monkeypatch):
    """The remembered path must not analyse the binary again."""
    if not _cross_compilers():
        pytest.skip("no cross-compiler available")
    arch, compiler = _cross_compilers()[0]
    if arch == native_arch() or choose(arch, log=Log("error")) is None:
        pytest.skip(f"no translator for {arch}")

    binary = _build(tmp_path, compiler)
    engine = Engine(log=Log("error"))
    engine.translate(str(binary), force=True)

    import liton.runner as runner

    def explode(*a, **k):
        raise AssertionError("analysis ran on the remembered path")

    monkeypatch.setattr(runner, "analyze", explode)
    rc = engine.run(str(binary))
    assert rc == 0


def test_translated_runner_carries_the_args(tmp_path):
    """Arguments must reach the guest program, not be swallowed."""
    if not _cross_compilers():
        pytest.skip("no cross-compiler available")
    arch, compiler = _cross_compilers()[0]
    if arch == native_arch() or choose(arch, log=Log("error")) is None:
        pytest.skip(f"no translator for {arch}")

    binary = _build(tmp_path, compiler)
    engine = Engine(log=Log("error"))
    profile = engine.translate(str(binary), force=True)
    run_sh = Path(profile.artifacts[0]).parent / "run.sh"
    out = subprocess.run(["/bin/sh", str(run_sh), "a", "b", "c"],
                         capture_output=True, text=True)
    assert out.returncode == 0
    assert "translated argc=4" in out.stdout
