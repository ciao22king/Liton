"""Data models shared by the analyser, planner, compiler and store."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any


SCHEMA_VERSION = 1


class LitonError(Exception):
    """Base error for all Liton failures."""


@dataclass
class ElfInfo:
    """Result of parsing an ELF file."""

    path: str
    valid: bool = False
    elf_class: int = 0          # 1 = ELF32, 2 = ELF64
    little_endian: bool = True
    machine: str = ""           # normalised, e.g. "x86-64"
    e_type: str = ""            # "EXEC", "DYN", "CORE"
    interpreter: str = ""       # PT_INTERP, e.g. /lib64/ld-linux-x86-64.so.2
    needed: list[str] = field(default_factory=list)
    rpath: list[str] = field(default_factory=list)
    runpath: list[str] = field(default_factory=list)
    soname: str = ""
    is_pie: bool = False
    has_exec_stack: bool = False
    error: str = ""


@dataclass
class ImportSurface:
    """Grouped dynamic symbols an application imports."""

    libraries: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    groups: dict[str, list[str]] = field(default_factory=dict)

    def summary(self) -> list[str]:
        return sorted(g for g in self.groups if self.groups[g])


@dataclass
class LibResolution:
    soname: str
    resolved: bool = False
    path: str = ""
    provider: str = ""       # Debian package providing it, when known
    source: str = ""         # "host" | "profile" | "apt"


@dataclass
class Analysis:
    """Everything Liton learned about an application, before planning."""

    path: str = ""
    format: str = "unknown"      # elf | appimage | directory | tar | pe | script | unknown
    size: int = 0
    arch: str = ""
    elf: ElfInfo | None = None
    imports: ImportSurface | None = None
    resolutions: list[LibResolution] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    version_conflicts: list[dict[str, Any]] = field(default_factory=list)
    package_hints: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def resolved_count(self) -> int:
        return sum(1 for r in self.resolutions if r.resolved)


@dataclass
class Step:
    """A single, typed instruction the compiler knows how to emit."""

    kind: str
    detail: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    reason: str = ""


@dataclass
class StrategyPlan:
    """The translated, reusable recipe for running an application."""

    strategy: str = "native"       # native | shim | container | unpack
    steps: list[Step] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    packages: list[str] = field(default_factory=list)
    ld_library_path: list[str] = field(default_factory=list)
    preload: list[str] = field(default_factory=list)
    runtime: str = "host"          # host | docker | podman
    source: str = "rules"          # rules | llm
    confidence: float = 0.0
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"strategy={self.strategy} runtime={self.runtime} confidence={self.confidence:.2f}"]
        for s in self.steps:
            lines.append(f"  - {s.kind}: {s.detail}")
        return "\n".join(lines)


@dataclass
class Profile:
    """A compiled, remembered translation for one exact application build."""

    key: str = ""
    name: str = ""
    fingerprint: str = ""
    source_path: str = ""
    source_hash: str = ""
    schema: int = SCHEMA_VERSION
    created: str = ""
    updated: str = ""
    runs: int = 0
    analysis: dict[str, Any] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    last_result: dict[str, Any] = field(default_factory=dict)
    verify: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), indent=2, sort_keys=False)

    @classmethod
    def from_json(cls, text: str) -> "Profile":
        data = json.loads(text)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
