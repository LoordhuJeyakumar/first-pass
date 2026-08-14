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


def evaluate_master_against_spec(master: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Evaluates a complete master metadata dictionary against a platform specification dictionary.
    Returns structured finding objects and an overall delivery verdict (PASS / REJECT).
    """
    findings = []
    evaluations = []
    blocker_count = 0
    warning_count = 0

    clauses = spec.get("clauses", [])
    
    # Extract master components
    audio_tracks = master.get("audio_tracks", [])
    video = master.get("video", {})
    timed_text = master.get("timed_text", [])
    packaging = master.get("packaging", {})
    certifications = master.get("certification", {})

    audio_langs = [t.get("language") for t in audio_tracks if t.get("language")]
    sub_langs = [t.get("language") for t in timed_text if t.get("language")]

    clauses_by_id = {c.get("clause_id"): c.get("text", "") for c in clauses if "clause_id" in c}

    for clause in clauses:
        clause_id = clause.get("clause_id")
        domain = clause.get("domain")
        check = clause.get("check", {})
        severity_on_fail = clause.get("severity_on_fail", "blocker")
        clause_text = clauses_by_id.get(clause_id, "")

        # Audio Loudness Check
        if domain == "audio" and check.get("op") == "within":
            target = check.get("target", -27.0)
            tolerance = check.get("tolerance", 2.0)
            for track in audio_tracks:
                lang = track.get("language", "unknown")
                loudness = track.get("integrated_loudness_lufs")
                if loudness is not None:
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
                        "loudness_deviation_lufs": round(diff, 1),
                    })
                    if not res["passed"]:
                        finding = {
                            "clause_id": clause_id,
                            "domain": domain,
                            "severity": severity_on_fail,
                            "clause_text": clause_text,
                            "language": lang,
                            "measured": res["measured"],
                            "expected": res["expected"],
                            "message": f"[{clause_id}] {lang} audio: {res['message']}",
                        }
                        findings.append(finding)
                        if severity_on_fail == "blocker":
                            blocker_count += 1
                        else:
                            warning_count += 1

        # Audio True Peak Check
        elif domain == "audio" and check.get("op") in ("max", "lte"):
            target_max = check.get("target", -2.0)
            for track in audio_tracks:
                lang = track.get("language", "unknown")
                tp = track.get("true_peak_dbtp")
                if tp is not None:
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
                        finding = {
                            "clause_id": clause_id,
                            "domain": domain,
                            "severity": severity_on_fail,
                            "clause_text": clause_text,
                            "language": lang,
                            "measured": res["measured"],
                            "expected": res["expected"],
                            "message": f"[{clause_id}] {lang} audio: {res['message']}",
                        }
                        findings.append(finding)
                        if severity_on_fail == "blocker":
                            blocker_count += 1
                        else:
                            warning_count += 1

        # Video Primaries Check
        elif domain == "video" and check.get("op") == "equals":
            target = check.get("target", "BT.2020")
            primaries = video.get("color_primaries")
            if primaries:
                res = evaluate_video_color_primaries(primaries, target=target)
                evaluations.append({
                    "clause_id": clause_id,
                    "domain": domain,
                    "passed": res["passed"],
                    "result": "pass" if res["passed"] else "fail",
                })
                if not res["passed"]:
                    finding = {
                        "clause_id": clause_id,
                        "domain": domain,
                        "severity": severity_on_fail,
                        "clause_text": clause_text,
                        "measured": res["measured"],
                        "expected": res["expected"],
                        "message": f"[{clause_id}] Video: {res['message']}",
                    }
                    findings.append(finding)
                    if severity_on_fail == "blocker":
                        blocker_count += 1
                    else:
                        warning_count += 1

        # Timed Text Language Coverage
        elif domain == "timed_text" and check.get("op") == "language_coverage":
            res = evaluate_timed_text_coverage(sub_langs, audio_langs)
            evaluations.append({
                "clause_id": clause_id,
                "domain": domain,
                "passed": res["passed"],
                "result": "pass" if res["passed"] else "fail",
            })
            if not res["passed"]:
                finding = {
                    "clause_id": clause_id,
                    "domain": domain,
                    "severity": severity_on_fail,
                    "clause_text": clause_text,
                    "measured": res.get("measured", f"missing {', '.join(res['missing_languages'])}"),
                    "expected": res.get("expected", f"subtitles for {', '.join(sorted(audio_langs))}"),
                    "missing_languages": res["missing_languages"],
                    "message": f"[{clause_id}] Timed Text: {res['message']}",
                }
                findings.append(finding)
                if severity_on_fail == "blocker":
                    blocker_count += 1
                else:
                    warning_count += 1

        # Packaging Naming
        elif domain == "packaging" and check.get("op") == "equals":
            pattern_ok = packaging.get("naming_pattern_ok", True)
            res = evaluate_packaging_naming(pattern_ok)
            evaluations.append({
                "clause_id": clause_id,
                "domain": domain,
                "passed": res["passed"],
                "result": "pass" if res["passed"] else "fail",
            })
            if not res["passed"]:
                finding = {
                    "clause_id": clause_id,
                    "domain": domain,
                    "severity": severity_on_fail,
                    "clause_text": clause_text,
                    "measured": "invalid pattern",
                    "expected": "valid naming pattern",
                    "message": f"[{clause_id}] Packaging: {res['message']}",
                }
                findings.append(finding)
                if severity_on_fail == "blocker":
                    blocker_count += 1
                else:
                    warning_count += 1

    # Check India Mode Gating if present
    india_mode = spec.get("india_mode", {})
    india_report = None
    if india_mode and certifications:
        # Find original language track
        orig_lang = "ta-IN"
        for t in audio_tracks:
            if t.get("role") == "original":
                orig_lang = t.get("language", "ta-IN")
                break
        india_report = evaluate_india_mode_gating(certifications, orig_lang)

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
    }

