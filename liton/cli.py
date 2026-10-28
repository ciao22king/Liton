"""The ``liton`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .analyze import analyze
from .models import LitonError
from .planner import make_plan
from .runner import Engine
from .store import Store, fingerprint, plan_from_dict
from .util import Log, data_home
from .verify import doctor, verify_profile


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="liton",
        description="Translate a Linux application once, remember how, run it forever.",
    )
    p.add_argument("--version", action="version", version=f"liton {__version__}")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("-q", "--quiet", action="store_true", help="errors only")

    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("translate", help="analyse, compile and remember a recipe")
    t.add_argument("path")
    t.add_argument("--llm", action="store_true", help="let an LLM refine the plan")
    t.add_argument("--force", action="store_true", help="re-translate even if remembered")

    d = sub.add_parser("deps", help="show or install the dependencies a profile needs")
    d.add_argument("path")
    d.add_argument("--install", action="store_true", help="run the generated installer")
    d.add_argument("--llm", action="store_true", help="let an LLM refine the plan")

    r = sub.add_parser("run", help="run using the remembered recipe (translate if needed)")
    r.add_argument("path")
    r.add_argument("args", nargs=argparse.REMAINDER, help="arguments for the application")
    r.add_argument("--llm", action="store_true", help="allow LLM planning if not yet translated")
    r.add_argument("--verify", action="store_true", help="verify before launching")
    r.add_argument("--execute-verify", action="store_true",
                   help="with --verify, actually launch the app as a check")

    a = sub.add_parser("analyze", help="print a static analysis as JSON")
    a.add_argument("path")
    a.add_argument("--llm", action="store_true", help="also produce an LLM plan")

    s = sub.add_parser("list", help="list remembered profiles")
    s.add_argument("--json", action="store_true")

    sh = sub.add_parser("show", help="show one remembered profile")
    sh.add_argument("key")

    f = sub.add_parser("forget", help="delete a remembered profile")
    f.add_argument("key")
    f.add_argument("--keep-artifacts", action="store_true")

    v = sub.add_parser("verify", help="verify a remembered profile")
    v.add_argument("key")
    v.add_argument("--execute", action="store_true", help="also launch it")

    sub.add_parser("doctor", help="report host capabilities")
    sub.add_parser("home", help="print the profile store location")
    return p


def _log(args) -> Log:
    level = "debug" if args.verbose else ("error" if args.quiet else "info")
    return Log(level)


def _load_profile(store: Store, ref: str):
    """Accept either a profile key or a path to the application."""
    profile = store.load(ref)
    if profile:
        return profile
    ap = os.path.abspath(ref)
    if os.path.exists(ap):
        from .analyze import analyze as _analyze

        key = fingerprint(ap, _analyze(ap, log=Log("error")))
        return store.load(key)
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = _log(args)
    engine = Engine(log=log)
    store = engine.store

    try:
        if args.command == "translate":
            profile = engine.translate(args.path, use_llm=args.llm, force=args.force)
            print(f"profile {profile.key}")
            print(f"strategy {profile.plan.get('strategy')} "
                  f"(confidence {profile.plan.get('confidence')})")
            if profile.plan.get("packages"):
                print("packages " + ", ".join(profile.plan["packages"]))
            print(f"artifacts in {store.profile_dir(profile.key)}")
            return 0

        if args.command == "run":
            return engine.run(args.path, args=args.args, use_llm=args.llm,
                              verify=args.verify, execute_verify=args.execute_verify)

        if args.command == "deps":
            profile = engine.translate(args.path, use_llm=args.llm)
            plan = engine.plan_for(profile)
            if not plan.packages:
                print("no extra packages required")
                return 0
            print("packages: " + ", ".join(plan.packages))
            for soname in (profile.analysis or {}).get("missing", []):
                print(f"  {soname} -> {plan_pkg(profile, soname)}")
            if args.install:
                installer = store.profile_dir(profile.key) / "install-deps.sh"
                print(f"running {installer}")
                import subprocess

                return subprocess.call(["/bin/sh", str(installer)])
            return 0

        if args.command == "analyze":
            analysis = analyze(args.path, log=log)
            payload = _analysis_json(analysis)
            if args.llm:
                plan = make_plan(analysis, use_llm=True, log=log)
                payload["plan"] = plan.__dict__
            print(json.dumps(payload, indent=2, default=str))
            return 0

        if args.command == "list":
            profiles = store.all()
            if args.json:
                print(json.dumps([p.__dict__ for p in profiles], indent=2, default=str))
                return 0
            if not profiles:
                print("no profiles yet")
                return 0
            for p in profiles:
                strat = p.plan.get("strategy", "?")
                print(f"{p.key}  {p.name:<24} {strat:<9} runs={p.runs:<3} {p.source_path}")
            return 0

        if args.command == "show":
            profile = _load_profile(store, args.key)
            if not profile:
                log.error(f"no profile for {args.key!r}")
                return 1
            print(profile.to_json())
            return 0

        if args.command == "forget":
            profile = _load_profile(store, args.key)
            key = profile.key if profile else args.key
            if not store.delete(key, remove_artifacts=not args.keep_artifacts):
                log.error(f"no profile for {args.key!r}")
                return 1
            print(f"forgot {key}")
            return 0

        if args.command == "verify":
            profile = _load_profile(store, args.key)
            if not profile:
                log.error(f"no profile for {args.key!r}")
                return 1
            result = verify_profile(profile, execute=args.execute, log=log)
            print(f"profile {profile.key} ({profile.name})")
            print(result.render())
            profile.verify = result.to_dict()
            store.save(profile)
            return 0 if result.ok else 1

        if args.command == "doctor":
            info = doctor()
            print(json.dumps(info, indent=2))
            return 0

        if args.command == "home":
            print(data_home())
            return 0

    except LitonError as exc:
        log.error(str(exc))
        return 2
    except FileNotFoundError as exc:
        log.error(f"not found: {exc}")
        return 2
    except KeyboardInterrupt:
        return 130

    return 0


def plan_pkg(profile, soname: str) -> str:
    """Look up which package a missing soname maps to in a profile."""
    hints = (profile.analysis or {}).get("package_hints", {})
    return hints.get(soname, "(unknown)")


def _analysis_json(a) -> dict:
    return {
        "path": a.path,
        "format": a.format,
        "arch": a.arch,
        "size": a.size,
        "missing": a.missing,
        "version_conflicts": a.version_conflicts,
        "package_hints": a.package_hints,
        "notes": a.notes,
        "elf": a.elf.__dict__ if a.elf else None,
        "capabilities": a.imports.summary() if a.imports else [],
        "imported_symbols": len(a.imports.symbols) if a.imports else 0,
        "resolved": [r.__dict__ for r in a.resolutions],
    }


if __name__ == "__main__":
    sys.exit(main())
