"""
First Pass — Telemetry Emitter (Prometheus Remote-Write & Loki Push API)

Ingests QC evaluation report objects and emits fixed-cardinality Prometheus metrics
and structured Loki JSON log lines to Grafana Cloud.
"""

import os
import time
import json
import logging
import requests
from typing import Dict, Any, List, Optional
from prometheus_remote_writer import RemoteWriter, MetricItem

logger = logging.getLogger("FirstPassTelemetry")

# Strictly enforced low-cardinality metric label sets to protect the 10,000-series cap.
ALLOWED_LABEL_SETS = {
    "qc_check_total": {"__name__", "domain", "result"},
    "qc_loudness_deviation_lufs": {"__name__", "language"},
    "qc_blockers_current": {"__name__"},
}


def validate_metric_labels(metric_name: str, labels: Dict[str, str]) -> None:
    """
    Validates that a metric's label keys strictly match its fixed allowed label set.
    Fails loudly with ValueError if an unauthorized label (e.g. run_id) or invalid metric name is used.
    """
    if metric_name not in ALLOWED_LABEL_SETS:
        raise ValueError(f"Metric name '{metric_name}' is not in allowed label registry: {list(ALLOWED_LABEL_SETS.keys())}")

    allowed_keys = ALLOWED_LABEL_SETS[metric_name]
    found_keys = set(labels.keys())

    if found_keys != allowed_keys:
        raise ValueError(
            f"Metric '{metric_name}' has invalid label keys: {found_keys}. "
            f"Exact allowed label set is: {allowed_keys}"
        )


def validate_telemetry_environment() -> Dict[str, str]:
    """
    Validates required environment variables for Grafana Cloud Prometheus and Loki telemetry.
    Fails loudly with RuntimeError if required variables are missing or set to placeholders.
    """
    prom_url = os.getenv("PROM_REMOTE_WRITE_URL")
    prom_user = os.getenv("PROM_USERNAME")
    loki_url = os.getenv("LOKI_PUSH_URL")
    loki_user = os.getenv("LOKI_USERNAME")
    api_key = os.getenv("GRAFANA_CLOUD_API_KEY")

    missing = []
    
    if not prom_url or "YOUR-REGION" in prom_url or "YOUR_PROMETHEUS" in prom_url:
        missing.append("PROM_REMOTE_WRITE_URL")
    if not prom_user or "YOUR_PROMETHEUS" in prom_user:
        missing.append("PROM_USERNAME")
    if not loki_url or "YOUR-REGION" in loki_url or "YOUR_LOKI" in loki_url:
        missing.append("LOKI_PUSH_URL")
    if not loki_user or "YOUR_LOKI" in loki_user:
        missing.append("LOKI_USERNAME")
    if not api_key or api_key == "glsa_REPLACE_ME" or "REPLACE_ME" in api_key:
        missing.append("GRAFANA_CLOUD_API_KEY")

    if missing:
        raise RuntimeError(
            f"Missing or unconfigured Grafana Cloud telemetry environment variables:\n"
            + "\n".join(f" - {var}" for var in missing)
            + "\nPlease configure your .env file with real Grafana Cloud credentials."
        )

    return {
        "prom_remote_write_url": prom_url,
        "prom_username": prom_user,
        "loki_push_url": loki_url,
        "loki_username": loki_user,
        "grafana_cloud_api_key": api_key,
    }


def build_prometheus_metrics(report: Dict[str, Any], timestamp_ms: Optional[int] = None) -> List[MetricItem]:
    """
    Constructs Prometheus MetricItem objects from a QC evaluation report object.
    Enforces strict label set checks on every generated metric item.
    """
    ts_ms = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    metrics: List[MetricItem] = []

    # 1. qc_blockers_current (no extra labels)
    blockers_val = float(report.get("blocker_count", 0))
    blockers_labels = {"__name__": "qc_blockers_current"}
    validate_metric_labels("qc_blockers_current", blockers_labels)
    metrics.append({
        "metric": blockers_labels,
        "values": [blockers_val],
        "timestamps": [ts_ms],
    })

    # Process evaluations if present
    evaluations = report.get("evaluations", [])
    for ev in evaluations:
        domain = str(ev.get("domain", "unknown"))
        result = str(ev.get("result", "pass"))

        # 2. qc_check_total{domain, result}
        check_labels = {
            "__name__": "qc_check_total",
            "domain": domain,
            "result": result,
        }
        validate_metric_labels("qc_check_total", check_labels)
        metrics.append({
            "metric": check_labels,
            "values": [1.0],
            "timestamps": [ts_ms],
        })

        # 3. qc_loudness_deviation_lufs{language}
        if domain == "audio" and "loudness_deviation_lufs" in ev:
            lang = str(ev.get("language", "unknown"))
            dev_val = float(ev["loudness_deviation_lufs"])
            loudness_labels = {
                "__name__": "qc_loudness_deviation_lufs",
                "language": lang,
            }
            validate_metric_labels("qc_loudness_deviation_lufs", loudness_labels)
            metrics.append({
                "metric": loudness_labels,
                "values": [dev_val],
                "timestamps": [ts_ms],
            })

    return metrics


