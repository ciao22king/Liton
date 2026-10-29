"""Tests for the foreign-architecture translation backends.

These cover the *decision* logic and the artifacts, which is what can be tested
without the translators themselves installed.  The end-to-end proof that a
foreign binary really runs lives in ``test_translate_e2e.py``, which skips
unless a translator is present.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from liton import foreign
from liton.compiler import compile_runner
from liton.models import Analysis, ElfInfo, StrategyPlan
from liton.planner import plan_rules
from liton.util import Log


def probe_from(mapping: dict[str, str]):
    return lambda name: mapping.get(name)


# -- backend selection ------------------------------------------------------

def test_fex_preferred_on_aarch64_for_x86_64():
    tr = foreign.choose("x86-64", host_arch="aarch64",
                        probe=probe_from({"FEXInterpreter": "/usr/bin/FEXInterpreter"}),
                        log=Log("error"))
    assert tr is not None and tr.kind == "fex"


def test_box64_used_when_fex_absent():
    tr = foreign.choose("x86-64", host_arch="aarch64",
                        probe=probe_from({"box64": "/usr/bin/box64"}),
                        log=Log("error"))
    assert tr is not None and tr.kind == "box64"


def test_qemu_is_the_universal_fallback():
    tr = foreign.choose("x86-64", host_arch="aarch64",
                        probe=probe_from({"qemu-x86_64-static": "/usr/bin/qemu-x86_64-static"}),
                        log=Log("error"))
    assert tr is not None and tr.kind == "qemu"


def test_riscv64_host_uses_box64_not_fex():
    tr = foreign.choose("x86-64", host_arch="riscv",
                        probe=probe_from({"FEXInterpreter": "/usr/bin/FEXInterpreter",
                                          "box64": "/usr/bin/box64"}),
                        log=Log("error"))
    assert tr is not None and tr.kind == "box64"


def test_box86_for_i386_on_arm():
    tr = foreign.choose("i386", host_arch="arm",
                        probe=probe_from({"box86": "/usr/bin/box86"}),
                        log=Log("error"))
    assert tr is not None and tr.kind == "box86"


def test_no_translator_returns_none():
    assert foreign.choose("x86-64", host_arch="aarch64",
                          probe=probe_from({}), log=Log("error")) is None


def test_native_arch_needs_no_translator():
    assert foreign.choose("x86-64", host_arch="x86-64",
                          probe=probe_from({"qemu-x86_64": "/x"}), log=Log("error")) is None


def test_qemu_gets_sysroot_and_prefix(tmp_path, monkeypatch):
    sysroot = tmp_path / "aarch64-linux-gnu"
    (sysroot / "lib").mkdir(parents=True)
    monkeypatch.setenv("LITON_SYSROOT", str(sysroot))
    tr = foreign.choose("aarch64", host_arch="x86-64",
                        probe=probe_from({"qemu-aarch64-static": "/usr/bin/qemu-aarch64-static"}),
                        log=Log("error"))
    assert tr.sysroot == str(sysroot)
    assert tr.argv_prefix == ["-L", str(sysroot)]
    assert foreign.translator_argv(tr, "/app/bin") == [
        "/usr/bin/qemu-aarch64-static", "-L", str(sysroot), "/app/bin"]


def test_candidate_order_never_includes_fex_off_arm():
    assert "fex" not in foreign.candidate_order("x86-64", "x86-64")
    assert "fex" not in foreign.candidate_order("x86-64", "riscv")
    assert foreign.candidate_order("x86-64", "aarch64")[0] == "fex"


def test_static_qemu_preferred_over_dynamic():
    tr = foreign.choose("aarch64", host_arch="x86-64",
                        probe=probe_from({"qemu-aarch64-static": "/usr/bin/qemu-aarch64-static",
                                          "qemu-aarch64": "/usr/bin/qemu-aarch64"}),
                        log=Log("error"))
    assert tr.path.endswith("-static")


# -- FEX specifics ----------------------------------------------------------

def test_fex_env_pins_caches_inside_the_profile(tmp_path):
    env = foreign.fex_env(str(tmp_path))
    assert env["FEX_APP_DATA_LOCATION"].startswith(str(tmp_path))
    assert env["FEX_APP_CACHE_LOCATION"].startswith(str(tmp_path))
    # The JIT cache must be inside the profile, or nothing is remembered.
    assert str(tmp_path) in env["FEX_APP_CACHE_LOCATION"]


def test_fex_rootfs_override_wins(tmp_path, monkeypatch):
    rootfs = tmp_path / "rootfs"
    (rootfs / "lib" / "x86_64-linux-gnu").mkdir(parents=True)
    (rootfs / "lib" / "x86_64-linux-gnu" / "libc.so.6").write_text("")
    monkeypatch.setenv("LITON_FEX_ROOTFS", str(rootfs))
    assert foreign.find_fex_rootfs() == str(rootfs)


def test_fex_rootfs_ignores_a_directory_without_libc(tmp_path, monkeypatch):
    (tmp_path / "empty").mkdir()
    monkeypatch.setenv("LITON_FEX_ROOTFS", str(tmp_path / "empty"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "no-such-data"))
    assert foreign.find_fex_rootfs() == ""


def test_fex_rootfs_found_in_data_dir(tmp_path, monkeypatch):
    rootfs = tmp_path / "fex-emu" / "RootFS" / "Debian_x86_64"
    (rootfs / "usr" / "lib" / "x86_64-linux-gnu").mkdir(parents=True)
    (rootfs / "usr" / "lib" / "x86_64-linux-gnu" / "libc.so.6").write_text("")
    monkeypatch.delenv("LITON_FEX_ROOTFS", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert foreign.find_fex_rootfs() == str(rootfs)


def test_translator_roundtrips_through_dict():
    tr = foreign.Translator(name="qemu-aarch64-static", kind="qemu",
                            path="/usr/bin/qemu-aarch64-static", host_arch="x86-64",
                            guest_arch="aarch64", argv_prefix=["-L", "/sys"],
                            sysroot="/sys", env={"QEMU_LD_PREFIX": "/sys"})
    again = foreign.Translator.from_dict(tr.to_dict())
    assert again == tr


# -- planner integration ----------------------------------------------------

def _foreign_analysis(tmp_path) -> Analysis:
    binary = tmp_path / "app"
    binary.write_bytes(b"\x7fELF" + b"\x00" * 60)
    a = Analysis(path=str(binary), format="elf", arch="x86-64", size=binary.stat().st_size)
    a.elf = ElfInfo(path=str(binary), valid=True, machine="x86-64", e_type="DYN")
    return a


def test_planner_chooses_translate_not_container(tmp_path, monkeypatch):
    monkeypatch.setattr("liton.planner.native_arch", lambda: "aarch64")
    monkeypatch.setattr("liton.planner.choose",
                        lambda guest, log=None: foreign.Translator(
                            name="FEXInterpreter", kind="fex", path="/usr/bin/FEXInterpreter",
                            host_arch="aarch64", guest_arch="x86-64"))
    plan = plan_rules(_foreign_analysis(tmp_path), log=Log("error"))
    assert plan.strategy == "translate"
    assert plan.translator["kind"] == "fex"
    assert plan.runtime == "host"
    assert any(s.kind == "translate" for s in plan.steps)


def test_planner_falls_back_to_container_without_translator(tmp_path, monkeypatch):
    monkeypatch.setattr("liton.planner.native_arch", lambda: "aarch64")
    monkeypatch.setattr("liton.planner.choose", lambda guest, log=None: None)
    plan = plan_rules(_foreign_analysis(tmp_path), log=Log("error"))
    assert plan.strategy == "container"
    assert plan.runtime == "docker"
    assert any("no translation backend" in n for n in plan.notes)


def test_native_binary_is_not_treated_as_foreign(tmp_path, monkeypatch):
    monkeypatch.setattr("liton.planner.native_arch", lambda: "x86-64")
    plan = plan_rules(_foreign_analysis(tmp_path), log=Log("error"))
    assert plan.strategy != "translate"


# -- generated runner -------------------------------------------------------

def _fex_plan() -> StrategyPlan:
    tr = foreign.Translator(name="FEXInterpreter", kind="fex", path="/usr/bin/FEXInterpreter",
                            host_arch="aarch64", guest_arch="x86-64", sysroot="/rootfs")
    return StrategyPlan(strategy="translate", translator=tr.to_dict(),
                        steps=[])


def test_fex_runner_pins_cache_and_rootfs(tmp_path):
    analysis = Analysis(path="/app/game", format="elf", arch="x86-64")
    script = compile_runner(_fex_plan(), analysis, tmp_path)
    assert "FEX_APP_CACHE_LOCATION" in script
    assert "FEX_APP_DATA_LOCATION" in script
    assert "FEX_ROOTFS=/rootfs" in script
    assert 'exec "$LITON_TRANSLATOR"' in script
    assert "$LITON_APP" in script


def test_fex_runner_accepts_runtime_rootfs_when_none_baked(tmp_path):
    plan = _fex_plan()
    plan.translator["sysroot"] = ""
    analysis = Analysis(path="/app/game", format="elf", arch="x86-64")
    script = compile_runner(plan, analysis, tmp_path)
    assert "LITON_FEX_ROOTFS" in script


def test_runner_script_is_valid_shell(tmp_path):
    analysis = Analysis(path="/app/game", format="elf", arch="x86-64")
    script = compile_runner(_fex_plan(), analysis, tmp_path)
    out = subprocess.run(["/bin/sh", "-n"], input=script, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_qemu_runner_quotes_the_sysroot(tmp_path):
    tr = foreign.Translator(name="qemu-aarch64-static", kind="qemu",
                            path="/usr/bin/qemu-aarch64-static", host_arch="x86-64",
                            guest_arch="aarch64", argv_prefix=["-L", "/sys root"],
                            sysroot="/sys root")
    plan = StrategyPlan(strategy="translate", translator=tr.to_dict(), steps=[])
    analysis = Analysis(path="/app/game", format="elf", arch="aarch64")
    script = compile_runner(plan, analysis, tmp_path)
    out = subprocess.run(["/bin/sh", "-n"], input=script, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "'/sys root'" in script


def test_runner_refuses_a_missing_translator(tmp_path):
    tr = foreign.Translator(name="FEXInterpreter", kind="fex",
                            path="/nonexistent/FEXInterpreter",
                            host_arch="aarch64", guest_arch="x86-64")
    plan = StrategyPlan(strategy="translate", translator=tr.to_dict(), steps=[])
    analysis = Analysis(path="/app/game", format="elf", arch="x86-64")
    script = compile_runner(plan, analysis, tmp_path)
    out = subprocess.run(["/bin/sh"], input=script, capture_output=True, text=True)
    assert out.returncode == 127
    assert "missing" in out.stderr
