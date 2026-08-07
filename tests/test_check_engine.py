"""
Independently authored unit test suite for First Pass Deterministic QC Check Engine.
Authored strictly against data/specs/streamone.json and docs/DOMAIN.md specifications.
"""

import pytest
import sys
import os

# Ensure agents package is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.check_engine import (
    evaluate_audio_loudness,
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

def test_evaluate_master_blockers_fixture():
    spec = {
        "spec_id": "STREAMONE-DELIVERY-2026",
        "clauses": [
            {
                "clause_id": "A-2.1",
                "domain": "audio",
                "check": {"op": "within", "target": -27.0, "tolerance": 2.0},
                "severity_on_fail": "blocker"
            },
            {
                "clause_id": "V-1.3",
                "domain": "video",
                "check": {"op": "equals", "target": "BT.2020"},
                "severity_on_fail": "blocker"
            },
            {
                "clause_id": "T-4.2",
                "domain": "timed_text",
                "check": {"op": "language_coverage"},
                "severity_on_fail": "blocker"
            },
            {
                "clause_id": "P-1.1",
                "domain": "packaging",
                "check": {"op": "equals", "target": True},
                "severity_on_fail": "warning"
            }
        ]
    }

    # Master with theatrical loudness (-24.0 LUFS on ta-IN) & missing subtitle (ta-IN missing)
    master = {
        "master_id": "STRM-2026-TEST-BLOCKER",
        "video": {"color_primaries": "BT.2020"},
        "audio_tracks": [
            {"language": "ta-IN", "role": "original", "integrated_loudness_lufs": -24.0},
            {"language": "hi-IN", "role": "dub", "integrated_loudness_lufs": -27.1}
        ],
        "timed_text": [
            {"language": "hi-IN", "type": "subtitle"}
        ],
        "packaging": {"naming_pattern_ok": True}
    }

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] == 2  # loudness on ta-IN + missing ta-IN subtitle
    clause_ids = [f["clause_id"] for f in report["findings"]]
    assert "A-2.1" in clause_ids
    assert "T-4.2" in clause_ids
