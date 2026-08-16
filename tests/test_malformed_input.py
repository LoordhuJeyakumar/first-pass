"""
Malformed spec and master inputs must fail loudly.

These assertions encode post-fix behaviour and fail on the pre-fix engine:
invented spec defaults, vacuous PASS on missing audio, and uncaught TypeError.
"""

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.check_engine import evaluate_master_against_spec


def _clause_a21(**check_overrides):
    check = {
        "field": "audio_tracks[].integrated_loudness_lufs",
        "op": "within",
        "target": -27.0,
        "tolerance": 2.0,
    }
    check.update(check_overrides)
    return {
        "clause_id": "A-2.1",
        "domain": "audio",
        "text": "Integrated loudness of every audio track must be -27 LUFS +/- 2 LU.",
        "check": check,
        "severity_on_fail": "blocker",
    }


def _clause_v13():
    return {
        "clause_id": "V-1.3",
        "domain": "video",
        "text": "HDR masters must carry BT.2020 primaries.",
        "check": {
            "field": "video.color_primaries",
            "op": "equals",
            "target": "BT.2020",
        },
        "severity_on_fail": "blocker",
    }


def _audio_track(loudness=-27.0, true_peak=-2.1, language="ta-IN"):
    track = {"language": language, "role": "original"}
    if loudness is not Ellipsis:
        track["integrated_loudness_lufs"] = loudness
    if true_peak is not Ellipsis:
        track["true_peak_dbtp"] = true_peak
    return track


def _spec_errors_text(report):
    parts = []
    for err in report.get("spec_errors") or []:
        parts.append(f"{err.get('clause_id')}:{err.get('field')}:{err.get('message')}")
    for finding in report.get("findings") or []:
        parts.append(str(finding.get("message", "")))
    return " ".join(parts)


def test_spec_missing_tolerance_is_spec_error_not_invented_2_0():
    spec = {
        "spec_id": "MALFORMED-TOL",
        "clauses": [_clause_a21()],
    }
    del spec["clauses"][0]["check"]["tolerance"]
    master = {"master_id": "M", "audio_tracks": [_audio_track()]}

    report = evaluate_master_against_spec(master, spec)

    blob = _spec_errors_text(report)
    assert "A-2.1" in blob
    assert "tolerance" in blob
    assert report.get("spec_errors"), "malformed spec must populate spec_errors"
    for ev in report.get("evaluations") or []:
        assert ev.get("tolerance_lu") != 2.0 or "tolerance" in str(report.get("spec_errors"))
    invented = [
        ev for ev in (report.get("evaluations") or [])
        if ev.get("clause_id") == "A-2.1" and ev.get("tolerance_lu") == 2.0
    ]
    assert invented == [], "engine must not invent tolerance_lu=2.0 from a spec that omitted it"


def test_spec_missing_target_is_spec_error_not_invented_minus_27():
    spec = {
        "spec_id": "MALFORMED-TARGET",
        "clauses": [_clause_a21()],
    }
    del spec["clauses"][0]["check"]["target"]
    master = {"master_id": "M", "audio_tracks": [_audio_track()]}

    report = evaluate_master_against_spec(master, spec)

    blob = _spec_errors_text(report)
    assert "A-2.1" in blob
    assert "target" in blob
    invented = [
        ev for ev in (report.get("evaluations") or [])
        if ev.get("clause_id") == "A-2.1" and ev.get("target_lufs") == -27.0
    ]
    assert invented == [], "engine must not invent target_lufs=-27.0 from a spec that omitted it"


def test_spec_missing_op_is_spec_error():
    spec = {
        "spec_id": "MALFORMED-OP",
        "clauses": [_clause_a21()],
    }
    del spec["clauses"][0]["check"]["op"]
    master = {"master_id": "M", "audio_tracks": [_audio_track()]}

    report = evaluate_master_against_spec(master, spec)
    blob = _spec_errors_text(report)
    assert "A-2.1" in blob
    assert "op" in blob
    assert report.get("spec_errors")


def test_spec_missing_field_is_spec_error():
    spec = {
        "spec_id": "MALFORMED-FIELD",
        "clauses": [_clause_a21()],
    }
    del spec["clauses"][0]["check"]["field"]
    master = {"master_id": "M", "audio_tracks": [_audio_track()]}

    report = evaluate_master_against_spec(master, spec)
    blob = _spec_errors_text(report)
    assert "A-2.1" in blob
    assert "field" in blob
    assert report.get("spec_errors")


