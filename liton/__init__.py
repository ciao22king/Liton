"""Liton - translate a Linux application once, remember how, run it forever.

The public entry point is the command line::

    python -m liton run ./SomeApp.AppImage

For use as a library, the stable surface is a handful of names, resolved lazily
so that importing :mod:`liton` stays cheap and never pulls in the heavier
modules (analysis, compilation, networking) until something needs them::

    from liton import Engine, analyze, plan_rules

Anything not listed in ``__all__`` is internal and may move between releases.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

__version__ = "0.3.1"

#: Public names, mapped to the module that defines them. No exported name may
#: equal a submodule name: importing a submodule rebinds that attribute on the
#: package, so a collision would silently replace the name we mean to export.
#: That is why the analyser lives in ``liton.analysis`` and not ``liton.analyze``.
_EXPORTS = {
    "Analysis": "liton.models",
    "ElfInfo": "liton.models",
    "LitonError": "liton.models",
    "Profile": "liton.models",
    "Step": "liton.models",
    "StrategyPlan": "liton.models",
    "analyze": "liton.analysis",
    "compile_all": "liton.compiler",
    "make_plan": "liton.planner",
    "plan_rules": "liton.planner",
    "fingerprint": "liton.store",
    "Store": "liton.store",
    "Engine": "liton.runner",
    "verify_profile": "liton.verify",
    "doctor": "liton.verify",
    "choose": "liton.foreign",
    "Translator": "liton.foreign",
    "check_update": "liton.update",
    "apply_update": "liton.update",
}

__all__ = ["__version__", *_EXPORTS]


if TYPE_CHECKING:  # pragma: no cover - for type checkers and IDEs only
    from .analysis import analyze
    from .compiler import compile_all
    from .foreign import Translator, choose
    from .models import Analysis, ElfInfo, LitonError, Profile, Step, StrategyPlan
    from .planner import make_plan, plan_rules
    from .runner import Engine
    from .store import Store, fingerprint
    from .update import apply_update, check_update
    from .verify import doctor, verify_profile


def __getattr__(name: str) -> Any:
    """Resolve a public name on first use (PEP 562)."""
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value  # cache, so later lookups skip the machinery
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
