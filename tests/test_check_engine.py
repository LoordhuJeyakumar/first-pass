"""
Independently authored unit test suite for First Pass Deterministic QC Check Engine.
Authored strictly against data/specs/streamone.json and docs/DOMAIN.md specifications.
"""

import pytest
import pytest_cov
import sys
import os

# Ensure agents package is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.check_engine import (
    evaluate_audio_loudness,
    evaluate_audio_true_peak,
    evaluate_language_readiness,
    evaluate_video_color_primaries,
    evaluate_timed_text_coverage,
    evaluate_packaging_naming,
    evaluate_india_mode_gating,
    evaluate_master_against_spec,
)

# -----------------------------------------------------------------------------
# Clause A-2.1: Integrated Loudness (-27 LUFS +/- 2 LU)
# -----------------------------------------------------------------------------

def test_audio_loudness_exact_target():
    result = evaluate_audio_loudness(-27.0, target=-27.0, tolerance=2.0)
    assert result["passed"] is True
    assert result["severity"] is None

def test_audio_loudness_upper_boundary():
    result = evaluate_audio_loudness(-25.0, target=-27.0, tolerance=2.0)
    assert result["passed"] is True

def test_audio_loudness_lower_boundary():
    result = evaluate_audio_loudness(-29.0, target=-27.0, tolerance=2.0)
    assert result["passed"] is True

def test_audio_loudness_theatrical_mix_violation():
    # -24 LUFS is typical theatrical mix, violates streaming spec (-27 +/- 2)
    result = evaluate_audio_loudness(-24.0, target=-27.0, tolerance=2.0)
    assert result["passed"] is False
    assert result["severity"] == "blocker"
    assert "LUFS" in result["message"]

def test_audio_loudness_too_quiet_violation():
    result = evaluate_audio_loudness(-30.0, target=-27.0, tolerance=2.0)
    assert result["passed"] is False
    assert result["severity"] == "blocker"

# -----------------------------------------------------------------------------
# Clause V-1.3: Video Color Primaries (BT.2020)
# -----------------------------------------------------------------------------

def test_video_color_primaries_bt2020_pass():
    result = evaluate_video_color_primaries("BT.2020", target="BT.2020")
    assert result["passed"] is True

def test_video_color_primaries_rec709_fail():
    result = evaluate_video_color_primaries("Rec.709", target="BT.2020")
    assert result["passed"] is False
    assert result["severity"] == "blocker"

# -----------------------------------------------------------------------------
# Clause T-4.2: Timed Text Language Coverage
# -----------------------------------------------------------------------------

def test_timed_text_coverage_complete_pass():
    audio_langs = ["ta-IN", "hi-IN"]
    subtitle_langs = ["ta-IN", "hi-IN"]
    result = evaluate_timed_text_coverage(subtitle_langs, audio_langs)
    assert result["passed"] is True

def test_timed_text_coverage_missing_subtitle_fail():
    audio_langs = ["ta-IN", "hi-IN"]
    subtitle_langs = ["hi-IN"]  # missing ta-IN
    result = evaluate_timed_text_coverage(subtitle_langs, audio_langs)
    assert result["passed"] is False
    assert result["severity"] == "blocker"
    assert "ta-IN" in result["missing_languages"]

# -----------------------------------------------------------------------------
# Clause P-1.1: Component Packaging Naming Pattern
# -----------------------------------------------------------------------------

def test_packaging_naming_pass():
    result = evaluate_packaging_naming(True)
    assert result["passed"] is True

def test_packaging_naming_fail_warning():
    result = evaluate_packaging_naming(False)
    assert result["passed"] is False
    assert result["severity"] == "warning"

# -----------------------------------------------------------------------------
# India Mode CBFC Regulatory Clearance Gating
# -----------------------------------------------------------------------------

def test_india_mode_original_cleared_dub_allowed():
    certifications = {"ta-IN": "cleared", "hi-IN": "pending"}
    original_lang = "ta-IN"
    result = evaluate_india_mode_gating(certifications, original_lang)
    assert result["original_cleared"] is True
    assert result["dubs_blocked"] is False

