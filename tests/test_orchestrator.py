import logging
from unittest.mock import MagicMock, patch
import pytest

from agents.orchestrator import (
    LEGACY_ADOPT_SPEC_ID,
    LEGACY_ALERT_TITLE,
    alert_rule_content_matches,
    alert_rule_uid_slug,
    check_existing_alert_rule,
    desired_alert_rule,
    match_alert_rule_for_spec,
)

STREAMONE = "STREAMONE-DELIVERY-2026"
HALLARC = "HALLARC-SCREENING-2026"


def _ruler_payload(grafana_alert: dict) -> dict:
    return {"first-pass-alerts": [{"grafana_alert": grafana_alert}]}


def _check(mock_resp, spec_id: str):
    with patch("requests.get", return_value=mock_resp):
        return check_existing_alert_rule(
            grafana_url="https://example.com",
            token="token123",
            folder_uid="first-pass-qc",
            rule_group="first-pass-alerts",
            spec_id=spec_id,
        )


def test_match_by_spec_id_label_ignores_title():
    """Identity is the spec_id label, not the human title."""
    rules = [
        {
            "grafana_alert": {
                "title": "some other title that used to be hardcoded",
                "uid": "uid-label-match",
                "labels": {"first_pass_spec_id": STREAMONE},
                "annotations": {"summary": "old"},
            }
        }
    ]
    matched = match_alert_rule_for_spec(rules, STREAMONE)
    assert matched is not None
    assert matched["uid"] == "uid-label-match"
    assert matched["title"] != "First Pass blockers (STREAMONE-DELIVERY-2026)"


def test_check_existing_found_by_label_not_title():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _ruler_payload(
        {
            "title": "unrelated display title",
            "uid": "alert-uid-label",
            "labels": {"first_pass_spec_id": STREAMONE},
        }
    )
    status, found = _check(mock_resp, STREAMONE)
    assert status == "found"
    assert found["uid"] == "alert-uid-label"
    assert found["title"] == "unrelated display title"


def test_legacy_title_adopt_only_for_streamone():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = _ruler_payload(
        {
            "title": LEGACY_ALERT_TITLE,
            "uid": "ffuv7y5eonpc0f",
        }
    )
    status, found = _check(mock_resp, STREAMONE)
    assert LEGACY_ADOPT_SPEC_ID == STREAMONE
    assert status == "found"
    assert found["uid"] == "ffuv7y5eonpc0f"

    status_h, found_h = _check(mock_resp, HALLARC)
    assert status_h == "absent"
    assert found_h is None


def test_check_existing_alert_rule_absent():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"first-pass-alerts": []}
    status, found = _check(mock_resp, STREAMONE)
    assert status == "absent"
    assert found is None


def test_check_existing_alert_rule_404_absent():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    status, found = _check(mock_resp, STREAMONE)
    assert status == "absent"
    assert found is None


def test_check_existing_alert_rule_failed_http_error(caplog):
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.text = "Internal Server Error"
    with caplog.at_level(logging.WARNING):
        status, found = _check(mock_resp, STREAMONE)
        assert status == "failed"
        assert found is None
        assert "HTTP status 500" in caplog.text


def test_check_existing_alert_rule_failed_exception(caplog):
    with caplog.at_level(logging.WARNING):
        with patch("requests.get", side_effect=RuntimeError("Connection timeout")):
            status, found = check_existing_alert_rule(
                grafana_url="https://example.com",
                token="token123",
                folder_uid="first-pass-qc",
                rule_group="first-pass-alerts",
                spec_id=STREAMONE,
            )
            assert status == "failed"
            assert found is None
            assert "RuntimeError" in caplog.text


