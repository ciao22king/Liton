# Changelog

Notable changes to Liton, newest first. This project follows semantic
versioning; the version in `liton/__init__.py` is the one that matters, and the
git tag of the same name marks the release.

## 0.3.1 — 2026-10-30

A test-suite fix that matters to anyone installing from source on a real
machine. No behaviour change for the translation itself.

### Fixed

- **The test suite depended on what the host had installed.** The
  `missing_dep_app` fixture links an application against `libSDL2-2.0.so.0` and
  then deletes it, so the application genuinely lacks it — unless the host ships
  that library, which every desktop Linux does. On such a machine the library
  resolved, nothing was missing, and six tests failed. It passed on a bare CI
  image and failed on a real one, including inside `build.sh`, so `./build.sh`
  could not install for exactly the users Liton targets. The analyser now accepts
  `LITON_ASSUME_MISSING` (or an explicit `known_missing` argument) to treat named
  sonames as absent, and the suite uses it.
- **`test_detect_install_finds_this_checkout` assumed it ran inside a git
  checkout.** An extracted release tarball has no `.git`, so the test failed
  there. It now builds its own fake checkout, and a separate test checks that
  detection is coherent wherever it runs.
- `detect_install()` gained an optional `start` argument so the git branch is
  testable without being inside a repository.

## 0.3.0 — 2026-10-29

The release that makes Liton a thing you install and then keep current.

### Added

- `build.sh` — one script to build, test and install Liton. It finds a Python
  3.10+, copes with an externally-managed environment (PEP 668), and supports
  `--user`, `--system`, `--venv`, `--prefix`, `--test`, `--check`,
  `--uninstall` and `--clean`.
- `liton update` — check for a newer Liton and install it. It knows whether it
  was installed from a git checkout (updates with `git pull --ff-only`) or with
  pip (updates with `pip install --upgrade`), and it reads the remote's tags
  directly, so it needs no API token and cannot be rate-limited.
- Opt-in automatic updating: set `LITON_AUTO_UPDATE=1` and Liton checks at most
  once a day, and only before cheap informational commands (`list`, `show`,
  `doctor`, `home`, `analyze`). It never runs on the `run` path, where an update
  would be a surprise.
- A lazy public API. `from liton import Engine, analyze, StrategyPlan` works, and
  importing the package stays instant because the submodules load on first use.
- `liton/py.typed`, so type checkers see Liton's annotations.
- This changelog.

### Changed

- `liton --version` and the package metadata now agree at 0.3.0.

## 0.2.0 — 2026-10-29

Real cross-architecture translation.

### Added

- `liton/foreign.py` and the `translate` strategy: a foreign-architecture binary
  is now actually run through FEX (x86-64/i386 on aarch64), box64/box86 (on arm
  and riscv64) or QEMU user-mode, chosen by preference and remembered in the
  profile.
- The generated runner pins the translator's cache inside the profile
  (`FEX_APP_CACHE_LOCATION` and friends, or `-L <sysroot>` for QEMU), which is
  what makes the translation happen once rather than every run.
- `--sysroot` and `LITON_SYSROOT`; a foreign binary's libraries resolve against
  the guest sysroot, and the symbol-version check is skipped there.
- `liton doctor` reports the translation backends it found.

### Fixed

- A duplicated `_emit_host` shadowed the translate-aware one, so translate plans
  emitted a plain `exec`.
- `StrategyPlan.translator` was dropped when a profile was saved, because
  `store._plan_to_dict` is a hand-written field list.

## 0.1.0 — 2026-10-28

The first working Liton: ELF analysis in pure Python, a rule-based planner with
an optional LLM, a compiler that emits readable `run.sh` / `run.env` /
`install-deps.sh` / `Dockerfile` artifacts, a fingerprint-keyed profile store,
and `translate` / `run` / `deps` / `analyze` / `list` / `show` / `forget` /
`verify` / `doctor` / `home`.
