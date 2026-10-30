"""Analyzer tests: format detection, dependency resolution, package mapping."""

from __future__ import annotations

import os
import shutil

from liton.analysis import HostLibraryIndex, SONAME_PACKAGES, analyze, detect_format


def test_detects_elf(simple_app):
    assert detect_format(str(simple_app)) == "elf"
    a = analyze(str(simple_app))
    assert a.format == "elf"
    assert a.arch == "x86-64"
    assert a.size > 0
    assert a.elf is not None and a.elf.valid


def test_detects_appimage(appimage_like):
    assert detect_format(str(appimage_like)) == "appimage"


def test_detects_pe(pe_like):
    assert detect_format(str(pe_like)) == "pe"
    a = analyze(str(pe_like))
    assert any("Wine" in n for n in a.notes)


def test_detects_tar(tar_bundle):
    assert detect_format(str(tar_bundle)) in ("tar", "archive")


def test_detects_script(tmp_path):
    f = tmp_path / "run.sh"
    f.write_text("#!/bin/sh\necho hi\n")
    os.chmod(f, 0o755)
    assert detect_format(str(f)) == "script"


def test_detects_directory(tmp_path):
    assert detect_format(str(tmp_path)) == "directory"


def test_detects_unknown(tmp_path):
    f = tmp_path / "blob"
    f.write_bytes(b"\x00\x01\x02\x03" * 16)
    assert detect_format(str(f)) == "unknown"


def test_resolves_system_libraries(simple_app):
    a = analyze(str(simple_app))
    assert "libc.so.6" in a.elf.needed
    assert a.missing == []
    by_name = {r.soname: r for r in a.resolutions}
    assert by_name["libc.so.6"].resolved
    assert by_name["libc.so.6"].path


def test_bundled_library_resolved_from_runpath(bundled_app):
    a = analyze(str(bundled_app))
    assert "libgreet.so" in a.elf.needed
    assert a.missing == []
    res = {r.soname: r for r in a.resolutions}["libgreet.so"]
    assert res.resolved
    assert res.source == "profile"
    assert res.path.endswith("libgreet.so")


def test_missing_library_and_package_hint(missing_dep_app):
    a = analyze(str(missing_dep_app))
    assert "libSDL2-2.0.so.0" in a.missing
    assert a.package_hints.get("libSDL2-2.0.so.0") == "libsdl2-2.0-0"


def test_capability_group_detected(missing_dep_app):
    a = analyze(str(missing_dep_app))
    assert "SDL" in a.imports.summary()


def test_cpp_symbols_flagged(bundled_app):
    # The bundled app is C, so there should be no c++ marker.
    a = analyze(str(bundled_app))
    assert "c++" not in a.imports.summary()


def test_soname_table_is_sane():
    assert SONAME_PACKAGES["libc.so.6"] == "libc6"
    assert SONAME_PACKAGES["libstdc++.so.6"] == "libstdc++6"
    for soname, pkg in SONAME_PACKAGES.items():
        assert soname and pkg and " " not in soname


def test_version_conflict_detected_against_old_library(tmp_path):
    """A present-but-too-old library must be reported as a version conflict."""
    gcc = shutil.which("gcc")
    if not gcc:
        pytest.skip("gcc not installed")
    import subprocess as sp

    app_src = tmp_path / "app.c"
    app_src.write_text("int main(void){return 0;}\n")
    app = tmp_path / "app"
    sp.run([gcc, "-O2", str(app_src), "-o", str(app)], check=True)

    stub_src = tmp_path / "stub.c"
    stub_src.write_text("int __libc_start_main(void){return 0;}\n")
    vmap = tmp_path / "v.map"
    vmap.write_text("GLIBC_2.2.5 { global: __libc_start_main; };\n")
    stub = tmp_path / "libc.so.6"
    sp.run([gcc, "-shared", "-fPIC", str(stub_src), "-Wl,--version-script=" + str(vmap),
            "-o", str(stub)], check=True)

    from liton.analysis import _check_symbol_versions
    from liton.models import Analysis, LibResolution
    from liton.util import Log
    from liton import elf as elfmod

    base = analyze(str(app))
    a = Analysis(path=base.path, format="elf", arch="x86-64", elf=base.elf,
                 imports=base.imports,
                 resolutions=[LibResolution("libc.so.6", True, str(stub), "libc6", "host")])
    _check_symbol_versions(a, elfmod.parse_elf(str(app)), Log("error"))
    assert a.version_conflicts
    assert a.version_conflicts[0]["library"] == "libc.so.6"
    assert any("too old" in n for n in a.notes)


