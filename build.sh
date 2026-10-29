#!/bin/sh
# Liton build and install script.
#
#   ./build.sh              build, test, then install
#   ./build.sh --user       install into the user's home instead of system-wide
#   ./build.sh --system     install system-wide (needs root or sudo)
#   ./build.sh --venv       install into a virtual environment under .venv
#   ./build.sh --prefix DIR install under DIR (a prefix, e.g. /usr/local)
#   ./build.sh --no-test    skip the test suite
#   ./build.sh --check      syntax-check the sources and exit
#   ./build.sh --test       run the tests only
#   ./build.sh --uninstall  remove an installed copy
#   ./build.sh --clean      remove build artefacts
#   ./build.sh --help       show this message
#
# It is plain POSIX sh on purpose: Liton has no build-time dependencies beyond
# Python, and this script should not add any.

set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$HERE"

MODE=auto          # auto | user | system | venv | prefix
PREFIX=
RUN_TEST=auto      # auto | yes | no
ACTION=install     # install | test | check | uninstall | clean
PYTHON=${PYTHON:-}

say()  { printf '%s\n' "$*"; }
info() { printf '[liton] %s\n' "$*"; }
warn() { printf '[liton] warning: %s\n' "$*" >&2; }
die()  { printf '[liton] error: %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; }

# --------------------------------------------------------------------------
# Arguments
# --------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --user)      MODE=user ;;
    --system)    MODE=system ;;
    --venv)      MODE=venv ;;
    --prefix)    shift; [ $# -gt 0 ] || die "--prefix needs a directory"
                 MODE=prefix; PREFIX=$1 ;;
    --prefix=*)  MODE=prefix; PREFIX=${1#--prefix=} ;;
    --no-test)   RUN_TEST=no ;;
    --test)      ACTION=test ;;
    --check)     ACTION=check ;;
    --uninstall) ACTION=uninstall ;;
    --clean)     ACTION=clean ;;
    -h|--help)   usage; exit 0 ;;
    *)           die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

# --------------------------------------------------------------------------
# Find a usable Python 3.10+
# --------------------------------------------------------------------------
find_python() {
  if [ -n "$PYTHON" ]; then
    command -v "$PYTHON" >/dev/null 2>&1 || die "PYTHON=$PYTHON not found"
    say "$PYTHON"; return
  fi
  for cand in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
        say "$cand"; return
      fi
    fi
  done
  die "no Python 3.10+ found; set PYTHON=/path/to/python3"
}

PY=$(find_python)
info "using $PY ($("$PY" -c 'import platform;print(platform.python_version())'))"

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
is_root() { [ "$(id -u)" -eq 0 ]; }

# Run pip install, retrying with --break-system-packages for an externally
# managed environment (PEP 668, common on Debian 12+/Ubuntu 23.04+).
pip_install() {
  if "$PY" -m pip install "$@"; then
    return 0
  fi
  case " $* " in
    *" --break-system-packages "*) return 1 ;;
  esac
  warn "pip refused; retrying with --break-system-packages"
  "$PY" -m pip install --break-system-packages "$@"
}

ensure_pip() {
  "$PY" -m pip --version >/dev/null 2>&1 && return 0
  info "pip is missing; trying to bootstrap it"
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || \
    die "pip is not available for $PY; install python3-pip"
}

run_tests() {
  ensure_pip
  if ! "$PY" -c 'import pytest' >/dev/null 2>&1; then
    info "installing pytest (test-only dependency)"
    pip_install --quiet pytest || die "could not install pytest"
  fi
  info "running the test suite"
  "$PY" -m pytest
}

syntax_check() {
  info "compiling every source file"
  "$PY" -m compileall -q liton tests >/dev/null || die "syntax errors found"
  info "checking the CLI loads"
  PYTHONPATH="$HERE" "$PY" -m liton --version >/dev/null || die "the CLI failed to load"
  say "OK"
}

# --------------------------------------------------------------------------
# Actions that do not install
# --------------------------------------------------------------------------
case "$ACTION" in
  check)     syntax_check; exit 0 ;;
  test)      run_tests; exit 0 ;;
  clean)
    info "removing build artefacts"
    rm -rf build dist .pytest_cache .venv liton.egg-info
    find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
    say "clean"
    exit 0 ;;
  uninstall)
    ensure_pip
    info "uninstalling liton"
    "$PY" -m pip uninstall -y liton || warn "pip reported a problem"
    say "done"
    exit 0 ;;
esac

# --------------------------------------------------------------------------
# Decide where to install
# --------------------------------------------------------------------------
if [ "$MODE" = auto ]; then
  if is_root; then
    MODE=system
  else
    MODE=user
  fi
fi

PIP_FLAGS=""
case "$MODE" in
  system)
    if ! is_root; then
      if command -v sudo >/dev/null 2>&1; then
        info "system install needs root; re-running pip under sudo"
        SUDO=sudo
      else
        die "system install needs root (or use --user)"
      fi
    else
      SUDO=""
    fi
    ;;
  user)
    SUDO=""
    PIP_FLAGS="--user"
    case ":${PATH}:" in
      *":$("$PY" -c 'import site;print(site.USER_BASE)' 2>/dev/null)/bin:"*) ;;
      *) warn "install with --user puts the 'liton' command in your user bin dir;"
         warn "add it to PATH if it is not already there." ;;
    esac
    ;;
  venv)
    SUDO=""
    info "creating a virtual environment in .venv"
    "$PY" -m venv .venv || die "could not create the venv (is python3-venv installed?)"
    PY="$HERE/.venv/bin/python"
    PIP_FLAGS=""
    ;;
  prefix)
    SUDO=""
    PIP_FLAGS="--prefix $PREFIX"
    ;;
esac

# --------------------------------------------------------------------------
# Build and install
# --------------------------------------------------------------------------
if [ "$RUN_TEST" = auto ]; then
  if [ "$ACTION" = install ]; then RUN_TEST=yes; fi
fi
if [ "$RUN_TEST" = yes ]; then
  run_tests
fi

ensure_pip

info "installing liton (${MODE})"
# shellcheck disable=SC2086
if [ -n "${SUDO:-}" ]; then
  $SUDO "$PY" -m pip install $PIP_FLAGS --upgrade . || die "install failed"
else
  pip_install $PIP_FLAGS --upgrade . || die "install failed"
fi

# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------
if [ "$MODE" = venv ]; then
  say ""
  info "installed into .venv"
  say "  activate it with:  . .venv/bin/activate"
  say "  or run it with:    .venv/bin/liton doctor"
else
  if command -v liton >/dev/null 2>&1; then
    say ""
    info "installed: $(command -v liton) (version $("$PY" -m liton --version | awk '{print $2}'))"
  else
    warn "'liton' is installed but not on your PATH"
  fi
fi

say ""
say "next steps:"
say "  liton doctor     report what this machine can translate"
say "  liton run APP    translate an application and run it"
say "  liton update     check for a newer Liton"
