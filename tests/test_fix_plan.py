"""Deterministic ranked fix-plan tests. No LLM."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.check_engine import (
    STAGE_ORDER,
    build_ranked_fix_plan,
    evaluate_master_against_spec,
    stage_for_domain,
)
from agents.orchestrator import assert_ground_truth_preservation


SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "specs", "streamone.json")
BLOCKERS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "masters", "master_blockers.json")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_stage_order_is_lead_time_not_pipeline():
    assert STAGE_ORDER == (
        "regulatory",
        "subtitling",
        "mix",
        "conform",
        "packaging",
    )


def test_timed_text_is_not_packaging():
    assert stage_for_domain("timed_text") == "subtitling"
    assert stage_for_domain("packaging") == "packaging"
    assert stage_for_domain(None) == "unknown"
    assert stage_for_domain("spec") == "unknown"


def test_master_blockers_ranked_order_and_mix_group():
    spec = _load(SPEC_PATH)
    master = _load(BLOCKERS_PATH)
    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] == 3
    assert report["warning_count"] == 0
    assert report["india_mode"]["dubs_blocked"] is True
    assert len(report["readiness"]) == 5

    jobs = report["ranked_fix_plan"]["jobs"]
    stages = [j["remediation_stage"] for j in jobs]
    assert stages == ["regulatory", "subtitling", "mix"]
    assert jobs[0]["language_fanout"] == 4
    assert jobs[1]["language_fanout"] == 4
    assert jobs[1]["clause_ids"] == ["T-4.2"]
    assert jobs[2]["clause_ids"] == ["A-2.1", "A-2.2"]
    assert jobs[2]["language_fanout"] == 1


def test_subtitling_and_packaging_never_share_a_job():
    plan = build_ranked_fix_plan(
        {
            "findings": [
                {
                    "clause_id": "T-4.2",
                    "domain": "timed_text",
                    "severity": "blocker",
                    "missing_languages": ["ta-IN"],
                    "measured": "missing ta-IN",
                    "expected": "subtitles",
                },
                {
                    "clause_id": "P-1.1",
                    "domain": "packaging",
                    "severity": "warning",
                    "measured": "invalid pattern",
                    "expected": "valid naming pattern",
                },
            ],
            "india_mode": {"dubs_blocked": False},
        },
        {},
    )
    stages = [j["remediation_stage"] for j in plan["jobs"]]
    assert stages == ["subtitling", "packaging"]
    assert plan["jobs"][0]["clause_ids"] == ["T-4.2"]
    assert plan["jobs"][1]["clause_ids"] == ["P-1.1"]


def test_equal_fanout_blockers_sort_by_stage_order():
    plan = build_ranked_fix_plan(
        {
            "findings": [
                {
                    "clause_id": "T-4.2",
                    "domain": "timed_text",
                    "severity": "blocker",
                    "missing_languages": ["a", "b", "c", "d"],
                    "measured": "missing four",
                },
            ],
            "india_mode": {
                "dubs_blocked": True,
                "original_language": "ta-IN",
                "original_status": "pending",
                "message": "gated",
            },
            "readiness": {},
        },
        {
            "india_mode": {
                "required_languages": ["ta-IN", "te-IN", "hi-IN", "kn-IN", "ml-IN"],
                "gating_rule": "original_language_certification_required_before_dub_clearance",
            }
        },
    )
    assert [j["remediation_stage"] for j in plan["jobs"]] == ["regulatory", "subtitling"]
    assert plan["jobs"][0]["language_fanout"] == 4
    assert plan["jobs"][1]["language_fanout"] == 4


def test_warning_sorts_after_blocker_regardless_of_fanout():
    plan = build_ranked_fix_plan(
        {
            "findings": [
                {
                    "clause_id": "P-1.1",
                    "domain": "packaging",
                    "severity": "warning",
                    "missing_languages": ["a", "b", "c", "d", "e"],
                    "measured": "pattern",
                },
                {
                    "clause_id": "A-2.1",
                    "domain": "audio",
                    "severity": "blocker",
                    "language": "ta-IN",
                    "measured": "-24.0 LUFS",
                },
            ],
            "india_mode": {"dubs_blocked": False},
        },
        {},
    )
    assert [j["remediation_stage"] for j in plan["jobs"]] == ["mix", "packaging"]


def test_regulatory_fanout_falls_back_to_readiness():
    plan = build_ranked_fix_plan(
        {
            "findings": [],
            "india_mode": {
                "dubs_blocked": True,
                "original_language": "ta-IN",
                "original_status": "pending",
                "message": "gated",
            },
            "readiness": {"ta-IN": 0.3, "hi-IN": 0.3, "te-IN": 0.3},
        },
        {"india_mode": {}},
    )
    assert plan["jobs"][0]["remediation_stage"] == "regulatory"
    assert plan["jobs"][0]["language_fanout"] == 2


def test_regulatory_fanout_zero_when_only_original_required():
    plan = build_ranked_fix_plan(
        {
            "findings": [],
            "india_mode": {
                "dubs_blocked": True,
                "original_language": "ta-IN",
                "original_status": "pending",
                "message": "gated",
            },
        },
        {"india_mode": {"required_languages": ["ta-IN"]}},
    )
    assert plan["jobs"][0]["language_fanout"] == 0
    plan = build_ranked_fix_plan(
        {
            "findings": [
                {
                    "clause_id": "SPEC",
                    "domain": "spec",
                    "severity": "blocker",
                    "measured": "bad",
                },
                {
                    "clause_id": "A-2.1",
                    "domain": "audio",
                    "severity": "blocker",
                    "measured": "-24.0 LUFS",
                },
            ],
            "india_mode": None,
        },
        {},
    )
    assert [j["remediation_stage"] for j in plan["jobs"]] == ["mix", "unknown"]
    plan = build_ranked_fix_plan(
        {"findings": [], "india_mode": {"dubs_blocked": False}},
        {},
    )
    assert plan["jobs"] == []


class _Call:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _Event:
    def __init__(self, calls):
        self._calls = calls
        self.content = None

    def get_function_calls(self):
        return self._calls


def test_ground_truth_requires_activity_body():
    findings = [
        {"clause_id": "A-2.1", "measured": "-24.0 LUFS", "expected": "-27.0 ± 2.0 LUFS"},
    ]
    ok = _Event(
        [_Call("add_activity_to_incident", {"body": "Job mix: A-2.1 measured -24.0 LUFS expected -27.0 ± 2.0 LUFS"})]
    )
    assert_ground_truth_preservation([ok], findings)

    missing_measured = _Event(
        [_Call("add_activity_to_incident", {"body": "Job mix: A-2.1 only"})]
    )
    with pytest.raises(AssertionError, match="measured value"):
        assert_ground_truth_preservation([missing_measured], findings)

    missing_clause = _Event(
        [_Call("add_activity_to_incident", {"body": "measured -24.0 LUFS"})]
    )
    with pytest.raises(AssertionError, match="clause ID"):
        assert_ground_truth_preservation([missing_clause], findings)
