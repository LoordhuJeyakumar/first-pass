"""
Tests for the operator console guardrails (frontend/app.py).

Covers:
  - Single-flight lock: second POST /api/run returns 409 while a run is locked.
  - Cooldown: POST /api/run returns 429 when inside the cooldown window.
  - Happy-path GET /api/masters returns file list.
  - Poll endpoint returns 404 for unknown run_id.

No live Grafana calls or Vertex AI calls are made. The pipeline function is
patched to avoid any network I/O.
"""

import time
import threading
import importlib
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers — isolate module state between tests
# ---------------------------------------------------------------------------

def _fresh_app():
    """
    Re-imports frontend.app to get a clean module with zeroed state.
    Each guardrail test needs isolated _run_lock / _last_run_at state.
    """
    import frontend.app as app_mod
    # Reset mutable module-level state
    app_mod._runs.clear()
    app_mod._last_run_at = 0.0
    # Release lock if somehow held from a previous test
    if not app_mod._run_lock.acquire(blocking=False):
        pass  # already free
    else:
        app_mod._run_lock.release()
    return app_mod


def _make_client(app_mod=None):
    if app_mod is None:
        app_mod = _fresh_app()
    return TestClient(app_mod.app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Fixture: ensure data/masters/ exists so /api/masters doesn't 500
# ---------------------------------------------------------------------------

FAKE_MASTER = "master_clean.json"


# ---------------------------------------------------------------------------
# § Masters list
# ---------------------------------------------------------------------------

class TestMastersList:
    def test_returns_json_list(self):
        client = _make_client()
        resp = client.get("/api/masters")
        assert resp.status_code == 200
        body = resp.json()
        assert "masters" in body
        assert isinstance(body["masters"], list)

    def test_contains_expected_files(self):
        client = _make_client()
        resp = client.get("/api/masters")
        masters = resp.json()["masters"]
        # data/masters/ must have at least the three canonical masters
        assert any("master_clean" in m for m in masters)
        assert any("master_blockers" in m for m in masters)


# ---------------------------------------------------------------------------
# § Poll endpoint
# ---------------------------------------------------------------------------

class TestPollEndpoint:
    def test_unknown_run_id_returns_404(self):
        client = _make_client()
        resp = client.get("/api/run/nonexistent-run-id")
        assert resp.status_code == 404

    def test_known_run_id_returns_state(self):
        import frontend.app as app_mod
        _fresh_app()
        run_id = "test-run-123"
        app_mod._runs[run_id] = {
            "status": "done",
            "verdict": "PASS",
            "blocker_count": 0,
            "warning_count": 0,
            "master_id": "STRM-TEST",
            "findings": [],
            "readiness": {},
            "india_mode": None,
            "ledger": [],
            "error": None,
        }
        client = _make_client(app_mod)
        resp = client.get(f"/api/run/{run_id}")
        assert resp.status_code == 200
        assert resp.json()["verdict"] == "PASS"


# ---------------------------------------------------------------------------
# § Single-flight guard (409)
# ---------------------------------------------------------------------------

class TestSingleFlightGuard:
    def test_second_run_while_locked_returns_409(self):
        """
        Acquire the run lock manually (simulating an active pipeline), then POST
        /api/run and confirm 409 is returned without starting a second execution.
        """
        import frontend.app as app_mod
        app_mod = _fresh_app()

        # Simulate an active run by holding the lock
        acquired = app_mod._run_lock.acquire(blocking=False)
        assert acquired, "Lock should be free at test start"

        try:
            client = _make_client(app_mod)
            resp = client.post(
                "/api/run",
                json={"master": FAKE_MASTER},
            )
            assert resp.status_code == 409
            body = resp.json()
            assert "detail" in body
            assert "progress" in body["detail"].lower() or "run" in body["detail"].lower()
        finally:
            app_mod._run_lock.release()

    def test_two_rapid_posts_produce_at_most_one_pipeline(self):
        """
        Patch run_delivery_qc to block briefly. Fire two concurrent POST /api/run
        requests. Only one should start a run; the other must return 409.
        """
        import frontend.app as app_mod
        app_mod = _fresh_app()

        barrier = threading.Barrier(1)
        pipeline_call_count = [0]
        run_completed = threading.Event()

        def _fake_pipeline(master_path, spec_path, *args, **kwargs):
            pipeline_call_count[0] += 1
            time.sleep(0.2)  # hold the lock briefly
            run_completed.set()
            return {
                "verdict": "PASS",
                "blocker_count": 0,
                "warning_count": 0,
                "master_id": "TEST",
                "findings": [],
                "readiness": {},
                "india_mode": None,
                "adk_result": {"tool_logs": []},
            }

        with patch.object(app_mod, "run_delivery_qc", side_effect=_fake_pipeline):
            client = _make_client(app_mod)

            results = [None, None]

            def _post(idx):
                results[idx] = client.post("/api/run", json={"master": FAKE_MASTER})

            t1 = threading.Thread(target=_post, args=(0,))
            t2 = threading.Thread(target=_post, args=(1,))
            t1.start()
            time.sleep(0.05)  # let t1 acquire the lock first
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        status_codes = sorted([results[0].status_code, results[1].status_code])
        assert status_codes == [200, 409], (
            f"Expected one 200 and one 409, got {status_codes}"
        )
        # Pipeline must have been called exactly once
        run_completed.wait(timeout=5)
        assert pipeline_call_count[0] == 1


# ---------------------------------------------------------------------------
# § Cooldown guard (429)
# ---------------------------------------------------------------------------

class TestCooldownGuard:
    def test_run_inside_cooldown_returns_429(self):
        """
        Set _last_run_at to 'just now' and confirm the next POST /api/run
        returns 429 with a Retry-After header.
        """
        import frontend.app as app_mod
        app_mod = _fresh_app()
        # Simulate a run that finished 2 seconds ago with a 30-second cooldown
        app_mod._last_run_at = time.time() - 2.0

        with patch.dict("os.environ", {"CONSOLE_COOLDOWN_SECONDS": "30"}):
            client = _make_client(app_mod)
            resp = client.post("/api/run", json={"master": FAKE_MASTER})

        assert resp.status_code == 429
        body = resp.json()
        assert "detail" in body
        assert "retry_after" in body
        assert body["retry_after"] > 0
        assert "Retry-After" in resp.headers

    def test_run_after_cooldown_is_accepted(self):
        """
        Set _last_run_at to well before the cooldown window. The next POST
        should be accepted (200), not rejected.
        """
        import frontend.app as app_mod
        app_mod = _fresh_app()
        # Last run was 120 seconds ago; cooldown is 30 s — should be clear
        app_mod._last_run_at = time.time() - 120.0

        def _fake_pipeline(master_path, spec_path, *args, **kwargs):
            return {
                "verdict": "PASS",
                "blocker_count": 0,
                "warning_count": 0,
                "master_id": "TEST",
                "findings": [],
                "readiness": {},
                "india_mode": None,
                "adk_result": {"tool_logs": []},
            }

        with patch.object(app_mod, "run_delivery_qc", side_effect=_fake_pipeline):
            with patch.dict("os.environ", {"CONSOLE_COOLDOWN_SECONDS": "30"}):
                client = _make_client(app_mod)
                resp = client.post("/api/run", json={"master": FAKE_MASTER})

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        assert "run_id" in resp.json()

    def test_cooldown_zero_disables_wait(self):
        """CONSOLE_COOLDOWN_SECONDS=0 means no cooldown — immediate re-run is allowed."""
        import frontend.app as app_mod
        app_mod = _fresh_app()
        app_mod._last_run_at = time.time() - 0.1  # just finished

        def _fake_pipeline(master_path, spec_path, *args, **kwargs):
            return {
                "verdict": "PASS",
                "blocker_count": 0,
                "warning_count": 0,
                "master_id": "TEST",
                "findings": [],
                "readiness": {},
                "india_mode": None,
                "adk_result": {"tool_logs": []},
            }

        with patch.object(app_mod, "run_delivery_qc", side_effect=_fake_pipeline):
            with patch.dict("os.environ", {"CONSOLE_COOLDOWN_SECONDS": "0"}):
                client = _make_client(app_mod)
                resp = client.post("/api/run", json={"master": FAKE_MASTER})

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# § Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_missing_master_field_returns_400(self):
        client = _make_client()
        resp = client.post("/api/run", json={})
        assert resp.status_code == 400

    def test_nonexistent_master_returns_400_or_404(self):
        client = _make_client()
        resp = client.post("/api/run", json={"master": "does_not_exist.json"})
        assert resp.status_code in (400, 404)

    def test_path_traversal_attempt_returns_400(self):
        client = _make_client()
        resp = client.post("/api/run", json={"master": "../../.env"})
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# § Ledger deeplink builder (unit, no network)
# ---------------------------------------------------------------------------

class TestLedgerBuilder:
    def test_incident_response_produces_deeplink(self):
        import frontend.app as app_mod
        tool_logs = [
            {
                "type": "response",
                "name": "create_incident",
                "response": {
                    "content": [
                        {
                            "type": "text",
                            "text": '{"incident": {"incidentID": "42", "title": "Delivery Blocker"}}',
                        }
                    ]
                },
            }
        ]
        with patch.dict("os.environ", {"GRAFANA_URL": "https://your-stack.grafana.net"}):
            entries = app_mod._build_ledger_entries(tool_logs)

        assert len(entries) == 1
        entry = entries[0]
        assert entry["link_label"] == "Incident #42"
        assert entry["href"] is not None
        assert "42" in entry["href"]
        # URL must not contain any visible text that leaks the hostname in the label
        assert entry["link_label"] != entry["href"]

    def test_missing_incident_id_produces_no_link(self):
        import frontend.app as app_mod
        tool_logs = [
            {
                "type": "response",
                "name": "create_incident",
                "response": {"content": [{"type": "text", "text": "{}"}]},
            }
        ]
        with patch.dict("os.environ", {"GRAFANA_URL": "https://your-stack.grafana.net"}):
            entries = app_mod._build_ledger_entries(tool_logs)

        # Row may or may not be added; if added, href must be None
        for e in entries:
            if e.get("operation") == "↳ Result":
                assert e["href"] is None

    def test_call_entries_are_included(self):
        import frontend.app as app_mod
        tool_logs = [
            {
                "type": "call",
                "name": "create_incident",
                "args": {"title": "Delivery Blocker: TEST", "severity": "critical", "roomPrefix": "first-pass"},
            }
        ]
        entries = app_mod._build_ledger_entries(tool_logs)
        assert any(e["operation"] == "Open Incident" for e in entries)


# ---------------------------------------------------------------------------
# § Thread safety (concurrency)
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_appends_and_reads_produce_valid_json(self):
        """
        Simulate concurrent ledger appends while GET /api/run/{id} polls rapidly.
        Asserts that every poll response parses as valid JSON with zero control character errors.
        """
        import frontend.app as app_mod
        app_mod = _fresh_app()
        run_id = "test-concurrent-run-999"

        with app_mod._runs_lock:
            app_mod._runs[run_id] = {
                "status": "running",
                "verdict": None,
                "blocker_count": 0,
                "warning_count": 0,
                "master_id": "STRM-TEST",
                "findings": [],
                "readiness": {},
                "india_mode": None,
                "ledger": [],
                "error": None,
            }

        client = _make_client(app_mod)
        stop_event = threading.Event()
        errors = []

        def _writer(writer_id):
            count = 0
            while not stop_event.is_set() and count < 100:
                event_entry = {
                    "type": "call",
                    "name": "create_incident",
                    "args": {"title": f"Concurrent Incident {writer_id}-{count}"},
                    "timestamp": "12:34:56 UTC",
                }
                row = app_mod._tool_entry_to_ledger_row(event_entry, "https://your-stack.grafana.net")
                if row:
                    with app_mod._runs_lock:
                        new_ledger = list(app_mod._runs[run_id]["ledger"])
                        new_ledger.append(row)
                        app_mod._runs[run_id]["ledger"] = new_ledger
                count += 1
                time.sleep(0.001)

        def _reader():
            for _ in range(50):
                resp = client.get(f"/api/run/{run_id}")
                if resp.status_code != 200:
                    errors.append(f"HTTP {resp.status_code}: {resp.text}")
                else:
                    try:
                        data = resp.json()
                        assert "ledger" in data
                    except Exception as exc:
                        errors.append(f"JSON decode error: {exc}")
                time.sleep(0.002)

        writers = [threading.Thread(target=_writer, args=(i,)) for i in range(5)]
        readers = [threading.Thread(target=_reader) for i in range(10)]

        for t in writers + readers:
            t.start()
        for t in readers:
            t.join()
        stop_event.set()
        for t in writers:
            t.join()

        assert not errors, f"Concurrent thread safety errors: {errors}"


def test_api_fixture_gating_and_success(monkeypatch):
    """
    Tests that POST /api/fixture returns 404 when CONSOLE_DEV_FIXTURES is not set,
    and returns 200 with run_id when CONSOLE_DEV_FIXTURES=1.
    """
    app_mod = _fresh_app()
    client = _make_client(app_mod)

    # 1. Without CONSOLE_DEV_FIXTURES env var -> 404
    monkeypatch.delenv("CONSOLE_DEV_FIXTURES", raising=False)
    resp = client.post("/api/fixture", json={"verdict": "PASS"})
    assert resp.status_code == 404

    # 2. With CONSOLE_DEV_FIXTURES=1 -> 200 and seedable
    monkeypatch.setenv("CONSOLE_DEV_FIXTURES", "1")
    fixture_payload = {
        "verdict": "REJECT",
        "blocker_count": 2,
        "master_id": "TEST-FIXTURE-MASTER",
        "evaluations": [],
        "findings": [],
    }
    resp_dev = client.post("/api/fixture", json=fixture_payload)
    assert resp_dev.status_code == 200
    data = resp_dev.json()
    assert "run_id" in data

    # Verify poll returns seeded state
    poll_resp = client.get(f"/api/run/{data['run_id']}")
    assert poll_resp.status_code == 200
    poll_data = poll_resp.json()
    assert poll_data["verdict"] == "REJECT"
    assert poll_data["blocker_count"] == 2

