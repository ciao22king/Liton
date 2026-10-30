"""Self-update: version ordering, install detection, and the apply paths.

Every network and install effect is injected, so these tests never touch the
network, never run pip, and never modify the running installation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from liton import update
from liton.update import (
    InstallInfo,
    UpdateInfo,
    apply_update,
    check_update,
    detect_install,
    is_newer,
    is_prerelease,
    latest_version,
    maybe_auto_update,
    parse_version,
)


# ---------------------------------------------------------------------------
# Version ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("0.2.0", (0, 2, 0)),
    ("v0.2.0", (0, 2, 0)),
    ("V1.10.3", (1, 10, 3)),
    ("0.3.0-rc1", (0, 3, 0)),
    ("1.0.0+build.5", (1, 0, 0)),
    ("", (0,)),
    ("nightly", (0,)),
])
def test_parse_version(text, expected):
    assert parse_version(text) == expected


def test_version_ordering_is_numeric_not_lexical():
    # 0.10 is newer than 0.9 even though "0.10" sorts before "0.9" as a string.
    assert is_newer("0.10.0", "0.9.0")
    assert not is_newer("0.9.0", "0.10.0")


def test_is_newer_equal_and_older():
    assert not is_newer("0.3.0", "0.3.0")
    assert not is_newer("0.2.9", "0.3.0")
    assert is_newer("0.3.1", "0.3.0")


def test_release_outranks_its_own_prerelease():
    assert is_newer("0.3.0", "0.3.0-rc1")
    assert not is_newer("0.3.0-rc1", "0.3.0")


@pytest.mark.parametrize("text,expected", [
    ("0.3.0-rc1", True),
    ("0.3.0+build", True),
    ("0.3.0", False),
    ("v0.3.0", False),
])
def test_is_prerelease(text, expected):
    assert is_prerelease(text) is expected


def test_latest_version_ignores_non_semver_tags():
    tags = ["FEX-2101", "nightly", "", "v0.1.0", "v0.10.0", "v0.2.0"]
    assert latest_version(tags) == "v0.10.0"


def test_latest_version_empty():
    assert latest_version([]) == ""
    assert latest_version(["no-versions-here"]) == ""


# ---------------------------------------------------------------------------
# Install detection
# ---------------------------------------------------------------------------

def test_detect_install_finds_a_checkout(tmp_path):
    # Build a fake checkout rather than assuming this test runs inside one: an
    # installed or sdist copy has no .git, which is not a bug in Liton.
    checkout = tmp_path / "src"
    package = checkout / "liton"
    package.mkdir(parents=True)
    (checkout / ".git").mkdir()
    info = detect_install(start=package)
    assert info.kind == "git"
    assert info.root == str(checkout)
    assert "git checkout" in info.describe()


def test_detect_install_this_copy_is_git_or_pip():
    # Whatever the surrounding directory looks like, detection must be coherent.
    info = detect_install()
    assert info.kind in ("git", "pip")
    if info.kind == "git":
        assert info.root and (Path(info.root) / ".git").exists()
    else:
        assert info.spec.startswith("git+https://github.com/")


def test_detect_install_falls_back_to_pip(monkeypatch):
    monkeypatch.setattr(update, "_find_git_root", lambda start, levels=6: "")
    info = detect_install()
    assert info.kind == "pip"
    assert "pip install" in info.describe()
    assert info.spec.startswith("git+https://github.com/")


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------

def _tags(*names):
    return lambda: list(names)


def test_check_update_reports_a_newer_version():
    info = check_update("0.2.0", install=InstallInfo(kind="pip"),
                        tag_fetcher=_tags("v0.2.0", "v0.3.0"),
                        api_fetcher=_tags())
    assert info.available
    assert info.latest == "v0.3.0"
    assert info.source == "tags"
    assert info.url.endswith("/releases/tag/v0.3.0")
    # The pip spec is pinned to the version we are about to install.
    assert info.install.spec.endswith("@v0.3.0")


def test_check_update_says_up_to_date():
    info = check_update("0.3.0", install=InstallInfo(kind="pip"),
                        tag_fetcher=_tags("v0.3.0"), api_fetcher=_tags())
    assert not info.available
    assert "up to date" in info.describe()


def test_check_update_falls_back_to_the_api():
    def boom():
        raise RuntimeError("git not available")
    info = check_update("0.1.0", install=InstallInfo(kind="pip"),
                        tag_fetcher=boom, api_fetcher=_tags("v0.2.0"))
    assert info.available
    assert info.source == "releases"


def test_check_update_reports_an_unreachable_source():
    def boom():
        raise RuntimeError("offline")
    info = check_update("0.1.0", install=InstallInfo(kind="pip"),
                        tag_fetcher=boom, api_fetcher=boom)
    assert not info.available
    assert info.error
    assert "failed" in info.describe()


def test_check_update_git_without_a_located_root_falls_back_to_pip():
    info = check_update("0.1.0", install=InstallInfo(kind="git", root=""),
                        tag_fetcher=_tags("v0.2.0"), api_fetcher=_tags())
    assert info.available
    assert info.install.kind == "pip"
    assert info.install.spec.endswith("@v0.2.0")
    assert any("git checkout not located" in n for n in info.notes)


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def test_apply_update_pulls_a_git_checkout(tmp_path):
    calls = []

    def run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        return 0, "Already up to date.", ""

    info = UpdateInfo(current="0.2.0", latest="0.3.0", available=True,
                      install=InstallInfo(kind="git", root=str(tmp_path)))
    result = apply_update(info, run=run)
    assert result.applied
    assert calls == [["git", "-C", str(tmp_path), "pull", "--ff-only"]]


def test_apply_update_falls_back_to_pip_when_the_pull_fails(tmp_path):
    calls = []

    def run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        if cmd[0] == "git":
            return 1, "", "fatal: not a git repository"
        return 0, "installed", ""

    info = UpdateInfo(current="0.2.0", latest="0.3.0", available=True,
                      install=InstallInfo(kind="git", root=str(tmp_path)))
    result = apply_update(info, run=run)
    assert result.applied
    assert calls[0][0] == "git"
    assert calls[1][:3] == ["python3", "-m", "pip"] or calls[1][0] != "git"
    assert any("git pull failed" in n for n in result.notes)


def test_apply_update_pip_retries_with_user_then_break_system():
    attempts = []

    def run(cmd, cwd=None, timeout=None):
        attempts.append(list(cmd))
        if len(attempts) < 3:
            return 1, "", "error: externally-managed-environment"
        return 0, "ok", ""

    info = UpdateInfo(current="0.2.0", latest="0.3.0", available=True,
                      install=InstallInfo(kind="pip", spec="git+https://x@v0.3.0"))
    result = apply_update(info, run=run)
    assert result.applied
    assert len(attempts) == 3
    assert "--user" in attempts[1]
    assert "--break-system-packages" in attempts[2]


def test_apply_update_gives_up_cleanly():
    def run(cmd, cwd=None, timeout=None):
        return 1, "", "no network"

    info = UpdateInfo(current="0.2.0", latest="0.3.0", available=True,
                      install=InstallInfo(kind="pip", spec="x"))
    result = apply_update(info, run=run)
    assert not result.applied
    assert result.error
    assert result.notes


def test_apply_update_does_nothing_when_already_current():
    def run(cmd, cwd=None, timeout=None):
        raise AssertionError("must not run anything")

    info = UpdateInfo(current="0.3.0", latest="0.3.0", available=False)
    result = apply_update(info, run=run)
    assert not result.applied


def test_apply_update_refuses_an_unknown_install():
    info = UpdateInfo(current="0.2.0", latest="0.3.0", available=True,
                      install=InstallInfo(kind="unknown"))
    result = apply_update(info, run=lambda *a, **k: (0, "", ""))
    assert not result.applied
    assert "by hand" in result.error


# ---------------------------------------------------------------------------
# Automatic updating
# ---------------------------------------------------------------------------

def test_auto_update_is_off_by_default(monkeypatch):
    monkeypatch.delenv(update.AUTO_ENV, raising=False)
    called = []
    result = maybe_auto_update("0.1.0", install=InstallInfo(kind="pip"),
                               tag_fetcher=lambda: called.append(1) or [],
                               run=lambda *a, **k: (0, "", ""))
    assert result is None
    assert called == []


def test_auto_update_is_throttled(monkeypatch):
    monkeypatch.setenv(update.AUTO_ENV, "1")
    stamp = update._stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"checked": 1000.0}))

    called = []

    def tags():
        called.append(1)
        return ["v9.9.9"]

    # One hour after the last check: too soon.
    result = maybe_auto_update("0.1.0", now=1000.0 + 3600, install=InstallInfo(kind="pip"),
                               tag_fetcher=tags, run=lambda *a, **k: (0, "", ""))
    assert result is None
    assert called == []


def test_auto_update_runs_after_the_interval(monkeypatch):
    monkeypatch.setenv(update.AUTO_ENV, "1")
    stamp = update._stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"checked": 0.0}))

    ran = []

    def run(cmd, cwd=None, timeout=None):
        ran.append(cmd)
        return 0, "", ""

    info = maybe_auto_update("0.1.0", now=48 * 3600, install=InstallInfo(kind="pip", spec="x"),
                             tag_fetcher=lambda: ["v0.2.0"], run=run)
    assert info is not None
    assert info.applied
    assert ran


def test_auto_update_stamps_even_when_the_source_fails(monkeypatch):
    monkeypatch.setenv(update.AUTO_ENV, "1")
    stamp = update._stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text(json.dumps({"checked": 0.0}))

    def boom():
        raise RuntimeError("offline")

    maybe_auto_update("0.1.0", now=99_999, install=InstallInfo(kind="pip"),
                      tag_fetcher=boom, api_fetcher=boom, run=lambda *a, **k: (0, "", ""))
    # The stamp moved, so a failing source cannot cause a check on every call.
    assert update._last_check() == 99_999


def test_auto_update_never_raises_on_a_bad_stamp(monkeypatch):
    monkeypatch.setenv(update.AUTO_ENV, "1")
    stamp = update._stamp_path()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.write_text("this is not json")
    assert update._last_check() == 0.0


def test_repo_slug_can_be_overridden(monkeypatch):
    monkeypatch.setenv("LITON_REPO", "someone/fork")
    assert update.repo_slug() == "someone/fork"
    monkeypatch.setenv("LITON_REPO", "   ")
    assert update.repo_slug() == update.DEFAULT_REPO
