"""Planner tests: rule engine, LLM validation, and graceful fallback."""

from __future__ import annotations

import pytest

from liton.analyze import analyze
from liton.llm import LLMClient, LLMUnavailable
from liton.planner import (make_plan, plan_from_dict, plan_llm, plan_rules)


def kinds(plan):
    return [s.kind for s in plan.steps]


def test_native_plan_for_plain_binary(simple_app):
    plan = plan_rules(analyze(str(simple_app)))
    assert plan.strategy == "native"
    assert plan.runtime == "host"
    assert "exec" in kinds(plan)
    assert plan.packages == []
    assert plan.confidence >= 0.8


def test_plan_adds_ld_library_path_for_bundle(bundled_app):
    plan = plan_rules(analyze(str(bundled_app)))
    assert "ld_library_path" in kinds(plan)
    assert any(d.endswith("lib") for d in plan.ld_library_path)


def test_plan_requests_packages_for_missing_dep(missing_dep_app):
    plan = plan_rules(analyze(str(missing_dep_app)))
    assert "install_packages" in kinds(plan)
    assert "libsdl2-2.0-0" in plan.packages


def test_plan_for_appimage_unpacks(appimage_like):
    plan = plan_rules(analyze(str(appimage_like)))
    assert plan.strategy == "unpack"
    assert "unpack" in kinds(plan)
    assert any(s.params.get("tool") == "appimage-extract" for s in plan.steps)


def test_plan_for_pe_uses_container(pe_like):
    plan = plan_rules(analyze(str(pe_like)))
    assert plan.strategy == "container"
    assert plan.runtime == "docker"


def test_plan_for_script_runs_directly(tmp_path):
    f = tmp_path / "s.sh"
    f.write_text("#!/bin/sh\ntrue\n")
    plan = plan_rules(analyze(str(f)))
    assert plan.strategy == "native"
    assert kinds(plan) == ["exec"]


def test_plan_from_dict_validates_and_filters():
    raw = {
        "strategy": "shim",
        "runtime": "podman",
        "packages": ["libfoo1", 42],
        "env": {"FOO": "bar", "NUM": 3},
        "ld_library_path": ["/opt/x/lib"],
        "steps": [
            {"kind": "install_packages", "detail": "libfoo1"},
            {"kind": "evil_kind", "detail": "should be dropped"},
            "not a dict",
            {"kind": "exec", "params": {"argv": ["/opt/x/app"]}},
        ],
        "confidence": 5,
        "notes": ["hello"],
    }
    plan = plan_from_dict(raw)
    assert plan.strategy == "shim"
    assert plan.runtime == "podman"
    assert plan.packages == ["libfoo1", "42"]
    assert plan.env["FOO"] == "bar" and plan.env["NUM"] == "3"
    assert plan.confidence == 1.0  # clamped
    assert kinds(plan) == ["install_packages", "exec"]


def test_plan_from_dict_rejects_bad_strategy():
    plan = plan_from_dict({"strategy": "rm -rf /", "runtime": "nope"})
    assert plan.strategy == "native"
    assert plan.runtime == "host"


def test_llm_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITON_LLM_API_KEY", raising=False)
    with pytest.raises(LLMUnavailable):
        LLMClient.from_env()


def test_make_plan_falls_back_to_rules_when_llm_fails(simple_app, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LITON_LLM_API_KEY", raising=False)
    a = analyze(str(simple_app))
    plan = make_plan(a, use_llm=True)
    assert plan.source == "rules"
    assert plan.strategy == "native"


def test_make_plan_merges_llm_packages_with_rules(missing_dep_app):
    a = analyze(str(missing_dep_app))

    class FakeClient:
        def complete(self, prompt, system=None):
            return ('{"strategy":"shim","runtime":"host","packages":["libextra9"],'
                    '"env":{"LITON_FAKE":"1"},"confidence":0.9,'
                    '"steps":[{"kind":"exec","detail":"x","params":{"argv":["/x"]}}]}')

    plan = make_plan(a, use_llm=True, client=FakeClient())
    assert plan.source == "llm"
    assert plan.strategy == "shim"
    # rules-discovered package must be preserved
    assert "libsdl2-2.0-0" in plan.packages
    assert "libextra9" in plan.packages


def test_llm_client_extracts_json_from_fences(monkeypatch):
    client = LLMClient("key")
    monkeypatch.setattr(client, "complete", lambda p, system=None:
                        "```json\n{\"strategy\":\"native\"}\n```")
    from liton.planner import _extract_json
    assert _extract_json(client.complete("x"))["strategy"] == "native"
