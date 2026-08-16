"""
First Pass — Deterministic QC Check Engine

Pure Python evaluation engine for technical master metadata against platform delivery specifications.
No LLM calls or non-deterministic logic. 100% reproducible and unit-testable.
"""

from typing import Dict, Any, List


def evaluate_audio_loudness(loudness_lufs: float, target: float = -27.0, tolerance: float = 2.0) -> Dict[str, Any]:
    """
    Evaluates integrated audio loudness against a target LUFS and tolerance.
    Clause A-2.1: Integrated loudness must be target +/- tolerance LUFS.
    """
    min_allowed = target - tolerance
    max_allowed = target + tolerance

    if min_allowed <= loudness_lufs <= max_allowed:
        return {
            "passed": True,
            "severity": None,
            "message": f"Audio loudness {loudness_lufs:.1f} LUFS within target {target:.1f} ± {tolerance:.1f} LUFS.",
            "measured": f"{loudness_lufs:.1f} LUFS",
            "expected": f"{target:.1f} ± {tolerance:.1f} LUFS",
        }
    else:
        diff = loudness_lufs - target
        sign = "+" if diff > 0 else ""
        return {
            "passed": False,
            "severity": "blocker",
            "message": f"Audio loudness deviation: measured {loudness_lufs:.1f} LUFS ({sign}{diff:.1f} LUFS from {target:.1f} target, tolerance ±{tolerance:.1f} LU).",
            "measured": f"{loudness_lufs:.1f} LUFS",
            "expected": f"{target:.1f} ± {tolerance:.1f} LUFS",
        }


def evaluate_audio_true_peak(true_peak_dbtp: float, target_max: float = -2.0) -> Dict[str, Any]:
    """
    Evaluates True Peak audio level against a maximum dBTP limit.
    Clause A-2.2: True Peak of every audio track must not exceed target_max dBTP.
    """
    if true_peak_dbtp <= target_max:
        return {
            "passed": True,
            "severity": None,
            "message": f"Audio True Peak {true_peak_dbtp:.1f} dBTP within maximum limit {target_max:.1f} dBTP.",
            "measured": f"{true_peak_dbtp:.1f} dBTP",
            "expected": f"<= {target_max:.1f} dBTP",
        }
    else:
        return {
            "passed": False,
            "severity": "blocker",
            "message": f"Audio True Peak violation: measured {true_peak_dbtp:.1f} dBTP exceeds maximum limit {target_max:.1f} dBTP.",
            "measured": f"{true_peak_dbtp:.1f} dBTP",
            "expected": f"<= {target_max:.1f} dBTP",
        }


def evaluate_language_readiness(
    audio_tracks: List[Dict[str, Any]],
    timed_text: List[Dict[str, Any]],
    certifications: Dict[str, str],
) -> Dict[str, float]:
    """
    Computes per-language delivery readiness ratio (0.0 to 1.0).
    For each language present in audio_tracks, timed_text, or certifications:
    Score is fraction of 3 conditions satisfied:
      1. Audio track exists
      2. Subtitle track exists
      3. Certification == 'cleared'
    """
    audio_langs = {t.get("language") for t in audio_tracks if t.get("language")}
    sub_langs = {t.get("language") for t in timed_text if t.get("language")}
    cert_langs = set(certifications.keys())

    all_langs = sorted(list(audio_langs | sub_langs | cert_langs))
    readiness = {}

    for lang in all_langs:
        c1 = 1 if lang in audio_langs else 0
        c2 = 1 if lang in sub_langs else 0
        c3 = 1 if certifications.get(lang) == "cleared" else 0
        score = round((c1 + c2 + c3) / 3.0, 3)
        readiness[lang] = score

    return readiness


def evaluate_video_color_primaries(color_primaries: str, target: str = "BT.2020") -> Dict[str, Any]:
    """
    Evaluates video color primaries.
    Clause V-1.3: HDR masters must carry specified primaries (e.g. BT.2020).
    """
    if color_primaries == target:
        return {
            "passed": True,
            "severity": None,
            "message": f"Video color primaries '{color_primaries}' matches required target '{target}'.",
            "measured": color_primaries,
            "expected": target,
        }
    else:
        return {
            "passed": False,
            "severity": "blocker",
            "message": f"Invalid video color primaries: measured '{color_primaries}', expected '{target}'.",
            "measured": color_primaries,
            "expected": target,
        }


