"""Store and engine tests: the memory behaviour and the full pipeline."""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from liton.analysis import analyze
from liton.runner import Engine
from liton.store import Store, fingerprint, make_profile, plan_from_dict
from liton.util import Log


def quiet():
    return Log("error")


def test_fingerprint_is_stable_and_path_sensitive(simple_app, tmp_path):
    a = analyze(str(simple_app), log=quiet())
    k1 = fingerprint(str(simple_app), a)
    k2 = fingerprint(str(simple_app), a)
    assert k1 == k2

    copy = tmp_path / "hello-copy"
    copy.write_bytes(simple_app.read_bytes())
    a2 = analyze(str(copy), log=quiet())
    assert fingerprint(str(copy), a2) != k1  # path is part of identity


def test_fingerprint_changes_when_build_changes(simple_app, tmp_path):
    import shutil

    copy = tmp_path / "hello"
    shutil.copy2(simple_app, copy)
    a1 = analyze(str(copy), log=quiet())
    k1 = fingerprint(str(copy), a1)

    gcc = shutil.which("gcc")
    src = tmp_path / "v2.c"
    src.write_text('int main(void){return 7;}\n')
    subprocess.run([gcc, "-O2", str(src), "-o", str(copy)], check=True)
    a2 = analyze(str(copy), log=quiet())
    assert fingerprint(str(copy), a2) != k1


def test_store_roundtrip(simple_app):
    engine = Engine(log=quiet())
    profile = engine.translate(str(simple_app))
    again = engine.store.load(profile.key)
    assert again is not None
    assert again.source_path == profile.source_path
    assert again.plan["strategy"] == "native"


def test_translate_is_idempotent(simple_app):
    engine = Engine(log=quiet())
    p1 = engine.translate(str(simple_app))
    created = p1.created
    time.sleep(0.01)
    p2 = engine.translate(str(simple_app))
    assert p2.key == p1.key
    assert p2.created == created


def test_force_recompiles(simple_app):
    engine = Engine(log=quiet())
    engine.translate(str(simple_app))
    p = engine.translate(str(simple_app), force=True)
    assert p.updated >= p.created


def test_run_executes_and_remembers(simple_app, capfd):
    engine = Engine(log=quiet())
    rc = engine.run(str(simple_app))
    assert rc == 0
    out = capfd.readouterr().out
    assert "value=" in out

    profile = engine.store.find_by_source(str(simple_app))
    assert profile is not None
    assert profile.runs == 1

    rc2 = engine.run(str(simple_app))
    assert rc2 == 0
    profile = engine.store.load(profile.key)
    assert profile.runs == 2


def test_run_forwards_arguments(tmp_path):
    gcc = __import__("shutil").which("gcc")
    if not gcc:
        pytest.skip("gcc not installed")
    src = tmp_path / "args.c"
    src.write_text('#include <stdio.h>\nint main(int c,char**v){'
                   'for(int i=1;i<c;i++)printf("arg:%s\\n",v[i]);return 0;}\n')
    out = tmp_path / "args"
    subprocess.run([gcc, "-O2", str(src), "-o", str(out)], check=True)

    engine = Engine(log=quiet())
    rc = engine.run(str(out), args=["one", "two"])
    assert rc == 0


def test_bundled_app_runs_through_profile(bundled_app, capfd):
    engine = Engine(log=quiet())
    rc = engine.run(str(bundled_app))
    assert rc == 0
    assert "from bundled lib" in capfd.readouterr().out


def test_missing_dep_profile_records_package(missing_dep_app):
    engine = Engine(log=quiet())
    profile = engine.translate(str(missing_dep_app))
    plan = plan_from_dict(profile.plan)
    assert "libsdl2-2.0-0" in plan.packages
    assert profile.analysis["missing"] == ["libSDL2-2.0.so.0"]


def test_forget_removes_profile(simple_app):
    engine = Engine(log=quiet())
    profile = engine.translate(str(simple_app))
    assert engine.store.load(profile.key) is not None
    assert engine.store.delete(profile.key)
    assert engine.store.load(profile.key) is None


def test_list_reports_profiles(simple_app, bundled_app):
    engine = Engine(log=quiet())
    engine.translate(str(simple_app))
    engine.translate(str(bundled_app))
    assert len(engine.store.all()) == 2


def test_schema_mismatch_is_treated_as_absent(simple_app):
    engine = Engine(log=quiet())
    profile = engine.translate(str(simple_app))
    path = engine.store.path_for(profile.key)
    text = path.read_text().replace('"schema": 1', '"schema": 999')
    path.write_text(text)
    assert engine.store.load(profile.key) is None


def test_corrupt_profile_is_treated_as_absent(simple_app):
    engine = Engine(log=quiet())
    profile = engine.translate(str(simple_app))
    engine.store.path_for(profile.key).write_text("{not json")
    assert engine.store.load(profile.key) is None


def test_run_translates_when_not_remembered(simple_app, capfd):
    engine = Engine(log=quiet())
    assert engine.store.all() == []
    rc = engine.run(str(simple_app))
    assert rc == 0
    assert len(engine.store.all()) == 1


def test_fingerprint_cache_invalidates_on_change(simple_app):
    first = fingerprint(str(simple_app), arch="x86-64")
    assert fingerprint(str(simple_app), arch="x86-64") == first
    simple_app.write_bytes(simple_app.read_bytes() + b"\x00")
    assert fingerprint(str(simple_app), arch="x86-64") != first


def test_fingerprint_does_not_read_contents(simple_app, monkeypatch):
    """Finding a remembered profile must not hash the file."""
    import liton.store as store

    def boom(*a, **k):
        raise AssertionError("fingerprint must not read file contents")

    monkeypatch.setattr(store, "sha256_file", boom)
    assert fingerprint(str(simple_app), arch="x86-64")


def test_remembered_run_does_no_analysis(simple_app, monkeypatch):
    """A second run must not re-analyse: that is the whole promise."""
    engine = Engine(log=quiet())
    engine.translate(str(simple_app))

    import liton.runner as runner

    def boom(*a, **k):
        raise AssertionError("a remembered run must not analyse")

    monkeypatch.setattr(runner, "analyze", boom)
    assert engine.run(str(simple_app)) == 0


def test_superseded_profiles_are_pruned(simple_app, tmp_path):
    import shutil

    copy = tmp_path / "app"
    shutil.copy2(simple_app, copy)
    engine = Engine(log=quiet())
    first = engine.translate(str(copy))

    src = tmp_path / "v2.c"
    src.write_text('int main(void){return 3;}\n')
    subprocess.run(["gcc", "-O2", str(src), "-o", str(copy)], check=True)
    second = engine.translate(str(copy))

    assert second.key != first.key
    assert engine.store.load(first.key) is None
    assert len(engine.store.all()) == 1


def test_profile_json_written_atomically(simple_app):
    engine = Engine(log=quiet())
    profile = engine.translate(str(simple_app))
    directory = engine.store.profile_dir(profile.key)
    assert not list(directory.glob(".*tmp"))
    assert (directory / "profile.json").is_file()