def test_india_mode_original_pending_dubs_blocked():
    certifications = {"ta-IN": "pending", "hi-IN": "cleared"}
    original_lang = "ta-IN"
    result = evaluate_india_mode_gating(certifications, original_lang)
    assert result["original_cleared"] is False
    assert result["dubs_blocked"] is True

# -----------------------------------------------------------------------------
# Full Master Metadata vs StreamOne Spec Integration Evaluation
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Table-Driven Unit Tests
# -----------------------------------------------------------------------------

@pytest.mark.parametrize("loudness,target,tolerance,expected_passed,expected_severity", [
    (-27.0, -27.0, 2.0, True, None),       # Exact target
    (-25.0, -27.0, 2.0, True, None),       # Upper boundary (-25 LUFS)
    (-29.0, -27.0, 2.0, True, None),       # Lower boundary (-29 LUFS)
    (-24.0, -27.0, 2.0, False, "blocker"), # Violation: too loud (+3 LUFS)
    (-30.0, -27.0, 2.0, False, "blocker"), # Violation: too quiet (-3 LUFS)
    (-25.1, -27.0, 2.0, True, None),       # Inside upper edge (-25.1 LUFS)
    (-29.1, -27.0, 2.0, False, "blocker"), # Just below lower boundary
])
def test_evaluate_audio_loudness_table_driven(loudness, target, tolerance, expected_passed, expected_severity):
    res = evaluate_audio_loudness(loudness, target=target, tolerance=tolerance)
    assert res["passed"] is expected_passed
    assert res["severity"] == expected_severity


@pytest.mark.parametrize("primaries,target,expected_passed,expected_severity", [
    ("BT.2020", "BT.2020", True, None),
    ("Rec.709", "BT.2020", False, "blocker"),
    ("P3-D65", "BT.2020", False, "blocker"),
    ("", "BT.2020", False, "blocker"),
])
def test_evaluate_video_color_primaries_table_driven(primaries, target, expected_passed, expected_severity):
    res = evaluate_video_color_primaries(primaries, target=target)
    assert res["passed"] is expected_passed
    assert res["severity"] == expected_severity


@pytest.mark.parametrize("subs,audios,expected_passed,expected_missing", [
    (["ta-IN", "hi-IN"], ["ta-IN", "hi-IN"], True, []),
    (["hi-IN"], ["ta-IN", "hi-IN"], False, ["ta-IN"]),
    ([], ["ta-IN", "hi-IN", "te-IN"], False, ["hi-IN", "ta-IN", "te-IN"]),
    (["ta-IN", "hi-IN", "en-US"], ["ta-IN"], True, []),
])
def test_evaluate_timed_text_coverage_table_driven(subs, audios, expected_passed, expected_missing):
    res = evaluate_timed_text_coverage(subs, audios)
    assert res["passed"] is expected_passed
    assert res["missing_languages"] == expected_missing


@pytest.mark.parametrize("naming_ok,expected_passed,expected_severity", [
    (True, True, None),
    (False, False, "warning"),
])
def test_evaluate_packaging_naming_table_driven(naming_ok, expected_passed, expected_severity):
    res = evaluate_packaging_naming(naming_ok)
    assert res["passed"] is expected_passed
    assert res["severity"] == expected_severity


@pytest.mark.parametrize("certs,orig_lang,expected_cleared,expected_blocked", [
    ({"ta-IN": "cleared", "hi-IN": "pending"}, "ta-IN", True, False),
    ({"ta-IN": "pending", "hi-IN": "cleared"}, "ta-IN", False, True),
    ({}, "ta-IN", False, True),
    ({"ta-IN": "rejected"}, "ta-IN", False, True),
])
def test_evaluate_india_mode_gating_table_driven(certs, orig_lang, expected_cleared, expected_blocked):
    res = evaluate_india_mode_gating(certs, orig_lang)
    assert res["original_cleared"] is expected_cleared
    assert res["dubs_blocked"] is expected_blocked


# -----------------------------------------------------------------------------
# Clause Text Lookup, Missing Clause ID, and Fallback Expression Tests
# -----------------------------------------------------------------------------

