"""
Read media files into master JSON. Loudness is measured; intent fields are declared.

ffmpeg/ffprobe are system binaries. This module may use subprocess; check_engine and
orchestrator must not. Video is not probed this pass — colour/resolution/frame rate
are declared inputs, same as packaging and certification.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

SINE_DURATION_S = 6
SAMPLE_RATE = 48000
VOLUME_FAIL_DB = -3.0
VOLUME_PASS_DB = -6.0
LUFS_ANCHOR_FAIL = -24.1
LUFS_ANCHOR_PASS = -27.1

_INTEGRATED_RE = re.compile(r"I:\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*LUFS", re.IGNORECASE)
_TRUE_PEAK_RE = re.compile(r"Peak:\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*dBFS", re.IGNORECASE)


class MeasureError(Exception):
    """ffmpeg/ffprobe failed, or output could not be parsed. Never a silent default."""


@dataclass
class DeclaredFields:
    master_id: Optional[str] = None
    title: Optional[str] = None
    language: Optional[str] = None
    role: Optional[str] = None
    timed_text_languages: Optional[List[str]] = None
    certification: Optional[Dict[str, str]] = None
    naming_pattern_ok: Optional[bool] = None
    color_primaries: Optional[str] = None
    transfer: Optional[str] = None
    resolution: Optional[str] = None
    frame_rate: Optional[float] = None


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MeasureError(f"{name} is not installed or not on PATH")
    return path


def generate_sine_wav(output_path: str, volume_db: float, duration_s: float = SINE_DURATION_S) -> None:
    ffmpeg = require_binary("ffmpeg")
    sine = f"sine=frequency=1000:duration={duration_s}:sample_rate={SAMPLE_RATE}"
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        sine,
        "-af",
        f"volume={volume_db}dB",
        "-c:a",
        "pcm_s24le",
        output_path,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise MeasureError(
            f"ffmpeg generate failed (exit {completed.returncode}): "
            f"{(completed.stderr or completed.stdout or '').strip() or 'no output'}"
        )


def probe_media(path: str) -> Dict[str, Any]:
    ffprobe = require_binary("ffprobe")
    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        path,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise MeasureError(
            f"ffprobe failed (exit {completed.returncode}): "
            f"{(completed.stderr or completed.stdout or '').strip() or 'no output'}"
        )
    raw = completed.stdout.strip()
    if not raw:
        raise MeasureError("ffprobe produced empty stdout")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MeasureError(f"ffprobe JSON is unparseable: {exc}") from exc
    if not isinstance(payload, dict):
        raise MeasureError("ffprobe JSON is not an object")
    return payload


def parse_ebur128_summary(ffmpeg_output: str) -> Dict[str, float]:
    if not ffmpeg_output or not ffmpeg_output.strip():
        raise MeasureError("ffmpeg ebur128 produced empty output")
    summary_idx = ffmpeg_output.lower().rfind("summary:")
    if summary_idx < 0:
        raise MeasureError("ffmpeg ebur128 output is missing a Summary block")
    summary = ffmpeg_output[summary_idx:]
    integrated = _INTEGRATED_RE.search(summary)
    peak = _TRUE_PEAK_RE.search(summary)
    if integrated is None:
        raise MeasureError("ffmpeg ebur128 Summary is missing a parseable Integrated loudness (I: … LUFS)")
    if peak is None:
        raise MeasureError("ffmpeg ebur128 Summary is missing a parseable True peak (Peak: … dBFS)")
    try:
        lufs = float(integrated.group(1))
        dbtp = float(peak.group(1))
    except ValueError as exc:
        raise MeasureError(f"ffmpeg ebur128 Summary contains an unparseable number: {exc}") from exc
    return {"integrated_loudness_lufs": lufs, "true_peak_dbtp": dbtp}


def measure_loudness(path: str) -> Tuple[Dict[str, float], str]:
    ffmpeg = require_binary("ffmpeg")
    cmd = [ffmpeg, "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    combined = (completed.stderr or "") + ("\n" + completed.stdout if completed.stdout else "")
    if completed.returncode != 0:
        raise MeasureError(
            f"ffmpeg ebur128 failed (exit {completed.returncode}): "
            f"{combined.strip() or 'no output'}"
        )
    return parse_ebur128_summary(combined), combined


def _audio_streams(probe: Dict[str, Any]) -> List[Dict[str, Any]]:
    streams = probe.get("streams")
    if not isinstance(streams, list):
        return []
    audio = []
    for stream in streams:
        if isinstance(stream, dict) and stream.get("codec_type") == "audio":
            audio.append(stream)
    return audio


def _channel_label(stream: Dict[str, Any]) -> Optional[str]:
    layout = stream.get("channel_layout")
    if isinstance(layout, str) and layout.strip():
        return layout.strip()
    channels = stream.get("channels")
    if isinstance(channels, int):
        return str(channels)
    if isinstance(channels, str) and channels.strip():
        return channels.strip()
    return None


def _stream_language(stream: Dict[str, Any]) -> Optional[str]:
    tags = stream.get("tags")
    if not isinstance(tags, dict):
        return None
    for key in ("language", "LANGUAGE", "lang"):
        value = tags.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def undeclared_notices(declared: DeclaredFields) -> List[str]:
    notices: List[str] = []
    if declared.color_primaries is None:
        notices.append(
            "V-1.3 cannot pass: video.color_primaries was not declared "
            "(this pass does not probe video; ffprobe is not used to invent colour)."
        )
    if declared.transfer is None:
        notices.append("video.transfer was not declared (not probed this pass).")
    if declared.resolution is None:
        notices.append("video.resolution was not declared (not probed this pass).")
    if declared.frame_rate is None:
        notices.append("video.frame_rate was not declared (not probed this pass).")
    if declared.naming_pattern_ok is None:
        notices.append("P-1.1 cannot be evaluated as intended: packaging.naming_pattern_ok was not declared.")
    if declared.timed_text_languages is None:
        notices.append("T-4.2 cannot pass: timed_text was not declared.")
    if declared.certification is None:
        notices.append("certification was not declared (India-mode gating will treat langs as pending).")
    return notices


def build_master(
    probe: Dict[str, Any],
    measurements: Dict[str, float],
    declared: DeclaredFields,
) -> Dict[str, Any]:
    if "integrated_loudness_lufs" not in measurements or "true_peak_dbtp" not in measurements:
        raise MeasureError("measurements dict is missing integrated_loudness_lufs or true_peak_dbtp")

    master: Dict[str, Any] = {}
    if declared.master_id is not None:
        master["master_id"] = declared.master_id
    if declared.title is not None:
        master["title"] = declared.title

    video: Dict[str, Any] = {}
    if declared.resolution is not None:
        video["resolution"] = declared.resolution
    if declared.frame_rate is not None:
        video["frame_rate"] = declared.frame_rate
    if declared.color_primaries is not None:
        video["color_primaries"] = declared.color_primaries
    if declared.transfer is not None:
        video["transfer"] = declared.transfer
    if video:
        master["video"] = video

    audio_tracks: List[Dict[str, Any]] = []
    streams = _audio_streams(probe)
    if not streams:
        raise MeasureError("ffprobe reported no audio streams")

    fmt = probe.get("format") if isinstance(probe.get("format"), dict) else {}
    duration = fmt.get("duration")

    for stream in streams:
        track: Dict[str, Any] = {
            "integrated_loudness_lufs": measurements["integrated_loudness_lufs"],
            "true_peak_dbtp": measurements["true_peak_dbtp"],
        }
        channels = _channel_label(stream)
        if channels is not None:
            track["channels"] = channels
        sample_rate = stream.get("sample_rate")
        if sample_rate is not None:
            try:
                track["sample_rate"] = int(sample_rate)
            except (TypeError, ValueError):
                pass
        if isinstance(duration, str) and duration.strip():
            try:
                track["duration_s"] = float(duration)
            except ValueError:
                pass
        language = _stream_language(stream)
        if language is None and declared.language is not None:
            language = declared.language
        if language is not None:
            track["language"] = language
        if declared.role is not None:
            track["role"] = declared.role
        audio_tracks.append(track)

    master["audio_tracks"] = audio_tracks

    if declared.timed_text_languages is not None:
        master["timed_text"] = [
            {"language": lang, "type": "subtitle", "format": "IMSC1"}
            for lang in declared.timed_text_languages
        ]
    if declared.naming_pattern_ok is not None:
        master["packaging"] = {"naming_pattern_ok": declared.naming_pattern_ok}
    if declared.certification is not None:
        master["certification"] = dict(declared.certification)
    return master


def assemble_from_file(media_path: str, declared: DeclaredFields) -> Tuple[Dict[str, Any], str, List[str]]:
    probe = probe_media(media_path)
    measurements, raw = measure_loudness(media_path)
    master = build_master(probe, measurements, declared)
    return master, raw, undeclared_notices(declared)


def _parse_cert(raw: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise MeasureError(f"cert declaration must be lang=status, got {part!r}")
        lang, status = part.split("=", 1)
        lang, status = lang.strip(), status.strip()
        if not lang or not status:
            raise MeasureError(f"cert declaration must be lang=status, got {part!r}")
        out[lang] = status
    if not out:
        raise MeasureError("cert declaration is empty")
    return out


def _parse_bool(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise MeasureError(f"boolean declaration must be true or false, got {raw!r}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure a media file into master JSON and optionally evaluate it."
    )
    parser.add_argument("media", nargs="?", help="Path to an audio file (pcm WAV).")
    parser.add_argument(
        "--generate",
        choices=("fail", "pass"),
        help="Generate a lavfi 1 kHz sine WAV (fail≈-24.1 LUFS, pass≈-27.1 LUFS) and measure it.",
    )
    parser.add_argument("--evaluate", action="store_true", help="Run evaluate_master_against_spec on the result.")
    parser.add_argument("--spec", help="Spec JSON path (default: data/specs/streamone.json).")
    parser.add_argument("--master-id", default=None)
    parser.add_argument("--title", default=None)
    parser.add_argument("--declare-language", default=None)
    parser.add_argument("--declare-role", default=None)
    parser.add_argument("--declare-timed-text", default=None, help="Comma-separated languages.")
    parser.add_argument("--declare-cert", default=None, help="Comma-separated lang=status pairs.")
    parser.add_argument("--declare-naming-ok", default=None)
    parser.add_argument("--declare-color-primaries", default=None)
    parser.add_argument("--declare-transfer", default=None)
    parser.add_argument("--declare-resolution", default=None)
    parser.add_argument("--declare-frame-rate", default=None)
    return parser


def declared_from_args(args: argparse.Namespace) -> DeclaredFields:
    timed = None
    if args.declare_timed_text is not None:
        timed = [p.strip() for p in args.declare_timed_text.split(",") if p.strip()]
    cert = None
    if args.declare_cert is not None:
        cert = _parse_cert(args.declare_cert)
    naming = None
    if args.declare_naming_ok is not None:
        naming = _parse_bool(args.declare_naming_ok)
    frame_rate = None
    if args.declare_frame_rate is not None:
        try:
            frame_rate = float(args.declare_frame_rate)
        except ValueError as exc:
            raise MeasureError(f"frame_rate is not a number: {args.declare_frame_rate!r}") from exc
    return DeclaredFields(
        master_id=args.master_id,
        title=args.title,
        language=args.declare_language,
        role=args.declare_role,
        timed_text_languages=timed,
        certification=cert,
        naming_pattern_ok=naming,
        color_primaries=args.declare_color_primaries,
        transfer=args.declare_transfer,
        resolution=args.declare_resolution,
        frame_rate=frame_rate,
    )


def _default_spec_path() -> str:
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    return os.path.join(base, "data", "specs", "streamone.json")


def run_cli(argv: Optional[List[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if not args.media and not args.generate:
        parser.error("provide a media path or --generate fail|pass")

    declared = declared_from_args(args)
    tmp_dir = None
    media_path = args.media
    try:
        if args.generate:
            tmp_dir = tempfile.mkdtemp(prefix="firstpass-measure-")
            media_path = os.path.join(tmp_dir, f"{args.generate}.wav")
            volume = VOLUME_FAIL_DB if args.generate == "fail" else VOLUME_PASS_DB
            generate_sine_wav(media_path, volume)
            print(f"Generated {media_path} (lavfi sine, volume={volume}dB, pcm_s24le)", file=sys.stderr)

        assert media_path is not None
        master, raw_ebur128, notices = assemble_from_file(media_path, declared)

        print("=== ffmpeg ebur128 ===")
        summary_at = raw_ebur128.lower().rfind("summary:")
        print(raw_ebur128[summary_at:].rstrip() if summary_at >= 0 else raw_ebur128.rstrip())
        print("=== master JSON ===")
        print(json.dumps(master, indent=2))
        if notices:
            print("=== undeclared fields ===")
            for notice in notices:
                print(notice)

        if args.evaluate:
            from agents.check_engine import evaluate_master_against_spec

            spec_path = os.path.abspath(args.spec) if args.spec else _default_spec_path()
            with open(spec_path, "r", encoding="utf-8") as handle:
                spec = json.load(handle)
            report = evaluate_master_against_spec(master, spec)
            print("=== engine verdict ===")
            print(f"master_id: {report.get('master_id')}")
            print(f"verdict: {report.get('verdict')}")
            print(f"blocker_count: {report.get('blocker_count')}")
            print(f"warning_count: {report.get('warning_count')}")
            for finding in report.get("findings") or []:
                print(
                    f"  [{finding.get('severity')}] {finding.get('clause_id')}: {finding.get('message')}"
                )
        return 0
    except MeasureError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run_cli())