def test_version_conflict_escalates_to_container(tmp_path):
    from liton.models import Analysis, LibResolution, StrategyPlan
    from liton.planner import plan_rules

    a = Analysis(path="/tmp/x", format="elf", arch="x86-64",
                 resolutions=[LibResolution("libc.so.6", True, "/lib/libc.so.6",
                                            "libc6", "host")],
                 version_conflicts=[{"library": "libc.so.6", "required": "GLIBC_2.38",
                                     "missing_versions": ["GLIBC_2.38"]}])
    plan = plan_rules(a)
    assert plan.strategy == "container"
    assert plan.runtime == "docker"
    assert plan.steps[0].params["image"] == "ubuntu:24.04"
    assert any("symbol-version conflicts" in n for n in plan.notes)


def test_base_image_selection():
    from liton.models import Analysis
    from liton.planner import _suggest_base_image

    def with_req(req):
        return Analysis(version_conflicts=[{"library": "libc.so.6", "required": req}])

    assert _suggest_base_image(with_req("GLIBC_2.34")) == "debian:bookworm-slim"
    assert _suggest_base_image(with_req("GLIBC_2.38")) == "ubuntu:24.04"
    assert _suggest_base_image(with_req("GLIBC_2.41")) == "debian:trixie-slim"
    assert _suggest_base_image(Analysis()) == "debian:stable-slim"


def test_host_index_finds_libc():
    index = HostLibraryIndex()
    found = index.find("libc.so.6")
    assert found is not None
    assert os.path.exists(found)


# ---------------------------------------------------------------------------
# Deterministic results regardless of what the host has installed
# ---------------------------------------------------------------------------

def test_assumed_missing_overrides_a_present_host_library(missing_dep_app, monkeypatch):
    """A host that really has the library must still be able to model its absence.

    `missing_dep_app` links SDL2 and then deletes it, but a desktop host ships
    `libSDL2-2.0.so.0`, so without the override the app resolves cleanly and the
    "missing dependency" path is never exercised. That is a real host-dependence
    bug, not a hypothetical one.
    """
    monkeypatch.setenv("LITON_ASSUME_MISSING", "libSDL2-2.0.so.0")
    a = analyze(str(missing_dep_app))
    assert "libSDL2-2.0.so.0" in a.missing
    assert a.package_hints["libSDL2-2.0.so.0"] == "libsdl2-2.0-0"


def test_known_missing_argument_overrides_a_present_host_library(missing_dep_app):
    a = analyze(str(missing_dep_app), known_missing=["libSDL2-2.0.so.0"])
    assert "libSDL2-2.0.so.0" in a.missing


def test_assumed_missing_is_empty_by_default(monkeypatch):
    monkeypatch.delenv("LITON_ASSUME_MISSING", raising=False)
    from liton.analysis import _assumed_missing
    assert _assumed_missing() == frozenset()


def test_assumed_missing_parses_a_colon_separated_list(monkeypatch):
    monkeypatch.setenv("LITON_ASSUME_MISSING", "liba.so.1:libb.so.2")
    from liton.analysis import _assumed_missing
    assert _assumed_missing() == frozenset({"liba.so.1", "libb.so.2"})


def test_installed_sdl2_does_not_change_the_fixture_outcome(missing_dep_app, monkeypatch):
    """The point of the fixture: the result is the same whether or not the host
    happens to ship SDL2."""
    monkeypatch.setenv("LITON_ASSUME_MISSING", "libSDL2-2.0.so.0")
    with_sdl2_present = analyze(str(missing_dep_app)).missing

    monkeypatch.delenv("LITON_ASSUME_MISSING")
    without_override = analyze(str(missing_dep_app)).missing

    assert with_sdl2_present == ["libSDL2-2.0.so.0"]
    # Without the override the host decides: either it is absent (bare image) or
    # it resolves (desktop). Both are valid; the fixture forces the first.
    assert without_override in ([], ["libSDL2-2.0.so.0"])
