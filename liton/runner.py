"""High-level engine: translate once, remember, then run from memory."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import elf as elfmod
from .analyze import analyze
from .compiler import compile_all
from .models import Analysis, Profile, StrategyPlan
from .planner import make_plan
from .store import Store, fingerprint, make_profile, plan_from_dict, record_run
from .util import Log
from .verify import verify_profile


class Engine:
    def __init__(self, store: Store | None = None, log: Log | None = None):
        self.store = store or Store()
        self.log = log or Log("info")

    # -- translation -------------------------------------------------------
    def translate(self, path: str, use_llm: bool = False, force: bool = False,
                  client=None) -> Profile:
        """Analyse, plan, compile and remember *path*.  Idempotent."""
        ap = os.path.abspath(path)
        if not os.path.exists(ap):
            raise FileNotFoundError(ap)

        analysis = analyze(ap, log=self.log)
        key = fingerprint(ap, analysis)

        if not force:
            existing = self.store.load(key)
            if existing:
                self.log.info(f"already translated (profile {key}); reusing")
                return existing

        self.log.info(f"analysing {ap} ({analysis.format}, {analysis.arch or 'n/a'})")
        plan = make_plan(analysis, use_llm=use_llm, log=self.log, client=client)

        if plan.packages:
            self.log.info("needs packages: " + ", ".join(plan.packages))
        if analysis.missing:
            self.log.warn("missing libraries: " + ", ".join(analysis.missing))

        directory = self.store.profile_dir(key)
        artifacts = compile_all(plan, analysis, directory)
        profile = make_profile(analysis, plan, artifacts, key=key)
        self.store.save(profile)
        self._prune_superseded(profile)
        self.log.info(f"translated -> profile {key} ({len(artifacts)} artifacts)")
        return profile

    def _prune_superseded(self, keep: Profile) -> None:
        """Drop profiles for older builds of the same path.

        The application at this path has been replaced, so its previous recipes
        can never be reached again; keeping them would grow the store without
        bound.
        """
        for other in self.store.all():
            if other.key != keep.key and other.source_path == keep.source_path:
                self.store.delete(other.key)
                self.log.debug(f"pruned superseded profile {other.key}")

    # -- execution ---------------------------------------------------------
    def run(self, path: str, args: list[str] | None = None, use_llm: bool = False,
            verify: bool = False, execute_verify: bool = False) -> int:
        """Run *path* using its remembered profile, translating only if needed.

        The remembered path is deliberately cheap: the fingerprint is derived
        from the file's size, mtime and ELF machine type, so no analysis is done
        when a profile already exists.
        """
        args = list(args or [])
        ap = os.path.abspath(path)
        if not os.path.exists(ap):
            self.log.error(f"no such file: {ap}")
            return 1

        key = fingerprint(ap, arch=elfmod.quick_arch(ap))
        profile = self.store.load(key)

        if profile is None:
            self.log.info("no remembered profile for this build; translating now")
            profile = self.translate(ap, use_llm=use_llm)
        else:
            self.log.info(f"using remembered profile {key} "
                          f"(translated {profile.updated}, {profile.runs} previous runs)")

        if verify:
            result = verify_profile(profile, execute=execute_verify, log=self.log)
            print(result.render())
            if not result.ok:
                record_run(profile, False, "verification failed")
                self.store.save(profile)
                return 1

        rc = self._exec_profile(profile, args)
        record_run(profile, rc == 0, f"exit {rc}")
        self.store.save(profile)
        return rc

    def _exec_profile(self, profile: Profile, args: list[str]) -> int:
        runner = self._runner_path(profile)
        if runner is None:
            self.log.error("profile has no runner artifact; re-run `liton translate --force`")
            return 1
        env = dict(os.environ)
        env.setdefault("LITON_PROFILE", profile.key)
        self.log.debug(f"exec {runner} {args}")
        try:
            proc = subprocess.run(["/bin/sh", str(runner), *args], env=env)
        except KeyboardInterrupt:
            return 130
        except OSError as exc:
            self.log.error(f"cannot launch runner: {exc}")
            return 1
        return proc.returncode

    @staticmethod
    def _runner_path(profile: Profile) -> Path | None:
        for art in profile.artifacts:
            if art.endswith("run.sh"):
                return Path(art)
        if profile.artifacts:
            cand = Path(profile.artifacts[0]).parent / "run.sh"
            if cand.exists():
                return cand
        return None

    # -- introspection -----------------------------------------------------
    def plan_for(self, profile: Profile) -> StrategyPlan:
        return plan_from_dict(profile.plan or {})