def evaluate_timed_text_coverage(subtitle_languages: List[str], audio_languages: List[str]) -> Dict[str, Any]:
    """
    Evaluates subtitle language coverage against delivered audio dub tracks.
    Clause T-4.2: Every delivered audio language requires a matching subtitle track.
    """
    audio_set = set(audio_languages)
    sub_set = set(subtitle_languages)
    missing = sorted(list(audio_set - sub_set))

    if not missing:
        return {
            "passed": True,
            "severity": None,
            "message": f"All {len(audio_languages)} audio languages have matching timed text tracks.",
            "missing_languages": [],
        }
    else:
        return {
            "passed": False,
            "severity": "blocker",
            "message": f"Missing subtitle track(s) for audio language(s): {', '.join(missing)}.",
            "missing_languages": missing,
        }


def evaluate_packaging_naming(naming_pattern_ok: bool) -> Dict[str, Any]:
    """
    Evaluates package component naming conventions.
    Clause P-1.1: Component naming pattern compliance.
    """
    if naming_pattern_ok:
        return {
            "passed": True,
            "severity": None,
            "message": "Package component naming conformed to delivery pattern.",
        }
    else:
        return {
            "passed": False,
            "severity": "warning",
            "message": "Component naming pattern deviation detected in package metadata.",
        }


def evaluate_india_mode_gating(certifications: Dict[str, str], original_language: str) -> Dict[str, Any]:
    """
    Evaluates CBFC regulatory certification gating for Pan-India multi-language releases.
    Rule: Original language certification is required before dubbed versions clear clearance.
    """
    # Absent certificate is not an invented spec value: CBFC gating treats
    # a missing original-language certificate as not cleared, which blocks dubs.
    orig_status = certifications.get(original_language, "pending")
    original_cleared = (orig_status == "cleared")

    return {
        "original_language": original_language,
        "original_status": orig_status,
        "original_cleared": original_cleared,
        "dubs_blocked": not original_cleared,
        "message": (
            f"Original language '{original_language}' certificate is '{orig_status}'."
            if original_cleared
            else f"Original language '{original_language}' certificate is 'pending'; dub clearances are gated."
        ),
    }