def test_spec_reports_all_missing_fields_at_once():
    spec = {
        "spec_id": "MALFORMED-MULTI",
        "clauses": [
            {
                "clause_id": "A-2.1",
                "domain": "audio",
                "check": {"op": "within", "field": "audio_tracks[].integrated_loudness_lufs"},
                "severity_on_fail": "blocker",
            },
            {
                "clause_id": "V-1.3",
                "domain": "video",
                "check": {"op": "equals", "field": "video.color_primaries"},
                "severity_on_fail": "blocker",
            },
        ],
    }
    master = {
        "master_id": "M",
        "audio_tracks": [_audio_track()],
        "video": {"color_primaries": "BT.2020"},
    }

    report = evaluate_master_against_spec(master, spec)
    errors = report.get("spec_errors") or []
    fields_by_clause = {}
    for err in errors:
        fields_by_clause.setdefault(err["clause_id"], set()).add(err["field"])
    assert "target" in fields_by_clause.get("A-2.1", set())
    assert "tolerance" in fields_by_clause.get("A-2.1", set())
    assert "target" in fields_by_clause.get("V-1.3", set())
    assert len(errors) >= 3


def test_audio_tracks_missing_is_blocker_not_pass():
    spec = {"spec_id": "S", "clauses": [_clause_a21()]}
    master = {"master_id": "NO-AUDIO-KEY"}

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] != "PASS"
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] >= 1
    blob = _spec_errors_text(report)
    assert "audio_tracks" in blob


def test_audio_tracks_empty_list_is_blocker_not_pass():
    spec = {"spec_id": "S", "clauses": [_clause_a21()]}
    master = {"master_id": "EMPTY-AUDIO", "audio_tracks": []}

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] >= 1
    blob = _spec_errors_text(report)
    assert "audio_tracks" in blob


@pytest.mark.parametrize("bad_loudness", ["-27.0", None, True])
def test_non_numeric_loudness_is_blocker_not_exception(bad_loudness):
    spec = {"spec_id": "S", "clauses": [_clause_a21()]}
    master = {"master_id": "BAD-TYPE", "audio_tracks": [_audio_track(loudness=bad_loudness)]}

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] >= 1
    assert report["findings"]


def test_video_block_missing_when_v13_exists_is_blocker():
    spec = {"spec_id": "S", "clauses": [_clause_v13()]}
    master = {"master_id": "NO-VIDEO", "audio_tracks": [_audio_track()]}

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] >= 1
    blob = _spec_errors_text(report)
    assert "video" in blob.lower()
    video_findings = [f for f in report["findings"] if f.get("domain") == "video"]
    assert video_findings
    assert video_findings[0]["clause_id"] == "V-1.3"


def test_missing_video_uses_spec_video_clause_id_not_v13():
    spec = {
        "spec_id": "HALLARC-SHAPE",
        "clauses": [
            {
                "clause_id": "V-2.1",
                "domain": "video",
                "text": "Masters must carry BT.709 primaries.",
                "check": {
                    "field": "video.color_primaries",
                    "op": "equals",
                    "target": "BT.709",
                },
                "severity_on_fail": "blocker",
            }
        ],
    }
    master = {"master_id": "NO-VIDEO", "audio_tracks": [_audio_track()]}

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    video_findings = [f for f in report["findings"] if f.get("domain") == "video"]
    assert len(video_findings) == 1
    assert video_findings[0]["clause_id"] == "V-2.1"
    assert video_findings[0]["severity"] == "blocker"


def test_certification_missing_when_india_gating_runs_is_explicit():
    spec = {
        "spec_id": "S",
        "clauses": [
            {
                "clause_id": "P-1.1",
                "domain": "packaging",
                "check": {"field": "packaging.naming_pattern_ok", "op": "equals", "target": True},
                "severity_on_fail": "warning",
            }
        ],
        "india_mode": {"gating_rule": "original_language_certification_required_before_dub_clearance"},
    }
    master = {
        "master_id": "NO-CERT",
        "audio_tracks": [_audio_track()],
        "packaging": {"naming_pattern_ok": True},
    }

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] >= 1
    blob = _spec_errors_text(report).lower()
    assert "certif" in blob
    # Must not silently skip India gating with india_mode is None and PASS
    assert report["india_mode"] is not None or any("certif" in str(f).lower() for f in report["findings"])


def test_language_coverage_missing_of_against_are_all_reported():
    spec = {
        "spec_id": "MALFORMED-TT",
        "clauses": [
            {
                "clause_id": "T-4.2",
                "domain": "timed_text",
                "check": {"op": "language_coverage"},
                "severity_on_fail": "blocker",
            }
        ],
    }
    report = evaluate_master_against_spec({"master_id": "M", "audio_tracks": [_audio_track()]}, spec)
    fields = {err["field"] for err in report["spec_errors"]}
    assert "of" in fields
    assert "against" in fields


def test_unsupported_op_is_spec_error():
    spec = {
        "spec_id": "BAD-OP",
        "clauses": [
            {
                "clause_id": "A-X",
                "domain": "audio",
                "check": {"op": "lte", "field": "x", "target": -2.0},
                "severity_on_fail": "blocker",
            }
        ],
    }
    report = evaluate_master_against_spec({"master_id": "M"}, spec)
    blob = _spec_errors_text(report)
    assert "A-X" in blob
    assert "op" in blob


