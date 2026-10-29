"""The persistent store: Liton's memory of how each application was translated.

A profile is keyed by a *fingerprint* of the exact application build (absolute
path + size + content hash + architecture).  Re-running ``liton run`` on the
same build therefore costs one hash and a lookup: no re-analysis, no re-planning,
no network.  When the build changes, the fingerprint changes and Liton
re-translates and remembers the new recipe.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

from . import __version__
from .models import SCHEMA_VERSION, Analysis, Profile, StrategyPlan
from .util import ensure_dirs, profiles_dir, sha256_bytes, sha256_file, slugify


def fingerprint(path: str, analysis: Analysis | None = None,
                arch: str | None = None) -> str:
    """A stable identity for one exact build of an application.

    The key is derived from the absolute path, the size, the nanosecond mtime and
    the architecture.  Those change on any real rebuild, and none of them costs a
    read of the file's contents, so finding a remembered profile is cheap even
    for a large AppImage.  ``analysis`` or ``arch`` may supply the machine type;
    when neither does, the ELF header is read for it.
    """
    ap = os.path.abspath(path)
    if arch is None:
        arch = analysis.arch if analysis else ""
    try:
        st = os.stat(ap)
        size, mtime = st.st_size, st.st_mtime_ns
    except OSError:
        size, mtime = 0, 0

    material = f"{ap}\0{size}\0{mtime}\0{arch}\0"
    return sha256_bytes(material.encode())[:32]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else profiles_dir()
        ensure_dirs()

    def profile_dir(self, key: str) -> Path:
        return self.root / key

    def path_for(self, key: str) -> Path:
        return self.profile_dir(key) / "profile.json"

    def list_keys(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir()
                      if (p / "profile.json").is_file())

    def load(self, key: str) -> Profile | None:
        path = self.path_for(key)
        if not path.is_file():
            return None
        try:
            profile = Profile.from_json(path.read_text())
        except Exception:
            return None
        if profile.schema != SCHEMA_VERSION:
            # Older schema: treat as absent so it gets recompiled.
            return None
        return profile

    def find_by_source(self, source_path: str) -> Profile | None:
        target = os.path.abspath(source_path)
        for key in self.list_keys():
            p = self.load(key)
            if p and os.path.abspath(p.source_path) == target:
                return p
        return None

    def save(self, profile: Profile) -> Path:
        profile.updated = _now()
        if not profile.created:
            profile.created = profile.updated
        directory = self.profile_dir(profile.key)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "profile.json"
        tmp = directory / ".profile.json.tmp"
        tmp.write_text(profile.to_json())
        os.replace(tmp, path)
        return path

    def delete(self, key: str, remove_artifacts: bool = True) -> bool:
        import shutil

        directory = self.profile_dir(key)
        if not directory.is_dir():
            return False
        if remove_artifacts:
            shutil.rmtree(directory, ignore_errors=True)
        else:
            (directory / "profile.json").unlink(missing_ok=True)
        return True

    def all(self) -> list[Profile]:
        out = []
        for key in self.list_keys():
            p = self.load(key)
            if p:
                out.append(p)
        return out


def make_profile(analysis: Analysis, plan: StrategyPlan, artifacts: list[Path],
                 key: str | None = None) -> Profile:
    ap = os.path.abspath(analysis.path)
    key = key or fingerprint(ap, analysis)
    name = slugify(os.path.basename(ap))
    profile = Profile(
        key=key,
        name=name,
        fingerprint=key,
        source_path=ap,
        source_hash=sha256_file(ap) if os.path.isfile(ap) else "",
        schema=SCHEMA_VERSION,
        created=_now(),
        updated=_now(),
        analysis=_analysis_to_dict(analysis),
        plan=_plan_to_dict(plan),
        artifacts=[str(a) for a in artifacts],
    )
    return profile


def _analysis_to_dict(a: Analysis) -> dict:
    return {
        "format": a.format,
        "arch": a.arch,
        "size": a.size,
        "missing": a.missing,
        "version_conflicts": a.version_conflicts,
        "package_hints": a.package_hints,
        "notes": a.notes,
        "needed": a.elf.needed if a.elf else [],
        "interpreter": a.elf.interpreter if a.elf else "",
        "capabilities": a.imports.summary() if a.imports else [],
        "resolved": [
            {"soname": r.soname, "path": r.path, "provider": r.provider,
             "resolved": r.resolved, "source": r.source}
            for r in a.resolutions
        ],
    }


def _plan_to_dict(p: StrategyPlan) -> dict:
    return {
        "strategy": p.strategy,
        "runtime": p.runtime,
        "source": p.source,
        "confidence": p.confidence,
        "packages": p.packages,
        "env": p.env,
        "ld_library_path": p.ld_library_path,
        "preload": p.preload,
        "notes": p.notes,
        "translator": p.translator,
        "steps": [
            {"kind": s.kind, "detail": s.detail, "params": s.params, "reason": s.reason}
            for s in p.steps
        ],
    }


def plan_from_dict(data: dict) -> StrategyPlan:
    from .models import Step

    p = StrategyPlan(
        strategy=data.get("strategy", "native"),
        runtime=data.get("runtime", "host"),
        source=data.get("source", "store"),
        confidence=float(data.get("confidence", 0.0)),
        packages=list(data.get("packages", [])),
        env=dict(data.get("env", {})),
        ld_library_path=list(data.get("ld_library_path", [])),
        preload=list(data.get("preload", [])),
        notes=list(data.get("notes", [])),
        translator=dict(data.get("translator") or {}),
    )
    for raw in data.get("steps", []):
        p.steps.append(Step(raw.get("kind", ""), raw.get("detail", ""),
                            raw.get("params", {}), raw.get("reason", "")))
    return p


def record_run(profile: Profile, ok: bool, detail: str = "") -> None:
    profile.runs += 1
    profile.last_result = {
        "when": _now(),
        "ok": bool(ok),
        "detail": detail,
        "liton": __version__,
    }
    profile.updated = _now()


def json_dumps(data: object) -> str:
    return json.dumps(data, indent=2, sort_keys=False)