def test_evaluate_master_clauses_by_id_and_missing_clause_text():
    """
    Tests clauses_by_id dictionary lookup logic in evaluate_master_against_spec:
    - Spec clause with text returns verbatim clause_text.
    - Spec clause without text key returns empty string default "".
    - Spec clause without clause_id returns clause_text default "".
    """
    spec = {
        "spec_id": "TEST-SPEC-LOOKUP",
        "clauses": [
            {
                "clause_id": "A-2.1",
                "domain": "audio",
                "text": "Integrated audio loudness must be -27 LUFS +/- 2 LU.",
                "check": {"field": "audio_tracks[].integrated_loudness_lufs", "op": "within", "target": -27.0, "tolerance": 2.0},
                "severity_on_fail": "blocker"
            },
            {
                "clause_id": "V-NO-TEXT",
                "domain": "video",
                # text field omitted on purpose
                "check": {"field": "video.color_primaries", "op": "equals", "target": "BT.2020"},
                "severity_on_fail": "blocker"
            },
            {
                # clause_id field omitted on purpose
                "domain": "packaging",
                "check": {"field": "packaging.naming_pattern_ok", "op": "equals", "target": True},
                "severity_on_fail": "warning"
            }
        ]
    }
    master = {
        "master_id": "TEST-LOOKUP-MASTER",
        "video": {"color_primaries": "Rec.709"},
        "audio_tracks": [{"language": "ta-IN", "integrated_loudness_lufs": -20.0}],
        "packaging": {"naming_pattern_ok": False}
    }

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    findings_by_clause = {f.get("clause_id"): f for f in report["findings"]}

    # Clause A-2.1: text present
    assert findings_by_clause["A-2.1"]["clause_text"] == "Integrated audio loudness must be -27 LUFS +/- 2 LU."

    # Clause V-NO-TEXT: clause_id present, text absent -> defaults to ""
    assert findings_by_clause["V-NO-TEXT"]["clause_text"] == ""

    # Clause without clause_id -> clause_id is None, clause_text is ""
    none_findings = [f for f in report["findings"] if f.get("clause_id") is None]
    assert len(none_findings) == 1
    assert none_findings[0]["clause_text"] == ""


def test_evaluate_master_fallback_expressions():
    """
    Tests fallback expressions in evaluate_master_against_spec when sub-evaluators
    do not provide measured/expected or when clause text is absent:
    - Timed text fallback: 'missing ta-IN' and 'subtitles for hi-IN, ta-IN'
    - Packaging fallback: 'invalid pattern' and 'valid naming pattern'
    """
    spec = {
        "spec_id": "TEST-SPEC-FALLBACKS",
        "clauses": [
            {
                "clause_id": "T-4.2",
                "domain": "timed_text",
                "check": {"op": "language_coverage", "of": "timed_text", "against": "audio_tracks"},
                "severity_on_fail": "blocker"
            },
            {
                "clause_id": "P-1.1",
                "domain": "packaging",
                "check": {"field": "packaging.naming_pattern_ok", "op": "equals", "target": True},
                "severity_on_fail": "warning"
            }
        ]
    }
    master = {
        "master_id": "TEST-FALLBACKS-MASTER",
        "audio_tracks": [
            {"language": "ta-IN", "role": "original"},
            {"language": "hi-IN", "role": "dub"}
        ],
        "timed_text": [
            {"language": "hi-IN", "type": "subtitle"}
        ],
        "packaging": {"naming_pattern_ok": False}
    }

    report = evaluate_master_against_spec(master, spec)
    findings = {f["clause_id"]: f for f in report["findings"]}

    # Timed text fallbacks
    tt_finding = findings["T-4.2"]
    assert tt_finding["measured"] == "missing ta-IN"
    assert tt_finding["expected"] == "subtitles for hi-IN, ta-IN"
    assert tt_finding["clause_text"] == ""

    # Packaging fallbacks
    pkg_finding = findings["P-1.1"]
    assert pkg_finding["measured"] == "invalid pattern"
    assert pkg_finding["expected"] == "valid naming pattern"
    assert pkg_finding["clause_text"] == ""


