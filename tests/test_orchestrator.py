import logging
from unittest.mock import MagicMock, patch
import pytest

from agents.orchestrator import check_existing_alert_rule


def test_check_existing_alert_rule_found():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "first-pass-alerts": [
            {
                "grafana_alert": {
                    "title": "First Pass - Delivery Blockers Present",
                    "uid": "alert-uid-999",
                }
            }
        ]
    }
    with patch("requests.get", return_value=mock_resp):
        status, uid = check_existing_alert_rule(
            grafana_url="https://example.com",
            token="token123",
            folder_uid="first-pass-qc",
            rule_group="first-pass-alerts",
            title="First Pass - Delivery Blockers Present",
        )
        assert status == "found"
        assert uid == "alert-uid-999"


def test_check_existing_alert_rule_absent():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"first-pass-alerts": []}
    with patch("requests.get", return_value=mock_resp):
        status, uid = check_existing_alert_rule(
            grafana_url="https://example.com",
            token="token123",
            folder_uid="first-pass-qc",
            rule_group="first-pass-alerts",
            title="First Pass - Delivery Blockers Present",
        )
        assert status == "absent"
        assert uid is None


def test_check_existing_alert_rule_404_absent():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    with patch("requests.get", return_value=mock_resp):
        status, uid = check_existing_alert_rule(
            grafana_url="https://example.com",
            token="token123",
            folder_uid="first-pass-qc",
            rule_group="first-pass-alerts",
            title="First Pass - Delivery Blockers Present",
        )
        assert status == "absent"
        assert uid is None


def test_check_existing_alert_rule_failed_http_error(caplog):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    with caplog.at_level(logging.WARNING):
        with patch("requests.get", return_value=mock_resp):
            status, uid = check_existing_alert_rule(
                grafana_url="https://example.com",
                token="token123",
                folder_uid="first-pass-qc",
                rule_group="first-pass-alerts",
                title="First Pass - Delivery Blockers Present",
            )
            assert status == "failed"
            assert uid is None
            assert "HTTP status 500" in caplog.text


def test_check_existing_alert_rule_failed_exception(caplog):
    with caplog.at_level(logging.WARNING):
        with patch("requests.get", side_effect=RuntimeError("Connection timeout")):
            status, uid = check_existing_alert_rule(
                grafana_url="https://example.com",
                token="token123",
                folder_uid="first-pass-qc",
                rule_group="first-pass-alerts",
                title="First Pass - Delivery Blockers Present",
            )
            assert status == "failed"
            assert uid is None
            assert "RuntimeError" in caplog.text


def test_ensure_delivery_readiness_dashboard_success():
    from agents.orchestrator import ensure_delivery_readiness_dashboard
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"uid": "first-pass-delivery-readiness"}
    with patch("requests.post", return_value=mock_resp):
        ok, uid = ensure_delivery_readiness_dashboard(
            grafana_url="https://example.com",
            token="token123",
            folder_uid="first-pass-qc",
            dashboard_template={"title": "Delivery Readiness"},
        )
        assert ok is True
        assert uid == "first-pass-delivery-readiness"


def test_ensure_delivery_readiness_dashboard_failure(caplog):
    from agents.orchestrator import ensure_delivery_readiness_dashboard
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    with caplog.at_level(logging.WARNING):
        with patch("requests.post", return_value=mock_resp):
            ok, uid = ensure_delivery_readiness_dashboard(
                grafana_url="https://example.com",
                token="token123",
                folder_uid="first-pass-qc",
                dashboard_template={"title": "Delivery Readiness"},
            )
            assert ok is False
            assert uid is None
            assert "HTTP 500" in caplog.text


def test_all_module_type_hints_resolve():
    import typing
    import inspect
    import agents.orchestrator as orch
    import agents.check_engine as check
    import agents.telemetry as telem

    for mod in (orch, check, telem):
        for name, obj in inspect.getmembers(mod, inspect.isfunction):
            if obj.__module__ == mod.__name__:
                hints = typing.get_type_hints(obj)
                assert isinstance(hints, dict)