def test_desired_alert_rule_lists_blocker_clauses():
    streamone = {
        "spec_id": STREAMONE,
        "clauses": [
            {"clause_id": "A-2.1", "severity_on_fail": "blocker"},
            {"clause_id": "P-1.1", "severity_on_fail": "warning"},
            {"clause_id": "V-1.3", "severity_on_fail": "blocker"},
            {"clause_id": "T-4.2", "severity_on_fail": "blocker"},
            {"clause_id": "A-2.2", "severity_on_fail": "blocker"},
        ],
    }
    desired = desired_alert_rule(streamone, STREAMONE)
    assert desired["title"] == f"First Pass blockers ({STREAMONE})"
    assert desired["labels"]["first_pass_spec_id"] == STREAMONE
    assert "A-2.1" in desired["annotations"]["summary"]
    assert "A-2.2" in desired["annotations"]["summary"]
    assert "T-4.2" in desired["annotations"]["summary"]
    assert "V-1.3" in desired["annotations"]["summary"]
    assert "P-1.1" not in desired["annotations"]["summary"]
    assert 27 <= len(desired["rule_uid"]) <= 28
    assert desired["rule_uid"] == alert_rule_uid_slug(STREAMONE)

    hallarc = {
        "spec_id": HALLARC,
        "clauses": [
            {"clause_id": "A-1.1", "severity_on_fail": "blocker"},
            {"clause_id": "A-1.2", "severity_on_fail": "blocker"},
            {"clause_id": "T-3.1", "severity_on_fail": "blocker"},
            {"clause_id": "V-2.1", "severity_on_fail": "blocker"},
        ],
    }
    h_desired = desired_alert_rule(hallarc, HALLARC)
    assert h_desired["clause_ids"] == ["A-1.1", "A-1.2", "T-3.1", "V-2.1"]


def test_content_match_requires_title_label_and_summary():
    desired = desired_alert_rule(
        {"spec_id": STREAMONE, "clauses": [{"clause_id": "A-2.1", "severity_on_fail": "blocker"}]},
        STREAMONE,
    )
    matching = {
        "title": desired["title"],
        "labels": dict(desired["labels"]),
        "annotations": dict(desired["annotations"]),
    }
    assert alert_rule_content_matches(matching, desired) is True
    stale_title = dict(matching)
    stale_title["title"] = LEGACY_ALERT_TITLE
    assert alert_rule_content_matches(stale_title, desired) is False


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


def test_ensure_public_dashboard_share_creates_when_missing():
    from agents.orchestrator import ensure_public_dashboard_share

    get_resp = MagicMock()
    get_resp.status_code = 404
    get_resp.text = "not found"
    post_resp = MagicMock()
    post_resp.status_code = 200
    post_resp.json.return_value = {
        "uid": "pd-uid",
        "accessToken": "abc123token",
        "isEnabled": True,
    }
    with patch("requests.get", return_value=get_resp), patch("requests.post", return_value=post_resp):
        ok, url = ensure_public_dashboard_share(
            grafana_url="https://example.com",
            token="token123",
            dashboard_uid="first-pass-delivery-readiness",
        )
    assert ok is True
    assert url == "https://example.com/public-dashboards/abc123token"


def test_ensure_public_dashboard_share_patches_when_disabled():
    from agents.orchestrator import ensure_public_dashboard_share

    get_resp = MagicMock()
    get_resp.status_code = 200
    get_resp.json.return_value = {
        "uid": "pd-uid",
        "accessToken": "abc123token",
        "isEnabled": False,
        "annotationsEnabled": False,
        "timeSelectionEnabled": False,
    }
    patch_resp = MagicMock()
    patch_resp.status_code = 200
    patch_resp.json.return_value = {
        "uid": "pd-uid",
        "accessToken": "abc123token",
        "isEnabled": True,
        "annotationsEnabled": True,
        "timeSelectionEnabled": True,
    }
    with patch("requests.get", return_value=get_resp), patch("requests.patch", return_value=patch_resp) as mock_patch:
        ok, url = ensure_public_dashboard_share(
            grafana_url="https://example.com/",
            token="token123",
            dashboard_uid="first-pass-delivery-readiness",
        )
    assert ok is True
    assert url == "https://example.com/public-dashboards/abc123token"
    mock_patch.assert_called_once()


def test_ensure_public_dashboard_share_permission_denied(caplog):
    from agents.orchestrator import ensure_public_dashboard_share

    get_resp = MagicMock()
    get_resp.status_code = 404
    post_resp = MagicMock()
    post_resp.status_code = 403
    post_resp.text = "Permissions needed: dashboards.public:write"
    with caplog.at_level(logging.WARNING):
        with patch("requests.get", return_value=get_resp), patch("requests.post", return_value=post_resp):
            ok, url = ensure_public_dashboard_share(
                grafana_url="https://example.com",
                token="token123",
                dashboard_uid="first-pass-delivery-readiness",
            )
    assert ok is False
    assert url is None
    assert "HTTP 403" in caplog.text


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