def test_evaluate_master_clause_id_absent_from_spec_clauses_by_id():
    """
    Tests that evaluate_master_against_spec handles a finding where the clause_id
    is absent from the spec's clauses_by_id map without throwing KeyError.
    """
    spec = {
        "spec_id": "TEST-ABSENT-ID",
        "clauses": [
            {
                "clause_id": "A-2.1",
                "domain": "audio",
                "text": "Audio check clause",
                "check": {"field": "audio_tracks[].integrated_loudness_lufs", "op": "within", "target": -27.0, "tolerance": 2.0}
            }
        ]
    }
    master = {
        "master_id": "TEST-MASTER-PASS",
        "audio_tracks": [{"language": "ta-IN", "integrated_loudness_lufs": -27.0}]
    }

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "PASS"
    assert report["blocker_count"] == 0
    assert report["findings"] == []


def test_evaluate_master_warning_severities_and_packaging_blocker():
    """
    Tests warning_count increments across audio, video, and timed_text clauses when
    severity_on_fail is set to 'warning', and blocker_count for packaging when blocker.
    Exercises lines 181, 203, 223, and 241 in agents/check_engine.py.
    """
    spec = {
        "spec_id": "TEST-SPEC-WARNINGS",
        "clauses": [
            {
                "clause_id": "A-WARN",
                "domain": "audio",
                "check": {"field": "audio_tracks[].integrated_loudness_lufs", "op": "within", "target": -27.0, "tolerance": 2.0},
                "severity_on_fail": "warning",
            },
            {
                "clause_id": "V-WARN",
                "domain": "video",
                "check": {"field": "video.color_primaries", "op": "equals", "target": "BT.2020"},
                "severity_on_fail": "warning",
            },
            {
                "clause_id": "T-WARN",
                "domain": "timed_text",
                "check": {"op": "language_coverage", "of": "timed_text", "against": "audio_tracks"},
                "severity_on_fail": "warning",
            },
            {
                "clause_id": "P-BLOCK",
                "domain": "packaging",
                "check": {"field": "packaging.naming_pattern_ok", "op": "equals", "target": True},
                "severity_on_fail": "blocker",
            },
        ],
    }
    master = {
        "master_id": "TEST-WARNINGS-MASTER",
        "audio_tracks": [{"language": "ta-IN", "integrated_loudness_lufs": -19.0}],
        "video": {"color_primaries": "Rec.709"},
        "timed_text": [],
        "packaging": {"naming_pattern_ok": False},
    }

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"  # Packaging is blocker
    assert report["blocker_count"] == 1
    assert report["warning_count"] == 3


def test_evaluate_master_india_mode_integration():
    """
    Tests India mode gating evaluation during evaluate_master_against_spec.
    Exercises lines 250-255 in agents/check_engine.py.
    """
    spec = {
        "spec_id": "TEST-SPEC-INDIA",
        "clauses": [
            {
                "clause_id": "P-1.1",
                "domain": "packaging",
                "check": {"field": "packaging.naming_pattern_ok", "op": "equals", "target": True},
                "severity_on_fail": "warning",
            }
        ],
        "india_mode": {"enabled": True},
    }
    master = {
        "master_id": "TEST-INDIA-MASTER",
        "audio_tracks": [
            {"language": "ta-IN", "role": "original"},
            {"language": "hi-IN", "role": "dub"},
        ],
        "packaging": {"naming_pattern_ok": True},
        "certification": {
            "ta-IN": "pending",
            "hi-IN": "cleared",
        },
    }

    report = evaluate_master_against_spec(master, spec)
    assert report["india_mode"] is not None
    assert report["india_mode"]["original_language"] == "ta-IN"
    assert report["india_mode"]["original_status"] == "pending"
    assert report["india_mode"]["dubs_blocked"] is True


# -----------------------------------------------------------------------------
# Clause A-2.2: True Peak Level (max -2.0 dBTP)
# -----------------------------------------------------------------------------

def test_audio_true_peak_pass():
    result = evaluate_audio_true_peak(-2.5, target_max=-2.0)
    assert result["passed"] is True
    assert result["severity"] is None
    assert "dBTP" in result["message"]


def test_audio_true_peak_exact_limit_pass():
    result = evaluate_audio_true_peak(-2.0, target_max=-2.0)
    assert result["passed"] is True
    assert result["severity"] is None


def test_audio_true_peak_violation_fail():
    result = evaluate_audio_true_peak(-1.0, target_max=-2.0)
    assert result["passed"] is False
    assert result["severity"] == "blocker"
    assert "dBTP" in result["message"]