_SPEC_OPS = {
    "within": ("target", "tolerance", "field"),
    "max": ("target", "field"),
    "equals": ("target", "field"),
    "language_coverage": ("of", "against"),
}


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_spec(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Collect every spec-shape problem. A clause missing a field required by its
    op is a SPEC ERROR — never substitute a literal and continue.
    """
    problems: List[Dict[str, Any]] = []
    clauses = spec.get("clauses")
    if not isinstance(clauses, list):
        problems.append({
            "clause_id": None,
            "field": "clauses",
            "message": "SPEC ERROR: spec is missing a clauses list.",
        })
        return problems

    if len(clauses) == 0:
        problems.append({
            "clause_id": None,
            "field": "clauses",
            "message": "SPEC ERROR: spec defines no clauses; nothing was evaluated.",
        })
        return problems

    for clause in clauses:
        if not isinstance(clause, dict):
            problems.append({
                "clause_id": None,
                "field": "clauses",
                "message": "SPEC ERROR: spec defines no valid clauses; clause entry is not an object.",
            })
            continue
        clause_id = clause.get("clause_id")
        check = clause.get("check")
        if not isinstance(check, dict):
            problems.append({
                "clause_id": clause_id,
                "field": "check",
                "message": f"SPEC ERROR: clause {clause_id} missing required check object.",
            })
            continue
        op = check.get("op")
        if op is None or op == "":
            problems.append({
                "clause_id": clause_id,
                "field": "op",
                "message": f"SPEC ERROR: clause {clause_id} missing required check field 'op'.",
            })
            continue
        required = _SPEC_OPS.get(op)
        if required is None:
            problems.append({
                "clause_id": clause_id,
                "field": "op",
                "message": f"SPEC ERROR: clause {clause_id} has unsupported check op '{op}'.",
            })
            continue
        for field_name in required:
            if field_name not in check or check[field_name] is None or check[field_name] == "":
                problems.append({
                    "clause_id": clause_id,
                    "field": field_name,
                    "message": (
                        f"SPEC ERROR: clause {clause_id} missing required check field '{field_name}'."
                    ),
                })
    return problems


def _list_section(master: Dict[str, Any], key: str):
    if key not in master:
        return None
    value = master[key]
    if not isinstance(value, list):
        return None
    return value


def _dict_section(master: Dict[str, Any], key: str):
    if key not in master:
        return None
    value = master[key]
    if not isinstance(value, dict):
        return None
    return value


def _reject_with_spec_errors(
    master: Any,
    spec: Any,
    spec_errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    master_id = master.get("master_id", "UNKNOWN") if isinstance(master, dict) else "UNKNOWN"
    spec_id = spec.get("spec_id", "UNKNOWN") if isinstance(spec, dict) else "UNKNOWN"
    findings = [
        {
            "clause_id": err.get("clause_id"),
            "domain": "spec",
            "severity": "blocker",
            "clause_text": "",
            "message": err["message"],
        }
        for err in spec_errors
    ]
    return {
        "master_id": master_id,
        "spec_id": spec_id,
        "verdict": "REJECT",
        "blocker_count": len(spec_errors),
        "warning_count": 0,
        "findings": findings,
        "evaluations": [],
        "india_mode": None,
        "readiness": {},
        "spec_errors": spec_errors,
    }


def evaluate_master_against_spec(master: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates a complete master metadata dictionary against a platform specification dictionary.
    Returns structured finding objects and an overall delivery verdict (PASS / REJECT).
    """
    findings: List[Dict[str, Any]] = []
    evaluations: List[Dict[str, Any]] = []
    blocker_count = 0
    warning_count = 0

    if not isinstance(master, dict):
        return _reject_with_spec_errors(
            master,
            spec,
            [{
                "clause_id": None,
                "field": "master",
                "message": "SPEC ERROR: master must be an object; nothing was evaluated.",
            }],
        )
    if not isinstance(spec, dict):
        return _reject_with_spec_errors(
            master,
            spec,
            [{
                "clause_id": None,
                "field": "spec",
                "message": "SPEC ERROR: spec must be an object; nothing was evaluated.",
            }],
        )

    spec_errors = validate_spec(spec)
    if spec_errors:
        return _reject_with_spec_errors(master, spec, spec_errors)

    clauses = spec.get("clauses", [])

    audio_tracks = _list_section(master, "audio_tracks")
    video = _dict_section(master, "video")
    timed_text = _list_section(master, "timed_text")
    packaging = _dict_section(master, "packaging")
    certifications = _dict_section(master, "certification")

    audio_usable = isinstance(audio_tracks, list) and len(audio_tracks) > 0
    video_usable = isinstance(video, dict) and len(video) > 0
    timed_text_present = isinstance(timed_text, list)
    packaging_usable = isinstance(packaging, dict) and len(packaging) > 0
    cert_usable = isinstance(certifications, dict) and len(certifications) > 0

    if audio_tracks is None:
        audio_tracks = []
    if timed_text is None:
        timed_text = []
    if video is None:
        video = {}
    if packaging is None:
        packaging = {}
    if certifications is None:
        certifications = {}

    audio_langs = [t.get("language") for t in audio_tracks if t.get("language")]
    sub_langs = [t.get("language") for t in timed_text if t.get("language")]

    clauses_by_id = {c.get("clause_id"): c.get("text", "") for c in clauses if "clause_id" in c}

    referenced_audio = False
    referenced_video = False
    referenced_timed_text = False
    referenced_packaging = False
    for clause in clauses:
        domain = clause.get("domain")
        check = clause.get("check") or {}
        if domain == "audio" or check.get("against") == "audio_tracks" or check.get("of") == "audio_tracks":
            referenced_audio = True
        if domain == "video":
            referenced_video = True
        if domain == "timed_text" or check.get("of") == "timed_text" or check.get("against") == "timed_text":
            referenced_timed_text = True
        if domain == "packaging":
            referenced_packaging = True

    india_mode = spec.get("india_mode") or {}

    def record_fail(finding: Dict[str, Any], severity: str) -> None:
        nonlocal blocker_count, warning_count
        findings.append(finding)
        if severity == "blocker":
            blocker_count += 1
        else:
            warning_count += 1

    if referenced_audio and not audio_usable:
        record_fail(
            {
                "clause_id": None,
                "domain": "audio",
                "severity": "blocker",
                "clause_text": "",
                "measured": "missing",
                "expected": "non-empty audio_tracks",
                "message": "Master is missing audio_tracks; audio clauses cannot be evaluated.",
            },
            "blocker",
        )
    if referenced_video and not video_usable:
        record_fail(
            {
                "clause_id": "V-1.3" if any(c.get("clause_id") == "V-1.3" for c in clauses) else None,
                "domain": "video",
                "severity": "blocker",
                "clause_text": "",
                "measured": "missing",
                "expected": "video",
                "message": "Master is missing video; video clauses cannot be evaluated.",
            },
            "blocker",
        )
    if referenced_timed_text and not timed_text_present:
        record_fail(
            {
                "clause_id": None,
                "domain": "timed_text",
                "severity": "blocker",
                "clause_text": "",
                "measured": "missing",
                "expected": "timed_text",
                "message": "Master is missing timed_text; timed-text clauses cannot be evaluated.",
            },
            "blocker",
        )
    if referenced_packaging and not packaging_usable:
        pkg_severity = "blocker"
        for clause in clauses:
            if clause.get("domain") == "packaging":
                pkg_severity = clause.get("severity_on_fail", "blocker")
                break
        record_fail(
            {
                "clause_id": None,
                "domain": "packaging",
                "severity": pkg_severity,
                "clause_text": "",
                "measured": "missing",
                "expected": "packaging",
                "message": "Master is missing packaging; packaging clauses cannot be evaluated.",
            },
            pkg_severity,
        )
    if india_mode and not cert_usable:
        record_fail(
            {
                "clause_id": None,
                "domain": "certification",
                "severity": "blocker",
                "clause_text": "",
                "measured": "missing",
                "expected": "certification",
                "message": "Master is missing certification; India-mode gating cannot be evaluated.",
            },
            "blocker",
        )

    skip_audio = referenced_audio and not audio_usable
    skip_video = referenced_video and not video_usable
    skip_timed_text = referenced_timed_text and (not timed_text_present or not audio_usable)
    skip_packaging = referenced_packaging and not packaging_usable

    for clause in clauses:
        clause_id = clause.get("clause_id")
        domain = clause.get("domain")
        check = clause.get("check", {})
        severity_on_fail = clause.get("severity_on_fail", "blocker")
        clause_text = clauses_by_id.get(clause_id, "")

        if domain == "audio" and check.get("op") == "within":
            if skip_audio:
                continue
            target = check["target"]
            tolerance = check["tolerance"]
            for track in audio_tracks:
                lang = track.get("language", "unknown")
                loudness = track.get("integrated_loudness_lufs")
                if not _is_number(loudness):
                    record_fail(
                        {
                            "clause_id": clause_id,
                            "domain": domain,
                            "severity": severity_on_fail,
                            "clause_text": clause_text,
                            "language": lang,
                            "measured": repr(loudness),
                            "expected": "numeric integrated_loudness_lufs",
                            "message": (
                                f"[{clause_id}] {lang} audio: cannot evaluate loudness; "
                                f"integrated_loudness_lufs is {repr(loudness)}, expected a number."
                            ),
                        },
                        severity_on_fail,
                    )
                    continue
                res = evaluate_audio_loudness(loudness, target=target, tolerance=tolerance)
                diff = loudness - target
                evaluations.append({
                    "clause_id": clause_id,
                    "domain": domain,
                    "passed": res["passed"],
                    "result": "pass" if res["passed"] else "fail",
                    "language": lang,
                    "loudness_lufs": loudness,
                    "target_lufs": target,
                    "tolerance_lu": tolerance,
                    "loudness_deviation_lufs": round(diff, 1),
                })
                if not res["passed"]:
                    record_fail(
                        {
                            "clause_id": clause_id,
                            "domain": domain,
                            "severity": severity_on_fail,
                            "clause_text": clause_text,
                            "language": lang,
                            "measured": res["measured"],
                            "expected": res["expected"],
                            "message": f"[{clause_id}] {lang} audio: {res['message']}",
                        },
                        severity_on_fail,
                    )

        elif domain == "audio" and check.get("op") == "max":
            if skip_audio:
                continue
            target_max = check["target"]
            for track in audio_tracks:
                lang = track.get("language", "unknown")
                tp = track.get("true_peak_dbtp")
                if not _is_number(tp):
                    record_fail(
                        {
                            "clause_id": clause_id,
                            "domain": domain,
                            "severity": severity_on_fail,
                            "clause_text": clause_text,
                            "language": lang,
                            "measured": repr(tp),
                            "expected": "numeric true_peak_dbtp",
                            "message": (
                                f"[{clause_id}] {lang} audio: cannot evaluate true peak; "
                                f"true_peak_dbtp is {repr(tp)}, expected a number."
                            ),
                        },
                        severity_on_fail,
                    )
                    continue
                res = evaluate_audio_true_peak(tp, target_max=target_max)
                evaluations.append({
                    "clause_id": clause_id,
                    "domain": domain,
                    "passed": res["passed"],
                    "result": "pass" if res["passed"] else "fail",
                    "language": lang,
                    "true_peak_dbtp": tp,
                    "target_max_dbtp": target_max,
                })
                if not res["passed"]:
                    record_fail(
                        {
                            "clause_id": clause_id,
                            "domain": domain,
                            "severity": severity_on_fail,
                            "clause_text": clause_text,
                            "language": lang,
                            "measured": res["measured"],
                            "expected": res["expected"],
                            "message": f"[{clause_id}] {lang} audio: {res['message']}",
                        },
                        severity_on_fail,
                    )

        elif domain == "video" and check.get("op") == "equals":
            if skip_video:
                continue
            target = check["target"]
            primaries = video.get("color_primaries")
            if not primaries:
                record_fail(
                    {
                        "clause_id": clause_id,
                        "domain": domain,
                        "severity": severity_on_fail,
                        "clause_text": clause_text,
                        "measured": repr(primaries),
                        "expected": target,
                        "message": (
                            f"[{clause_id}] Video: cannot evaluate color primaries; "
                            "video.color_primaries is missing."
                        ),
                    },
                    severity_on_fail,
                )
                continue
            res = evaluate_video_color_primaries(primaries, target=target)
            evaluations.append({
                "clause_id": clause_id,
                "domain": domain,
                "passed": res["passed"],
                "result": "pass" if res["passed"] else "fail",
            })
            if not res["passed"]:
                record_fail(
                    {
                        "clause_id": clause_id,
                        "domain": domain,
                        "severity": severity_on_fail,
                        "clause_text": clause_text,
                        "measured": res["measured"],
                        "expected": res["expected"],
                        "message": f"[{clause_id}] Video: {res['message']}",
                    },
                    severity_on_fail,
                )

        elif domain == "timed_text" and check.get("op") == "language_coverage":
            if skip_timed_text:
                continue
            res = evaluate_timed_text_coverage(sub_langs, audio_langs)
            evaluations.append({
                "clause_id": clause_id,
                "domain": domain,
                "passed": res["passed"],
                "result": "pass" if res["passed"] else "fail",
            })
            if not res["passed"]:
                record_fail(
                    {
                        "clause_id": clause_id,
                        "domain": domain,
                        "severity": severity_on_fail,
                        "clause_text": clause_text,
                        "measured": res.get("measured", f"missing {', '.join(res['missing_languages'])}"),
                        "expected": res.get("expected", f"subtitles for {', '.join(sorted(audio_langs))}"),
                        "missing_languages": res["missing_languages"],
                        "message": f"[{clause_id}] Timed Text: {res['message']}",
                    },
                    severity_on_fail,
                )

        elif domain == "packaging" and check.get("op") == "equals":
            if skip_packaging:
                continue
            if "naming_pattern_ok" not in packaging:
                record_fail(
                    {
                        "clause_id": clause_id,
                        "domain": domain,
                        "severity": severity_on_fail,
                        "clause_text": clause_text,
                        "measured": "missing",
                        "expected": "naming_pattern_ok",
                        "message": (
                            f"[{clause_id}] Packaging: cannot evaluate naming; "
                            "packaging.naming_pattern_ok is missing."
                        ),
                    },
                    severity_on_fail,
                )
                continue
            pattern_ok = packaging.get("naming_pattern_ok")
            res = evaluate_packaging_naming(bool(pattern_ok))
            evaluations.append({
                "clause_id": clause_id,
                "domain": domain,
                "passed": res["passed"],
                "result": "pass" if res["passed"] else "fail",
            })
            if not res["passed"]:
                record_fail(
                    {
                        "clause_id": clause_id,
                        "domain": domain,
                        "severity": severity_on_fail,
                        "clause_text": clause_text,
                        "measured": "invalid pattern",
                        "expected": "valid naming pattern",
                        "message": f"[{clause_id}] Packaging: {res['message']}",
                    },
                    severity_on_fail,
                )

    india_report = None
    if india_mode and cert_usable:
        orig_lang = "ta-IN"
        for t in audio_tracks:
            if t.get("role") == "original":
                orig_lang = t.get("language", "ta-IN")
                break
        india_report = evaluate_india_mode_gating(certifications, orig_lang)
    elif india_mode and not cert_usable:
        india_report = {
            "original_language": None,
            "original_status": "missing",
            "original_cleared": False,
            "dubs_blocked": True,
            "message": "Certification block is missing; India-mode gating cannot be evaluated.",
        }

    readiness = evaluate_language_readiness(audio_tracks, timed_text, certifications)

    verdict = "REJECT" if blocker_count > 0 else "PASS"

    return {
        "master_id": master.get("master_id", "UNKNOWN"),
        "spec_id": spec.get("spec_id", "UNKNOWN"),
        "verdict": verdict,
        "blocker_count": blocker_count,
        "warning_count": warning_count,
        "findings": findings,
        "evaluations": evaluations,
        "india_mode": india_report,
        "readiness": readiness,
        "spec_errors": [],
    }

