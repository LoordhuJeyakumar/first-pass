"""
Unit test suite for First Pass Telemetry Emitter (Prometheus Remote-Write & Loki Push API).
Validates fixed label set constraints, high-cardinality protection, environment validation,
and payload construction across all synthetic masters.
"""

import os
import json
import pytest
from unittest.mock import patch, MagicMock

from agents.check_engine import evaluate_master_against_spec
from agents.telemetry import (
    ALLOWED_LABEL_SETS,
    validate_metric_labels,
    validate_telemetry_environment,
    build_prometheus_metrics,
    build_loki_log_payload,
    send_prometheus_metrics,
    send_loki_logs,
    emit_qc_telemetry,
)


@pytest.fixture
def streamone_spec():
    spec_path = os.path.join(os.path.dirname(__file__), "..", "data", "specs", "streamone.json")
    with open(spec_path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def master_clean():
    path = os.path.join(os.path.dirname(__file__), "..", "data", "masters", "master_clean.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def master_blockers():
    path = os.path.join(os.path.dirname(__file__), "..", "data", "masters", "master_blockers.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def master_warnings():
    path = os.path.join(os.path.dirname(__file__), "..", "data", "masters", "master_warnings.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def valid_telemetry_env():
    return {
        "prom_remote_write_url": "https://prometheus-prod-01.grafana.net/api/v1/push",
        "prom_username": "123456",
        "loki_push_url": "https://logs-prod-01.grafana.net/loki/api/v1/push",
        "loki_username": "654321",
        "grafana_cloud_api_key": "glsa_TEST_TOKEN",
    }



# -----------------------------------------------------------------------------
# 1. Label Set Cardinality Protection Tests
# -----------------------------------------------------------------------------

def test_validate_metric_labels_valid_sets():
    # Valid qc_checks
    validate_metric_labels("qc_checks", {"__name__": "qc_checks", "domain": "audio", "result": "pass"})
    # Valid qc_loudness_deviation_lufs
    validate_metric_labels("qc_loudness_deviation_lufs", {"__name__": "qc_loudness_deviation_lufs", "language": "ta-IN"})
    # Valid qc_blockers_current
    validate_metric_labels("qc_blockers_current", {"__name__": "qc_blockers_current"})
    # Valid qc_readiness_ratio
    validate_metric_labels("qc_readiness_ratio", {"__name__": "qc_readiness_ratio", "language": "ta-IN"})


def test_validate_metric_labels_rejects_unknown_metric():
    with pytest.raises(ValueError, match="not in allowed label registry"):
        validate_metric_labels("qc_invented_metric", {"__name__": "qc_invented_metric"})


def test_validate_metric_labels_rejects_high_cardinality_run_id():
    """
    CRITICAL CARDINALITY TEST:
    Asserts that adding run_id to ANY metric label set causes an immediate ValueError.
    """
    labels_with_run_id = {
        "__name__": "qc_checks",
        "domain": "audio",
        "result": "pass",
        "run_id": "run-12345-abc",  # High cardinality violation!
    }
    with pytest.raises(ValueError, match="invalid label keys"):
        validate_metric_labels("qc_checks", labels_with_run_id)


def test_validate_metric_labels_rejects_extra_master_id_label():
    labels_with_master_id = {
        "__name__": "qc_blockers_current",
        "master_id": "STRM-2026-0142",
    }
    with pytest.raises(ValueError, match="invalid label keys"):
        validate_metric_labels("qc_blockers_current", labels_with_master_id)


def test_validate_metric_labels_rejects_missing_required_label():
    labels_missing_result = {
        "__name__": "qc_checks",
        "domain": "audio",
        # missing "result"
    }
    with pytest.raises(ValueError, match="invalid label keys"):
        validate_metric_labels("qc_checks", labels_missing_result)


# -----------------------------------------------------------------------------
# 2. Environment Variable Validation Tests
# -----------------------------------------------------------------------------

def test_validate_telemetry_environment_success(monkeypatch):
    monkeypatch.setenv("PROM_REMOTE_WRITE_URL", "https://prometheus-us-central1.grafana.net/api/v1/push")
    monkeypatch.setenv("PROM_USERNAME", "112233")
    monkeypatch.setenv("LOKI_PUSH_URL", "https://logs-us-central1.grafana.net/loki/api/v1/push")
    monkeypatch.setenv("LOKI_USERNAME", "445566")
    monkeypatch.setenv("GRAFANA_CLOUD_API_KEY", "glsa_TEST_SECRET_KEY")

    cfg = validate_telemetry_environment()
    assert cfg["prom_remote_write_url"] == "https://prometheus-us-central1.grafana.net/api/v1/push"
    assert cfg["prom_username"] == "112233"


def test_validate_telemetry_environment_fails_on_missing_or_placeholders(monkeypatch):
    monkeypatch.setenv("PROM_REMOTE_WRITE_URL", "https://prometheus-YOUR-REGION.grafana.net/api/v1/push")
    monkeypatch.setenv("PROM_USERNAME", "YOUR_PROMETHEUS_USERNAME")
    monkeypatch.setenv("LOKI_PUSH_URL", "https://logs-YOUR-REGION.grafana.net/loki/api/v1/push")
    monkeypatch.setenv("LOKI_USERNAME", "YOUR_LOKI_USERNAME")
    monkeypatch.setenv("GRAFANA_CLOUD_API_KEY", "glsa_REPLACE_ME")

    with pytest.raises(RuntimeError) as exc_info:
        validate_telemetry_environment()

    err_msg = str(exc_info.value)
    assert "PROM_REMOTE_WRITE_URL" in err_msg
    assert "PROM_USERNAME" in err_msg
    assert "LOKI_PUSH_URL" in err_msg
    assert "LOKI_USERNAME" in err_msg
    assert "GRAFANA_CLOUD_API_KEY" in err_msg


# -----------------------------------------------------------------------------
# 3. Prometheus Metrics Payload Construction Tests
# -----------------------------------------------------------------------------

def test_build_prometheus_metrics_master_clean(master_clean, streamone_spec):
    report = evaluate_master_against_spec(master_clean, streamone_spec)
    metrics = build_prometheus_metrics(report, timestamp_ms=1700000000000)

    # Clean run should have 0 blockers
    blockers_metric = [m for m in metrics if m["metric"]["__name__"] == "qc_blockers_current"][0]
    assert blockers_metric["values"] == [0.0]
    assert blockers_metric["timestamps"] == [1700000000000]

    # Verify all metric label keys match allowed label sets
    for item in metrics:
        name = item["metric"]["__name__"]
        assert set(item["metric"].keys()) == ALLOWED_LABEL_SETS[name]
        assert "run_id" not in item["metric"]


def test_build_prometheus_metrics_master_blockers(master_blockers, streamone_spec):
    report = evaluate_master_against_spec(master_blockers, streamone_spec)
    metrics = build_prometheus_metrics(report, timestamp_ms=1700000000000)

    # master_blockers has 3 blockers
    blockers_metric = [m for m in metrics if m["metric"]["__name__"] == "qc_blockers_current"][0]
    assert blockers_metric["values"] == [3.0]

    # Loudness deviation metric for ta-IN should be +3.0 LUFS (-24.0 vs -27.0 target)
    ta_dev = [
        m for m in metrics
        if m["metric"]["__name__"] == "qc_loudness_deviation_lufs" and m["metric"]["language"] == "ta-IN"
    ][0]
    assert ta_dev["values"] == [3.0]

    # Readiness ratio for ta-IN should be present
    ta_readiness = [
        m for m in metrics
        if m["metric"]["__name__"] == "qc_readiness_ratio" and m["metric"]["language"] == "ta-IN"
    ][0]
    assert ta_readiness["values"] == [0.333]


# -----------------------------------------------------------------------------
# 4. Loki Log Payload Construction Tests
# -----------------------------------------------------------------------------

def test_build_loki_log_payload_master_blockers(master_blockers, streamone_spec):
    report = evaluate_master_against_spec(master_blockers, streamone_spec)
    run_id = "run-TEST-BLOCKERS-999"
    ts_ns = 1700000000000000000

    payload = build_loki_log_payload(report, run_id=run_id, timestamp_ns=ts_ns)

    streams = payload.get("streams", [])
    assert len(streams) == 1
    assert streams[0]["stream"] == {"job": "first-pass-qc", "service_name": "first-pass"}

    values = streams[0]["values"]
    assert len(values) == 3  # 3 blocker findings in master_blockers

    # Inspect first finding log line
    first_ts, first_log_str = values[0]
    assert first_ts == str(ts_ns)

    first_dict = json.loads(first_log_str)
    assert first_dict["run_id"] == run_id
    assert first_dict["clause_id"] == "A-2.1"
    assert first_dict["severity"] == "blocker"
    assert first_dict["measured"] == "-24.0 LUFS"
    assert first_dict["expected"] == "-27.0 ± 2.0 LUFS"
    assert first_dict["language"] == "ta-IN"


def test_build_loki_log_payload_master_clean_has_empty_log_lines(master_clean, streamone_spec):
    report = evaluate_master_against_spec(master_clean, streamone_spec)
    run_id = "run-TEST-CLEAN-000"

    payload = build_loki_log_payload(report, run_id=run_id)
    assert payload["streams"][0]["values"] == []


def test_build_loki_log_payload_timestamps_unique_and_monotonic(master_blockers, streamone_spec):
    report = evaluate_master_against_spec(master_blockers, streamone_spec)
    run_id = "run-TEST-TIMESTAMPS-UNIQUE"
    ts_ns = 1700000000000000000

    payload = build_loki_log_payload(report, run_id=run_id, timestamp_ns=ts_ns)
    values = payload["streams"][0]["values"]
    assert len(values) > 1, "Test requires multiple findings to verify timestamp uniqueness across lines"

    timestamps = [int(v[0]) for v in values]
    assert len(timestamps) == len(set(timestamps)), "All timestamps in Loki log payload must be unique"
    assert all(timestamps[i] < timestamps[i + 1] for i in range(len(timestamps) - 1)), "Timestamps must be monotonically increasing"


# -----------------------------------------------------------------------------
# 5. Transmission & Network Mock Tests
# -----------------------------------------------------------------------------

@patch("agents.telemetry.RemoteWriter")
def test_send_prometheus_metrics_success(mock_writer_cls, valid_telemetry_env):
    from prometheus_remote_writer import SendResult
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_writer = MagicMock()
    mock_writer.send.return_value = SendResult(
        requests_sent=1, series_sent=1, samples_sent=1, last_response=mock_response
    )
    mock_writer_cls.return_value = mock_writer

    metrics = [{"metric": {"__name__": "qc_blockers_current"}, "values": [0.0], "timestamps": [123]}]
    res = send_prometheus_metrics(metrics, valid_telemetry_env)

    assert res.series_sent == 1
    mock_writer.send.assert_called_once_with(metrics)
    mock_writer.close.assert_called_once()


@patch("agents.telemetry.RemoteWriter")
def test_send_prometheus_metrics_empty_metrics(mock_writer_cls, valid_telemetry_env):
    from prometheus_remote_writer import SendResult

    mock_writer = MagicMock()
    mock_writer.send.return_value = SendResult(
        requests_sent=0, series_sent=0, samples_sent=0, last_response=None
    )
    mock_writer_cls.return_value = mock_writer

    metrics = []
    res = send_prometheus_metrics(metrics, valid_telemetry_env)

    assert res.series_sent == 0
    assert res.last_response is None
    mock_writer.close.assert_called_once()


@patch("agents.telemetry.RemoteWriter")
def test_send_prometheus_metrics_zero_series_sent_raises(mock_writer_cls, valid_telemetry_env):
    from prometheus_remote_writer import SendResult
    mock_response = MagicMock()
    mock_response.status_code = 200

    mock_writer = MagicMock()
    # Metrics non-empty but series_sent = 0
    mock_writer.send.return_value = SendResult(
        requests_sent=1, series_sent=0, samples_sent=0, last_response=mock_response
    )
    mock_writer_cls.return_value = mock_writer

    metrics = [{"metric": {"__name__": "qc_blockers_current"}, "values": [0.0], "timestamps": [123]}]
    with pytest.raises(RuntimeError, match="completed but 0 series were sent"):
        send_prometheus_metrics(metrics, valid_telemetry_env)


@patch("agents.telemetry.requests.post")
def test_send_loki_logs_success(mock_post, valid_telemetry_env):
    mock_resp = MagicMock()
    mock_resp.status_code = 204
    mock_post.return_value = mock_resp

    payload = {"streams": []}
    send_loki_logs(payload, valid_telemetry_env)
    mock_post.assert_called_once()


@patch("agents.telemetry.requests.post")
def test_send_loki_logs_failure_raises(mock_post, valid_telemetry_env):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    mock_post.return_value = mock_resp

    payload = {"streams": []}
    with pytest.raises(RuntimeError, match="Loki push API failed with HTTP status 500"):
        send_loki_logs(payload, valid_telemetry_env)


@patch("agents.telemetry.send_prometheus_metrics")
@patch("agents.telemetry.send_loki_logs")
def test_emit_qc_telemetry_full_flow(
    mock_send_loki, mock_send_prom, master_blockers, streamone_spec, valid_telemetry_env
):
    report = evaluate_master_against_spec(master_blockers, streamone_spec)

    result = emit_qc_telemetry(report, env_cfg=valid_telemetry_env, run_id="run-TEST-EMIT-123")

    assert result["status"] == "ok"
    assert result["run_id"] == "run-TEST-EMIT-123"
    assert result["metrics_count"] > 0
    assert result["logs_count"] == 3

    mock_send_prom.assert_called_once()
    mock_send_loki.assert_called_once()


@patch("agents.telemetry.send_prometheus_metrics")
@patch("agents.telemetry.send_loki_logs")
def test_emit_qc_telemetry_auto_generated_run_id(
    mock_send_loki, mock_send_prom, master_blockers, streamone_spec, valid_telemetry_env
):
    report = evaluate_master_against_spec(master_blockers, streamone_spec)

    # Do not pass run_id -> exercises if not run_id auto-generation branch
    result = emit_qc_telemetry(report, env_cfg=valid_telemetry_env, run_id=None)

    assert result["status"] == "ok"
    assert result["run_id"].startswith("run-STRM-2026-0142-")
    assert result["metrics_count"] > 0
    assert result["logs_count"] == 3


@patch("agents.telemetry.send_prometheus_metrics")
@patch("agents.telemetry.send_loki_logs")
def test_emit_qc_telemetry_auto_env_cfg(
    mock_send_loki, mock_send_prom, master_blockers, streamone_spec, monkeypatch
):
    monkeypatch.setenv("PROM_REMOTE_WRITE_URL", "https://prometheus-prod-01.grafana.net/api/v1/push")
    monkeypatch.setenv("PROM_USERNAME", "123456")
    monkeypatch.setenv("LOKI_PUSH_URL", "https://logs-prod-01.grafana.net/loki/api/v1/push")
    monkeypatch.setenv("LOKI_USERNAME", "654321")
    monkeypatch.setenv("GRAFANA_CLOUD_API_KEY", "glsa_TEST_TOKEN")

    report = evaluate_master_against_spec(master_blockers, streamone_spec)

    # env_cfg=None -> exercises validate_telemetry_environment() call on line 238
    result = emit_qc_telemetry(report, env_cfg=None, run_id="run-AUTO-ENV")

    assert result["status"] == "ok"
    assert result["run_id"] == "run-AUTO-ENV"