@pytest.mark.parametrize("true_peak,target_max,expected_passed,expected_severity", [
    (-2.5, -2.0, True, None),
    (-2.0, -2.0, True, None),
    (-1.9, -2.0, False, "blocker"),
    (-1.0, -2.0, False, "blocker"),
    (0.0, -2.0, False, "blocker"),
])
def test_evaluate_audio_true_peak_table_driven(true_peak, target_max, expected_passed, expected_severity):
    res = evaluate_audio_true_peak(true_peak, target_max=target_max)
    assert res["passed"] is expected_passed
    assert res["severity"] == expected_severity


def test_audio_true_peak_warning_severity():
    """Tests True Peak check with warning severity when severity_on_fail is warning."""
    spec = {
        "spec_id": "TEST-SPEC-TP-WARN",
        "clauses": [
            {
                "clause_id": "A-2.2-WARN",
                "domain": "audio",
                "check": {"field": "audio_tracks[].true_peak_dbtp", "op": "max", "target": -2.0},
                "severity_on_fail": "warning",
            }
        ]
    }
    master = {
        "master_id": "TEST-TP-WARN-MASTER",
        "audio_tracks": [{"language": "ta-IN", "true_peak_dbtp": -1.0}],
    }
    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "PASS"
    assert report["warning_count"] == 1
    assert report["blocker_count"] == 0


# -----------------------------------------------------------------------------
# Language Readiness Ratio
# -----------------------------------------------------------------------------

def test_language_readiness_all_cleared():
    audio = [{"language": "ta-IN"}]
    sub = [{"language": "ta-IN"}]
    certs = {"ta-IN": "cleared"}
    res = evaluate_language_readiness(audio, sub, certs)
    assert res["ta-IN"] == 1.0


def test_language_readiness_partial():
    audio = [{"language": "ta-IN"}, {"language": "hi-IN"}]
    sub = [{"language": "hi-IN"}]  # missing ta-IN
    certs = {"ta-IN": "cleared", "hi-IN": "pending"}
    res = evaluate_language_readiness(audio, sub, certs)
    # ta-IN: audio yes (1), sub no (0), cert cleared yes (1) -> 2/3 = 0.667
    assert res["ta-IN"] == 0.667
    # hi-IN: audio yes (1), sub yes (1), cert cleared no (0) -> 2/3 = 0.667
    assert res["hi-IN"] == 0.667


def test_language_readiness_zero_score():
    audio = []
    sub = []
    certs = {"kn-IN": "pending"}
    res = evaluate_language_readiness(audio, sub, certs)
    assert res["kn-IN"] == 0.0


def test_master_blockers_full_integration_with_true_peak():
    """
    Integration test verifying master_blockers against real streamone spec
    produces 3 blockers (Loudness A-2.1, True Peak A-2.2, Subtitles T-4.2).
    """
    import json
    spec_path = os.path.join(os.path.dirname(__file__), "..", "data", "specs", "streamone.json")
    blockers_path = os.path.join(os.path.dirname(__file__), "..", "data", "masters", "master_blockers.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    with open(blockers_path, "r", encoding="utf-8") as f:
        master = json.load(f)

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] == 3
    clause_ids = [f["clause_id"] for f in report["findings"]]
    assert "A-2.1" in clause_ids
    assert "A-2.2" in clause_ids
    assert "T-4.2" in clause_ids


def test_evaluate_master_loudness_evaluation_includes_tolerance_lu():
    """
    Verifies that clause A-2.1 evaluation output contains tolerance_lu and target_lufs
    matching the spec's tolerance value (2.0 LU) and target (-27.0 LUFS).
    """
    import json
    spec_path = os.path.join(os.path.dirname(__file__), "..", "data", "specs", "streamone.json")
    blockers_path = os.path.join(os.path.dirname(__file__), "..", "data", "masters", "master_blockers.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    with open(blockers_path, "r", encoding="utf-8") as f:
        master = json.load(f)

    report = evaluate_master_against_spec(master, spec)
    a21_evals = [e for e in report["evaluations"] if e.get("clause_id") == "A-2.1"]
    assert len(a21_evals) > 0
    for ev in a21_evals:
        assert ev["tolerance_lu"] == 2.0
        assert ev["target_lufs"] == -27.0




