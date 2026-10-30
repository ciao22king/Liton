# Liton internals

A tour of the pipeline for anyone reading or extending the code. Each stage is a
module with a single job, and each is a pure function of its inputs wherever
possible, so the stages can be tested in isolation.

```
liton/elf.py       ELF structures (pure python, no execution)
liton/analysis.py   format detection, dependency resolution, capability inference
liton/foreign.py   translation backends: FEX / box64 / QEMU, and sysroots
liton/planner.py   rules (+ optional LLM) -> StrategyPlan
liton/compiler.py  StrategyPlan -> artifacts on disk
liton/store.py     fingerprint-keyed profiles: the memory
liton/runner.py    Engine: translate / remember / run
liton/verify.py    prove a profile resolves and launches
liton/update.py    self-update: version check, install, opt-in auto-update
liton/llm.py       optional OpenAI-compatible client
liton/cli.py       argument parsing and output
```

> The analyser module is `liton/analysis.py`, not `liton/analyze.py`, on purpose.
> The package re-exports the function `analyze`, and an exported name must never
> equal a submodule name: importing a submodule rebinds that attribute on the
> package, so `from liton import analyze` would silently hand back the module
> instead of the function. `tests/test_packaging.py` guards this.

## Stage 1 — Analyse

A result must not depend on what the host happens to have installed. Resolution
is therefore overridable: `LITON_ASSUME_MISSING` (colon-separated sonames) or the
`known_missing` argument to `analyze()` makes the analyser treat those sonames as
absent even when the host provides them. The test suite needs this — the
"missing dependency" fixture links SDL2 and deletes it, but a desktop host ships
`libSDL2-2.0.so.0`, so without the override the missing-dependency path is never
exercised and six tests fail on a real machine while passing on a bare CI image.

`liton/elf.py` reads the file itself rather than shelling out to `readelf` or
`ldd`. Two reasons: `ldd` executes the loader, which is exactly what you must not
do to an untrusted binary; and parsing the bytes makes the whole stage a pure
function that can be cross-checked against the system tools in tests.

It extracts:

* program headers — `PT_INTERP` (the interpreter), `PT_LOAD`, `PT_GNU_STACK`;
* the dynamic table — `DT_NEEDED`, `DT_RPATH`, `DT_RUNPATH`, `DT_SONAME`;
* the dynamic symbol table — undefined symbols, i.e. the imported API surface;
* `DT_VERNEED` / `DT_VERDEF` — the symbol versions required and provided.

The dynamic symbol count is derived from `DT_GNU_HASH` (or the legacy
`DT_HASH`) rather than from section headers, because section headers are optional
and are stripped in some builds. There is a section-header fallback for the rare
case where no hash table is present.

`liton/analysis.py` then classifies the input (`elf`, `appimage`, `pe`, `tar`,
`zip`, `script`, `directory`, `unknown`), resolves every `NEEDED` soname against
the app's own directories and then the host's, and infers capabilities from the
imported symbols (X11, Wayland, OpenGL, ALSA, PulseAudio, DBus, GTK, Qt, …).

### The version check

The interesting part is `_check_symbol_versions`. A binary can have every library
present and still die at startup:

```
./app: /lib/x86_64-linux-gnu/libc.so.6: version `GLIBC_2.38' not found
```

`ldd` says everything is fine, because the file is there. The file simply does
not export the required symbol *version*. Liton compares the `DT_VERNEED`
versions the binary asks for against the `DT_VERDEF` versions the host library
provides, and when something is missing it records a `version_conflicts` entry.
Because installing packages cannot fix a too-old glibc, the planner responds by
choosing a container with a new enough base image.

## Stage 2 — Plan

`plan_rules` is the primary planner and needs no network. It produces a
`StrategyPlan`:

* `strategy` — `native`, `shim`, `container`, `unpack` or `translate`;
* `steps` — typed, ordered instructions (`install_packages`, `unpack`,
  `ld_library_path`, `use_loader`, `use_runtime`, `translate`, `exec`);
* `env`, `packages`, `ld_library_path`, `preload`, `runtime`, `translator`,
  `confidence`.

The rules escalate in a fixed order: a Windows PE goes to a Wine container; a
foreign architecture is *translated* by a real backend (`liton/foreign.py`) and
only falls back to an emulating container when none is installed; a
present-but-too-old library goes to a newer base image; a missing library becomes
a package installation; a bundle's private libraries become an `LD_LIBRARY_PATH`
entry; a bundle's own loader becomes a loader override (which makes the strategy
a `shim`).

### Choosing a translation backend

`foreign.choose` picks by preference for the guest/host pair:

| Backend | Guest → host | Notes |
|---|---|---|
| FEX | x86-64 / i386 → aarch64 | fast; has a JIT code cache |
| box64 / box86 | x86-64 / i386 → arm, riscv64 | low-end ARM |
| QEMU user-mode | anything → anything | universal fallback |

The `probe` argument is injectable, so the decision is unit-tested without any
backend installed. The chosen `Translator` is serialised into the plan, so the
decision is made once and remembered with the rest of the recipe.

For a foreign binary, analysis resolves `NEEDED` against the **guest sysroot**
(`analyze(..., guest_sysroot=...)`), because the host's libraries are the wrong
architecture; symbol-version checks are skipped in that case as they would
compare against the wrong libc. The sysroot is found by `foreign.find_sysroot`
(an explicit `LITON_SYSROOT` wins, then the standard `*-linux-gnu` locations).

`plan_llm` is optional and strictly additive. It asks any OpenAI-compatible model
to refine the plan, validates the reply with `plan_from_dict` (unknown step kinds
are dropped, confidence is clamped, invalid strategies fall back to `native`),
and merges the rule-discovered packages back in. Any failure — no key, no
network, malformed JSON — falls back to the rules silently.

## Stage 3 — Compile

`liton/compiler.py` turns the plan into artifacts that a human can read and a
shell can run:

* `run.env` — the environment (`LD_LIBRARY_PATH`, `LD_PRELOAD`, variables).
* `install-deps.sh` — installs the packages that provide the missing libraries.
* `run.sh` — the entrypoint: unpacks a rootfs if needed, sources `run.env`,
  optionally goes through the app's own loader or a translator, and execs the
  target.
* `Dockerfile` — only for container plans.

Everything is quoted with `shlex.quote`, and the AppImage rootfs is extracted
once and cached inside the profile directory, so the second run skips the
extraction too.

For a `translate` plan the runner pins the backend's caches **inside the
profile**, which is what makes the translation persist rather than repeat. For
FEX that is `FEX_APP_DATA_LOCATION` and `FEX_APP_CACHE_LOCATION` (the JIT code
cache), plus `FEX_ROOTFS` when a guest rootfs was found; a rootfs installed later
is accepted at run time through `LITON_FEX_ROOTFS`. QEMU gets `-L <sysroot>` and
`QEMU_LD_PREFIX`. A missing translator is a clean `exit 127` with a message, not
a confusing "Exec format error" from the kernel.

## Stage 4 — Remember

`liton/store.py` is the memory. A profile's key is a fingerprint of the exact
build: absolute path, size, nanosecond mtime and architecture. Nothing is read
from the file's contents, so finding a profile costs a `stat` and a hash of a
short string — 0.08 ms against 12 ms for a full analysis. The consequences are
the whole point of the project:

* the same build is translated **once** and then only looked up, with no
  analysis on the remembered path;
* a rebuilt binary has a different fingerprint, so it is translated afresh and
  the new recipe is remembered alongside the old one;
* a profile whose schema is older than the running Liton is treated as absent, so
  it gets recompiled rather than misread;
* a corrupt profile is treated as absent, never as a crash;
* when a build is replaced, the superseded profile for that path is pruned, so
  the store does not grow without bound.

Writes are atomic (`os.replace` of a temp file), so an interrupted save cannot
leave a half-written profile.

## Stage 5 — Run

`liton/runner.py` implements `Engine.run`: fingerprint, look up, execute, and
record the outcome (`runs`, `last_result`) back into the profile. If there is no
profile for the build, it translates first — so `liton run` is always correct,
and `liton translate` exists only to do the work ahead of time.

## Stage 6 — Update

`liton/update.py` keeps an installed Liton current. It first works out *how*
this copy was installed — a git checkout (walk up for `.git`) updates with
`git pull --ff-only`, anything else updates with `pip install --upgrade`. It
reads the remote's tags with `git ls-remote` and falls back to the GitHub tags
API, so no token is needed and there is no rate limit. Versions are compared
numerically, so `0.10.0` is newer than `0.9.0`.

Automatic updating is **opt-in** (`LITON_AUTO_UPDATE=1`) and throttled to once a
day, and only ever runs before cheap informational commands (`list`, `show`,
`doctor`, `home`, `analyze`) — never on the `run` path, where an update appearing
mid-launch would be a surprise. The throttle stamp is written *before* the
network work, so a failing source cannot cause a check on every invocation.

Every external effect (the network fetch, the command runner) is an injectable
callable, which is why the whole module is tested without a network or a pip.

## Verification

`liton/verify.py` checks three things, cheapest first: that the artifacts exist,
that the recorded libraries still resolve on this host, and that the dynamic
loader can resolve the target (via `ld.so --list`, which does not run the
program's code). With `--execute` it goes further and actually launches the
program, treating "still running after the timeout" as success for a GUI app.

`liton doctor` additionally reports the translation backends it found (`fex`,
`box64`, `box86`), the QEMU user-mode binaries present, and any FEX rootfs.

## Testing

`tests/conftest.py` builds real C binaries with the system compiler: a plain
executable, one that ships a private library behind an `$ORIGIN` runpath, one
linked against a library that is then deleted, an AppImage-like file, a PE stub
and a tarball. The suite drives the full pipeline over these, and cross-checks
the ELF parser against `readelf` for the `NEEDED` list, the undefined symbols and
both version tables.
