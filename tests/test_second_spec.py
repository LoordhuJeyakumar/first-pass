"""HallArc second destination spec: spec-driven verdicts, no India gating."""

import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.check_engine import evaluate_master_against_spec, validate_spec

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HALLARC_PATH = os.path.join(ROOT, "data", "specs", "hallarc.json")
STREAMONE_PATH = os.path.join(ROOT, "data", "specs", "streamone.json")
MASTERS_DIR = os.path.join(ROOT, "data", "masters")


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _hallarc():
    return _load(HALLARC_PATH)


def _master(name):
    return _load(os.path.join(MASTERS_DIR, name))


def _india_absent(report):
    assert report.get("india_mode") is None
    assert not any(f.get("domain") == "certification" for f in report.get("findings") or [])
    jobs = (report.get("ranked_fix_plan") or {}).get("jobs") or []
    assert not any(j.get("remediation_stage") == "regulatory" for j in jobs)


def test_hallarc_and_streamone_pass_validate_spec():
    assert validate_spec(_hallarc()) == []
    assert validate_spec(_load(STREAMONE_PATH)) == []


def test_master_clean_against_hallarc_is_reject_six_blockers():
    report = evaluate_master_against_spec(_master("master_clean.json"), _hallarc())
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] == 6
    assert report["spec_id"] == "HALLARC-SCREENING-2026"
    clause_ids = [f["clause_id"] for f in report["findings"]]
    assert clause_ids.count("A-1.1") == 5
    assert "V-2.1" in clause_ids
    _india_absent(report)


def test_master_warnings_against_hallarc_is_reject_two_blockers():
    report = evaluate_master_against_spec(_master("master_warnings.json"), _hallarc())
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] == 2
    clause_ids = [f["clause_id"] for f in report["findings"] if f.get("severity") == "blocker"]
    assert "A-1.1" in clause_ids
    assert "V-2.1" in clause_ids
    _india_absent(report)


def test_master_blockers_against_hallarc_is_reject_six_blockers():
    report = evaluate_master_against_spec(_master("master_blockers.json"), _hallarc())
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] == 6
    clause_ids = [f["clause_id"] for f in report["findings"]]
    assert "A-1.1" in clause_ids
    assert "V-2.1" in clause_ids
    assert "T-3.1" in clause_ids
    _india_absent(report)


def test_spec_without_india_mode_emits_no_india_findings():
    spec = {
        "spec_id": "NO-INDIA",
        "clauses": [
            {
                "clause_id": "A-1.1",
                "domain": "audio",
                "text": "Integrated loudness -24 LUFS +/- 1 LU.",
                "check": {
                    "field": "audio_tracks[].integrated_loudness_lufs",
                    "op": "within",
                    "target": -24.0,
                    "tolerance": 1.0,
                },
                "severity_on_fail": "blocker",
            }
        ],
    }
    assert "india_mode" not in spec
    assert validate_spec(spec) == []
    master = _master("master_blockers.json")
    report = evaluate_master_against_spec(master, spec)
    _india_absent(report)