def test_missing_check_object_and_missing_clauses_list():
    report = evaluate_master_against_spec(
        {"master_id": "M"},
        {"spec_id": "NO-CHECK", "clauses": [{"clause_id": "A-2.1", "domain": "audio"}]},
    )
    assert any(err["field"] == "check" for err in report["spec_errors"])

    report2 = evaluate_master_against_spec({"master_id": "M"}, {"spec_id": "NO-CLAUSES"})
    assert any(err["field"] == "clauses" for err in report2["spec_errors"])


def test_timed_text_section_missing_is_blocker():
    spec = {
        "spec_id": "S",
        "clauses": [
            {
                "clause_id": "T-4.2",
                "domain": "timed_text",
                "check": {"op": "language_coverage", "of": "timed_text", "against": "audio_tracks"},
                "severity_on_fail": "blocker",
            }
        ],
    }
    master = {"master_id": "NO-TT", "audio_tracks": [_audio_track()]}
    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert "timed_text" in _spec_errors_text(report)


def test_packaging_section_missing_and_naming_key_missing():
    spec = {
        "spec_id": "S",
        "clauses": [
            {
                "clause_id": "P-1.1",
                "domain": "packaging",
                "check": {"field": "packaging.naming_pattern_ok", "op": "equals", "target": True},
                "severity_on_fail": "warning",
            }
        ],
    }
    missing_section = evaluate_master_against_spec({"master_id": "M"}, spec)
    assert missing_section["warning_count"] >= 1 or missing_section["blocker_count"] >= 1
    assert "packaging" in _spec_errors_text(missing_section).lower()

    missing_key = evaluate_master_against_spec(
        {"master_id": "M", "packaging": {"components": ["video"]}},
        spec,
    )
    assert missing_key["findings"]
    assert "naming_pattern_ok" in _spec_errors_text(missing_key)


def test_video_primaries_missing_inside_video_block():
    spec = {"spec_id": "S", "clauses": [_clause_v13()]}
    master = {"master_id": "M", "video": {"resolution": "3840x2160"}}
    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert "color_primaries" in _spec_errors_text(report)


def test_wrong_section_types_and_empty_audio_skips_true_peak():
    spec = {
        "spec_id": "S",
        "clauses": [
            {
                "clause_id": "A-2.2",
                "domain": "audio",
                "check": {"field": "audio_tracks[].true_peak_dbtp", "op": "max", "target": -2.0},
                "severity_on_fail": "blocker",
            },
            _clause_v13(),
        ],
    }
    report = evaluate_master_against_spec(
        {"master_id": "M", "audio_tracks": "not-a-list", "video": "not-a-dict"},
        spec,
    )
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] >= 1


def test_true_peak_non_numeric_is_blocker():
    spec = {
        "spec_id": "S",
        "clauses": [
            {
                "clause_id": "A-2.2",
                "domain": "audio",
                "check": {"field": "audio_tracks[].true_peak_dbtp", "op": "max", "target": -2.0},
                "severity_on_fail": "blocker",
            }
        ],
    }
    master = {
        "master_id": "M",
        "audio_tracks": [_audio_track(true_peak="hot")],
    }
    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["findings"]

@pytest.mark.parametrize("loudness", [-999.0, 999.0])
def test_extreme_loudness_evaluated_no_crash(loudness):
    spec = {"spec_id": "S", "clauses": [_clause_a21()]}
    master = {"master_id": "EXTREME", "audio_tracks": [_audio_track(loudness=loudness)]}

    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    a21 = [e for e in report["evaluations"] if e.get("clause_id") == "A-2.1"]
    assert a21
    assert a21[0]["loudness_lufs"] == loudness
    assert a21[0]["passed"] is False


def test_empty_clauses_is_spec_error_not_pass():
    report = evaluate_master_against_spec(
        {"master_id": "M"},
        {"spec_id": "X", "clauses": []},
    )
    assert report["verdict"] == "REJECT"
    assert report["blocker_count"] >= 1
    assert report["evaluations"] == []
    blob = _spec_errors_text(report).lower()
    assert "no clauses" in blob
    assert report.get("spec_errors")


def test_non_object_clause_entries_are_spec_error_not_pass():
    report = evaluate_master_against_spec(
        {"master_id": "M"},
        {"spec_id": "X", "clauses": ["not-a-clause"]},
    )
    assert report["verdict"] == "REJECT"
    assert report.get("spec_errors")


@pytest.mark.parametrize("master", [[], "master.json"])
def test_master_wrong_outer_type_is_structured_error(master):
    spec = {"spec_id": "S", "clauses": [_clause_a21()]}
    report = evaluate_master_against_spec(master, spec)
    assert report["verdict"] == "REJECT"
    assert report["spec_errors"]
    assert report["spec_errors"][0]["field"] == "master"
    assert "object" in report["spec_errors"][0]["message"]


def test_spec_as_list_is_structured_error():
    report = evaluate_master_against_spec({"master_id": "M"}, [])
    assert report["verdict"] == "REJECT"
    assert report["spec_errors"]
    assert report["spec_errors"][0]["field"] == "spec"
    assert "object" in report["spec_errors"][0]["message"]
