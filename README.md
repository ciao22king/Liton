# Liton

**Translate a Linux application once. Remember how. Run it forever.**

Liton is a compatibility layer for Linux that works the other way round from
Wine. Wine translates *calls* at runtime, every time the program runs. Liton
translates the *application* once — it reads the binary, works out everything
that build needs in order to start on this machine, compiles that knowledge into
a recipe, and stores the recipe in a profile keyed by the exact build. From then
on, starting the app is a hash and a lookup: no re-analysis, no re-planning, no
network.

```console
$ liton run ./SomeApp-1.4.2-x86_64.AppImage
[liton] info:  no remembered profile for this build; translating now
[liton] info:  analysing .../SomeApp-1.4.2-x86_64.AppImage (appimage, x86-64)
[liton] info:  needs packages: libgtk-3-0, libasound2
[liton] info:  translated -> profile 9f2c...c41 (4 artifacts)
...the application starts...

$ liton run ./SomeApp-1.4.2-x86_64.AppImage
[liton] info:  using remembered profile 9f2c...c41 (translated 2026-10-28, 1 previous runs)
...the application starts, immediately...
```

## Why this is different

| | Wine | Liton |
|---|---|---|
| What is translated | Windows API calls | the whole application's Linux requirements |
| When | at every run, in a runtime shim | once, ahead of time |
| What is remembered | nothing per application | a compiled profile per exact build |
| Cost of the second run | the same as the first | one hash and a file lookup |

The point is the remembering. A machine that has translated an app once starts
it as fast as a native binary, even offline, even after a reboot.

## How it works

```
   analyse  ──►  plan  ──►  compile  ──►  remember  ──►  run
   (static)     (recipe)    (artifacts)    (profile)     (fast path)
```

1. **Analyse** — Liton parses the ELF file itself, in pure Python. It reads the
   program headers, the `DT_NEEDED` list, the interpreter, `RPATH`/`RUNPATH`, the
   undefined dynamic symbols and the required *symbol versions* (`GLIBC_2.38` and
   friends). Nothing is executed, so analysing an untrusted binary is safe.
2. **Plan** — a rule engine turns that analysis into a strategy (`native`,
   `shim`, `container`, `unpack`) and a list of typed steps: install these
   packages, prepend this `LD_LIBRARY_PATH`, use the app's own loader, unpack the
   AppImage, run it in a container because the host's libraries are too old.
   An optional LLM can refine the plan; Liton works fully offline without one.
3. **Compile** — the plan becomes plain, inspectable artifacts: `run.sh`,
   `run.env`, `install-deps.sh` and, when needed, a `Dockerfile`.
4. **Remember** — everything is stored under a profile keyed by a fingerprint of
   the exact build (path + size + content hash + architecture).
5. **Run** — `liton run` hashes the binary, finds the profile, and executes it.

## What it catches

* **Missing libraries** — every `NEEDED` entry that is not on the host, mapped to
  the Debian package that provides it (`libSDL2-2.0.so.0` → `libsdl2-2.0-0`).
* **Private libraries** — apps that ship their own `.so` files next to the binary
  or via `$ORIGIN` runpaths; Liton records the directory in `LD_LIBRARY_PATH`.
* **Libraries that are present but too old** — the failure that looks like
  `version 'GLIBC_2.38' not found` even though `ldd` reports every library as
  found. Liton compares the required symbol versions against the ones the host
  library actually exports and escalates to a container with a new enough base
  image when they do not match.
* **Foreign architectures** — an `aarch64` binary on an `x86-64` host, which
  needs emulation.
* **AppImages, tarballs, Windows `.exe`, scripts, directories** — each gets the
  right treatment instead of a wrong one.
* **Capabilities** — X11, Wayland, OpenGL, Vulkan, ALSA, PulseAudio, PipeWire,
  DBus, GTK, Qt, and more, detected from the imported symbols, so the environment
  and container device mounts are set up correctly.

## Install

Liton is pure Python 3 (3.10+) with **no third-party dependencies**.

```console
$ pip install .
# or run it straight from a checkout:
$ python3 -m liton doctor
```

## Usage

```console
liton translate <path>      analyse, compile and remember a recipe
liton run <path> [args...]  run using the remembered recipe (translates if needed)
liton deps <path>           show (or --install) the dependencies a profile needs
liton analyze <path>        print the static analysis as JSON
liton list                  list remembered profiles
liton show <key|path>       print one remembered profile
liton forget <key|path>     delete a remembered profile
liton verify <key|path>     verify a profile resolves and can launch
liton doctor                report host capabilities
liton home                  print the profile store location
```

Useful flags: `--llm` (let an LLM refine the plan), `--force` (re-translate),
`--verify` / `--execute-verify` (check before launching).

### The profile store

Profiles live under `$XDG_DATA_HOME/liton` (default `~/.local/share/liton`), or
`$LITON_HOME` if set. Each profile is a directory containing `profile.json` and
the compiled artifacts. Deleting a profile is `liton forget`.

## Optional LLM planner

Liton's rules are the primary planner and always run offline. If you set
`LITON_LLM_API_KEY` (or `OPENAI_API_KEY`) and pass `--llm`, Liton will ask any
OpenAI-compatible endpoint to refine the recipe. The model's output is validated
against a strict schema and merged with the rule-based result; if anything fails,
Liton silently falls back to the rules. The model is never trusted blindly and is
never required.

```console
$ export LITON_LLM_API_KEY=...        # optional
$ export LITON_LLM_BASE_URL=https://api.openai.com/v1
$ export LITON_LLM_MODEL=gpt-4o-mini
$ liton translate --llm ./app
```

## Scope and honesty

Liton makes a binary *start correctly* on a Linux host it was not built for. It
does not recompile machine code for another architecture, and it does not
translate Windows API calls. Where a translation genuinely needs a different
userspace, Liton says so and compiles a container recipe rather than pretending.
The strategy it chose and its confidence are always recorded in the profile, and
`liton verify` reports what it could and could not confirm.

## Development

```console
$ python3 -m pytest
```

The test suite builds real C binaries with the system compiler and exercises the
whole pipeline on them, and cross-checks the ELF parser against `readelf` on the
system's own executables.

## License

Apache-2.0. See `LICENSE`.