def build_loki_log_payload(
    report: Dict[str, Any], run_id: str, timestamp_ns: Optional[int] = None
) -> Dict[str, Any]:
    """
    Constructs structured Loki log push payload containing one JSON log line per finding.
    run_id is included inside the JSON log payload ONLY, never as a Prometheus metric label.
    """
    ts_ns = timestamp_ns if timestamp_ns is not None else int(time.time() * 1e9)
    ts_ns_str = str(ts_ns)

    log_values = []
    findings = report.get("findings", [])

    for finding in findings:
        line_dict = {
            "run_id": run_id,
            "clause_id": str(finding.get("clause_id", "")),
            "severity": str(finding.get("severity", "blocker")),
            "measured": str(finding.get("measured", "")),
            "expected": str(finding.get("expected", "")),
            "language": str(finding.get("language", "")),
            "message": str(finding.get("message", "")),
        }
        line_str = json.dumps(line_dict, separators=(",", ":"))
        log_values.append([ts_ns_str, line_str])

    return {
        "streams": [
            {
                "stream": {
                    "job": "first-pass-qc",
                    "service_name": "first-pass",
                },
                "values": log_values,
            }
        ]
    }


def send_prometheus_metrics(metrics: List[MetricItem], env_cfg: Dict[str, str]) -> None:
    """
    Sends metric items to Grafana Cloud via Prometheus Remote-Write.
    Fails loudly with RuntimeError if remote-write operation encounters an error.
    """
    writer = RemoteWriter(
        url=env_cfg["prom_remote_write_url"],
        auth={
            "username": env_cfg["prom_username"],
            "password": env_cfg["grafana_cloud_api_key"],
        },
    )
    try:
        result = writer.send(metrics)
        if not result.success:
            raise RuntimeError(f"Prometheus remote-write request failed: {result.error}")
        logger.info(f"Prometheus remote-write succeeded ({len(metrics)} series sent).")
    finally:
        writer.close()


def send_loki_logs(payload: Dict[str, Any], env_cfg: Dict[str, str]) -> None:
    """
    Pushes structured JSON log lines to Grafana Cloud Loki HTTP Push API.
    Fails loudly with RuntimeError if Loki push API returns an error status code.
    """
    resp = requests.post(
        env_cfg["loki_push_url"],
        json=payload,
        auth=(env_cfg["loki_username"], env_cfg["grafana_cloud_api_key"]),
        headers={"Content-Type": "application/json"},
        timeout=10.0,
    )
    if resp.status_code not in (200, 204):
        raise RuntimeError(
            f"Loki push API failed with HTTP status {resp.status_code}: {resp.text}"
        )
    logger.info("Loki log push succeeded.")


def emit_qc_telemetry(
    report: Dict[str, Any],
    env_cfg: Optional[Dict[str, str]] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Primary entrypoint for emitting QC run telemetry (Prometheus metrics and Loki logs).
    Validates environment, constructs payloads, and sends telemetry to Grafana Cloud.
    """
    if env_cfg is None:
        env_cfg = validate_telemetry_environment()

    if not run_id:
        master_id = str(report.get("master_id", "UNKNOWN"))
        ts_ms = int(time.time() * 1000)
        run_id = f"run-{master_id}-{ts_ms}"

    metrics = build_prometheus_metrics(report)
    send_prometheus_metrics(metrics, env_cfg)

    loki_payload = build_loki_log_payload(report, run_id)
    pushed_logs_count = len(loki_payload["streams"][0]["values"])
    if pushed_logs_count > 0:
        send_loki_logs(loki_payload, env_cfg)

    return {
        "status": "ok",
        "run_id": run_id,
        "metrics_count": len(metrics),
        "logs_count": pushed_logs_count,
    }
