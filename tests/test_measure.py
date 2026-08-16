"""Tests for audio-only measurement adapter (ebur128 parse + ffprobe map)."""

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.check_engine import evaluate_master_against_spec
from agents.measure import (
    LUFS_ANCHOR_FAIL,
    LUFS_ANCHOR_PASS,
    SINE_DURATION_S,
    VOLUME_FAIL_DB,
    VOLUME_PASS_DB,
    DeclaredFields,
    MeasureError,
    assemble_from_file,
    build_master,
    generate_sine_wav,
    parse_ebur128_summary,
    probe_media,
    require_binary,
    run_cli,
    undeclared_notices,
)

FFMPEG_MISSING = shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None
NEEDS_FFMPEG = pytest.mark.skipif(FFMPEG_MISSING, reason="ffmpeg not installed")

SPEC_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "specs", "streamone.json")

EBUR128_FIXTURE = """
[Parsed_ebur128_0 @ 0x0] t: 5.99998
[Parsed_ebur128_0 @ 0x0] Summary:

  Integrated loudness:
    I:         -24.1 LUFS
    Threshold: -34.2 LUFS

  Loudness range:
    LRA:         0.0 LU
    Threshold: -44.2 LUFS
    LRA low:   -24.1 LUFS
    LRA high:  -24.1 LUFS

  True peak:
    Peak:       -3.0 dBFS
"""

FFPROBE_AUDIO_ONLY = {
    "streams": [
        {
            "codec_type": "audio",
            "codec_name": "pcm_s24le",
            "sample_rate": "48000",
            "channels": 1,
            "channel_layout": "mono",
        }
    ],
    "format": {"duration": "6.000000"},
}

FFPROBE_WITH_LANG = {
    "streams": [
        {
            "codec_type": "audio",
            "codec_name": "pcm_s24le",
            "sample_rate": "48000",
            "channels": 2,
            "channel_layout": "stereo",
            "tags": {"language": "ta-IN"},
        }
    ],
    "format": {"duration": "6.000000"},
}


def _full_declared() -> DeclaredFields:
    return DeclaredFields(
        master_id="STRM-MEAS-001",
        title="Measured sine",
        language="ta-IN",
        role="original",
        timed_text_languages=["ta-IN"],
        certification={"ta-IN": "cleared"},
        naming_pattern_ok=True,
        color_primaries="BT.2020",
        transfer="PQ",
        resolution="3840x2160",
        frame_rate=24.0,
    )


def _streamone_spec():
    with open(SPEC_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_parse_ebur128_summary_reads_i_and_peak():
    parsed = parse_ebur128_summary(EBUR128_FIXTURE)
    assert parsed["integrated_loudness_lufs"] == pytest.approx(-24.1)
    assert parsed["true_peak_dbtp"] == pytest.approx(-3.0)


def test_parse_ebur128_missing_summary_is_error():
    with pytest.raises(MeasureError, match="Summary"):
        parse_ebur128_summary("Integrated loudness:\n    I: -24.1 LUFS\n  Peak: -3.0 dBFS\n")


def test_parse_ebur128_unparseable_number_is_error():
    blob = "Summary:\n  Integrated loudness:\n    I: not-a-number LUFS\n  True peak:\n    Peak: -3.0 dBFS\n"
    with pytest.raises(MeasureError, match="Integrated loudness"):
        parse_ebur128_summary(blob)


def test_parse_ebur128_missing_peak_is_error():
    blob = "Summary:\n  Integrated loudness:\n    I: -24.1 LUFS\n"
    with pytest.raises(MeasureError, match="True peak"):
        parse_ebur128_summary(blob)


def test_build_master_omits_language_when_tags_and_declaration_absent():
    master = build_master(
        FFPROBE_AUDIO_ONLY,
        {"integrated_loudness_lufs": -24.1, "true_peak_dbtp": -3.0},
        DeclaredFields(),
    )
    assert "language" not in master["audio_tracks"][0]
    assert master["audio_tracks"][0]["channels"] == "mono"
    assert "video" not in master
    assert "packaging" not in master
    assert "timed_text" not in master
    assert "certification" not in master


def test_build_master_uses_stream_language_tag():
    master = build_master(
        FFPROBE_WITH_LANG,
        {"integrated_loudness_lufs": -27.1, "true_peak_dbtp": -6.0},
        DeclaredFields(),
    )
    assert master["audio_tracks"][0]["language"] == "ta-IN"


def test_build_master_declared_fields_only_when_provided():
    master = build_master(
        FFPROBE_AUDIO_ONLY,
        {"integrated_loudness_lufs": -27.1, "true_peak_dbtp": -6.0},
        _full_declared(),
    )
    assert master["video"]["color_primaries"] == "BT.2020"
    assert master["video"]["transfer"] == "PQ"
    assert master["video"]["resolution"] == "3840x2160"
    assert master["video"]["frame_rate"] == 24.0
    assert master["packaging"]["naming_pattern_ok"] is True
    assert master["timed_text"][0]["language"] == "ta-IN"
    assert master["certification"]["ta-IN"] == "cleared"
    assert master["audio_tracks"][0]["role"] == "original"
    assert master["audio_tracks"][0]["language"] == "ta-IN"


def test_undeclared_color_primaries_names_v13():
    notices = undeclared_notices(DeclaredFields())
    assert any("V-1.3" in n and "color_primaries" in n for n in notices)


def test_build_master_rejects_missing_measurement_keys():
    with pytest.raises(MeasureError, match="measurements"):
        build_master(FFPROBE_AUDIO_ONLY, {}, DeclaredFields())


def test_channel_label_normalises_layout_and_count():
    wav_style = build_master(
        FFPROBE_AUDIO_ONLY,
        {"integrated_loudness_lufs": -24.1, "true_peak_dbtp": -3.0},
        DeclaredFields(),
    )
    mxf_style = build_master(
        {
            "streams": [
                {
                    "codec_type": "audio",
                    "codec_name": "pcm_s24le",
                    "sample_rate": "48000",
                    "channels": 2,
                }
            ],
            "format": {"duration": "6.000000", "format_name": "mxf"},
        },
        {"integrated_loudness_lufs": -24.1, "true_peak_dbtp": -3.0},
        DeclaredFields(),
    )
    assert wav_style["audio_tracks"][0]["channels"] == "mono"
    assert mxf_style["audio_tracks"][0]["channels"] == "stereo"


def _encode_delivery(path: str, fmt: str, volume_db: float) -> None:
    ffmpeg = require_binary("ffmpeg")
    duration = SINE_DURATION_S
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=1920x1080:rate=25:duration={duration}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=1000:duration={duration}:sample_rate=48000",
        "-af",
        f"volume={volume_db}dB",
        "-c:v",
        "mpeg2video",
        "-b:v",
        "20M",
        "-c:a",
        "pcm_s24le",
        "-f",
        fmt,
        path,
    ]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise AssertionError(
            f"ffmpeg encode {fmt} failed (exit {completed.returncode}): "
            f"{(completed.stderr or completed.stdout or '').strip() or 'no output'}"
        )


