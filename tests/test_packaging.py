"""Packaging surface: the public API facade and the build script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import liton

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build.sh"


def test_public_names_resolve():
    for name in liton.__all__:
        assert getattr(liton, name) is not None


def test_facade_is_lazy_then_cached():
    # A fresh interpreter has not imported the heavy submodules yet.
    code = (
        "import sys, liton\n"
        "assert 'liton.analysis' not in sys.modules\n"
        "liton.analyze\n"
        "assert 'liton.analysis' in sys.modules\n"
        "print('ok')\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_no_export_shares_a_name_with_a_submodule():
    """An exported name must never equal a submodule name.

    Importing a submodule rebinds that attribute on the package, so a collision
    silently replaces the function we meant to export with the module. This
    caught `analyze` (function) colliding with `liton.analyze` (module).
    """
    import inspect

    package_dir = ROOT / "liton"
    submodules = {p.stem for p in package_dir.glob("*.py")}
    for name in liton.__all__:
        if name == "__version__":
            continue
        assert name not in submodules, (
            f"{name!r} is both a public export and a submodule name")
        assert not inspect.ismodule(getattr(liton, name)), (
            f"liton.{name} resolved to a module, not the intended object")


def test_analyze_import_order_cannot_shadow_the_function():
    """`from liton import Engine, analyze` must give the function, in any order."""
    for order in (
        "from liton import Engine, analyze",
        "from liton import analyze, Engine",
        "import liton.runner\nfrom liton import analyze",
        "from liton import analyze",
    ):
        code = order + "\nassert callable(analyze), type(analyze)\nprint('ok')"
        proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT),
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{order!r} -> {proc.stderr}"
        assert proc.stdout.strip() == "ok"


def test_unknown_attribute_raises():
    with pytest.raises(AttributeError):
        liton.does_not_exist


def test_dir_lists_only_the_public_surface():
    names = dir(liton)
    assert "__version__" in names
    assert set(liton.__all__) <= set(names)
    assert not any(n.startswith("_") and n != "__version__" for n in names)


def test_version_is_a_release_number():
    parts = liton.__version__.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)


def test_pyproject_and_package_agree_on_the_version():
    text = (ROOT / "pyproject.toml").read_text()
    assert f'version = "{liton.__version__}"' in text


def test_py_typed_marker_is_present_and_packaged():
    assert (ROOT / "liton" / "py.typed").exists()
    assert 'liton = ["py.typed"]' in (ROOT / "pyproject.toml").read_text()


@pytest.mark.skipif(not BUILD.exists(), reason="build.sh is missing")
def test_build_script_is_valid_posix_shell():
    proc = subprocess.run(["/bin/sh", "-n", str(BUILD)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(not BUILD.exists(), reason="build.sh is missing")
def test_build_script_help_and_check_run():
    for flag in ("--help", "--check"):
        proc = subprocess.run(["/bin/sh", str(BUILD), flag], cwd=str(ROOT),
                              capture_output=True, text=True)
        assert proc.returncode == 0, f"{flag}: {proc.stderr}"
    assert "install system-wide" in subprocess.run(
        ["/bin/sh", str(BUILD), "--help"], cwd=str(ROOT),
        capture_output=True, text=True).stdout


@pytest.mark.skipif(not BUILD.exists(), reason="build.sh is missing")
def test_build_script_rejects_an_unknown_flag():
    proc = subprocess.run(["/bin/sh", str(BUILD), "--nonsense"], cwd=str(ROOT),
                          capture_output=True, text=True)
    assert proc.returncode != 0
    assert "unknown option" in proc.stderr
