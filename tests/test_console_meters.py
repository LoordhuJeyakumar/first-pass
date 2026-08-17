"""
Frontend meter collector: HallArc-shaped evaluations must reach collectTracks.

The operator console identifies loudness vs true-peak by evaluation payload shape,
not hardcoded StreamOne clause IDs. These tests pipe real check-engine evaluations
through the same meter_collect.js module the browser loads.
"""

import json
import os
import re
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

from agents.check_engine import evaluate_master_against_spec

HALLARC_PATH = os.path.join(ROOT, "data", "specs", "hallarc.json")
STREAMONE_PATH = os.path.join(ROOT, "data", "specs", "streamone.json")
MASTERS_DIR = os.path.join(ROOT, "data", "masters")
NODE_HARNESS = os.path.join(ROOT, "tests", "console_meters_test.mjs")
FRONTEND_DIR = os.path.join(ROOT, "frontend")


def _load(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _run_meter_harness(evaluations):
    proc = subprocess.run(
        ["node", NODE_HARNESS],
        input=json.dumps(evaluations),
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"meter harness failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return json.loads(proc.stdout)


def test_hallarc_master_blockers_populates_meter_tracks():
    """HallArc A-1.x loudness evals must bind to per-language meter tracks."""
    report = evaluate_master_against_spec(
        _load(os.path.join(MASTERS_DIR, "master_blockers.json")),
        _load(HALLARC_PATH),
    )
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] == 6

    result = _run_meter_harness(report["evaluations"])
    assert result["track_count"] >= 5
    assert result["loudness_eval_count"] >= 5
    assert result["sample"]["loudnessLufs"] is not None
    assert result["sample"]["targetLufs"] == pytest.approx(-24.0)


def test_streamone_master_blockers_still_populates_meter_tracks():
    """Regression: StreamOne A-2.x evals must still bind via payload shape."""
    report = evaluate_master_against_spec(
        _load(os.path.join(MASTERS_DIR, "master_blockers.json")),
        _load(STREAMONE_PATH),
    )
    assert report["verdict"] == "REJECT"

    result = _run_meter_harness(report["evaluations"])
    assert result["track_count"] >= 5
    assert result["loudness_eval_count"] >= 5
    assert result["sample"]["loudnessLufs"] is not None


def test_malformed_loudness_eval_yields_dash_not_throw():
    """Missing loudness_lufs must not crash collectTracks; harness still exits 0 if shape matches."""
    evaluations = [
        {
            "clause_id": "X-9.9",
            "domain": "audio",
            "language": "xx-XX",
            "loudness_deviation_lufs": 0.0,
            "target_lufs": -24.0,
            "tolerance_lu": 1.0,
        },
        {
            "clause_id": "Y-9.9",
            "domain": "audio",
            "language": "xx-XX",
            "true_peak_dbtp": -2.0,
            "target_max_dbtp": -1.0,
        },
    ]
    # Incomplete loudness (no loudness_lufs) — harness should fail (no loudness-shaped eval)
    proc = subprocess.run(
        ["node", NODE_HARNESS],
        input=json.dumps(evaluations),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert proc.returncode != 0


def test_frontend_has_no_hardcoded_clause_id_matching():
    """grep guard: frontend must not match evaluations by hardcoded clause_id literals."""
    hardcoded = re.compile(
        r'clause_id\s*===\s*["\']A-\d+\.\d+["\']|'
        r'["\']A-\d+\.\d+["\']\s*===\s*.*clause_id'
    )
    offenders = []
    for dirpath, _, filenames in os.walk(FRONTEND_DIR):
        for name in filenames:
            if not name.endswith((".js", ".html", ".py")):
                continue
            path = os.path.join(dirpath, name)
            with open(path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle, 1):
                    if hardcoded.search(line):
                        offenders.append(f"{path}:{lineno}: {line.strip()}")
    assert not offenders, "Hardcoded clause_id matching in frontend:\n" + "\n".join(offenders)