@NEEDS_FFMPEG
def test_fail_clip_measures_near_minus_24_1(tmp_path):
    wav = str(tmp_path / "fail.wav")
    generate_sine_wav(wav, VOLUME_FAIL_DB)
    master, raw, _ = assemble_from_file(wav, _full_declared())
    lufs = master["audio_tracks"][0]["integrated_loudness_lufs"]
    assert lufs == pytest.approx(LUFS_ANCHOR_FAIL, abs=0.3)
    assert "true_peak_dbtp" in master["audio_tracks"][0]
    assert "Summary:" in raw or "summary:" in raw.lower()
    report = evaluate_master_against_spec(master, _streamone_spec())
    a21 = [f for f in report["findings"] if f.get("clause_id") == "A-2.1"]
    assert a21, "fail clip must produce an A-2.1 finding"
    assert a21[0]["severity"] == "blocker"
    assert report["verdict"] == "REJECT"


@NEEDS_FFMPEG
def test_pass_clip_measures_near_minus_27_1(tmp_path):
    wav = str(tmp_path / "pass.wav")
    generate_sine_wav(wav, VOLUME_PASS_DB)
    master, _, _ = assemble_from_file(wav, _full_declared())
    lufs = master["audio_tracks"][0]["integrated_loudness_lufs"]
    assert lufs == pytest.approx(LUFS_ANCHOR_PASS, abs=0.3)
    report = evaluate_master_against_spec(master, _streamone_spec())
    a21 = [f for f in report["findings"] if f.get("clause_id") == "A-2.1"]
    assert not a21
    assert report["verdict"] == "PASS"


@NEEDS_FFMPEG
def test_cli_without_declared_color_prints_v13(tmp_path, capsys):
    wav = str(tmp_path / "cli.wav")
    generate_sine_wav(wav, VOLUME_PASS_DB)
    code = run_cli([wav, "--evaluate", "--master-id", "CLI-NO-VIDEO"])
    captured = capsys.readouterr()
    assert code == 0
    assert "V-1.3" in captured.out
    assert "color_primaries" in captured.out


@NEEDS_FFMPEG
@pytest.mark.parametrize("fmt,ext,format_token", [("mxf", "mxf", "mxf"), ("mov", "mov", "mov")])
def test_delivery_container_lufs_matches_wav_and_does_not_invent_video(tmp_path, fmt, ext, format_token):
    wav = str(tmp_path / "same-level.wav")
    container = str(tmp_path / f"same-level.{ext}")
    generate_sine_wav(wav, VOLUME_FAIL_DB)
    _encode_delivery(container, fmt, VOLUME_FAIL_DB)

    probe = probe_media(container)
    fmt_name = (probe.get("format") or {}).get("format_name") or ""
    assert format_token in fmt_name
    video = next(
        (s for s in probe.get("streams") or [] if isinstance(s, dict) and s.get("codec_type") == "video"),
        {},
    )
    assert not video.get("color_primaries")

    wav_master, _, _ = assemble_from_file(wav, DeclaredFields())
    container_master, _, notices = assemble_from_file(container, DeclaredFields())
    wav_lufs = wav_master["audio_tracks"][0]["integrated_loudness_lufs"]
    container_lufs = container_master["audio_tracks"][0]["integrated_loudness_lufs"]
    assert wav_lufs == pytest.approx(LUFS_ANCHOR_FAIL, abs=0.3)
    assert container_lufs == pytest.approx(wav_lufs, abs=0.3)
    assert "video" not in container_master
    assert any("V-1.3" in n and "color_primaries" in n for n in notices)
