"""
First Pass — Operator Console (FastAPI)

Single-page web app that provides:
  - GET  /           : Renders the operator console HTML page.
  - GET  /api/masters: Lists available master files from data/masters/.
  - POST /api/run    : Starts a pipeline run in a background thread; returns run_id.
                       Returns 409 if a run is already in progress.
                       Returns 429 if inside the cooldown window.
  - GET  /api/run/{run_id}: Polls run status; returns verdict, findings, ledger entries.

Guardrails:
  - Single-flight: threading.Lock prevents two concurrent pipeline executions.
  - Cooldown: configurable via CONSOLE_COOLDOWN_SECONDS (default 30 s) between runs.

The console renders what the engine produced. It never recomputes verdicts.
"""

import os
import sys
import json
import time
import uuid
import threading
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# ---------------------------------------------------------------------------
# Path setup — allow importing agents.orchestrator from project root
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load .env if present, using the same guarded pattern as orchestrator.py
_env_file = PROJECT_ROOT / ".env"
if _env_file.exists():
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(str(_env_file))
    except ImportError:
        pass

from agents.orchestrator import run_delivery_qc  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MASTERS_DIR = PROJECT_ROOT / "data" / "masters"
SPEC_PATH = str(PROJECT_ROOT / "data" / "specs" / "streamone.json")
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

DEFAULT_COOLDOWN_SECONDS = 30
THREAD_POOL_MAX_WORKERS = 1

logger = logging.getLogger("FirstPassConsole")

# ---------------------------------------------------------------------------
# Run state store (in-memory; single-process)
# ---------------------------------------------------------------------------

_run_lock = threading.Lock()
_runs_lock = threading.Lock()
_runs: Dict[str, Dict[str, Any]] = {}
_last_run_at: float = 0.0
_executor = ThreadPoolExecutor(max_workers=THREAD_POOL_MAX_WORKERS)


def _cooldown_seconds() -> int:
    """Reads cooldown from environment; falls back to default."""
    try:
        return int(os.getenv("CONSOLE_COOLDOWN_SECONDS", str(DEFAULT_COOLDOWN_SECONDS)))
    except (ValueError, TypeError):
        return DEFAULT_COOLDOWN_SECONDS


def _is_any_run_active() -> bool:
    """Returns True if the single-flight lock is currently held."""
    acquired = _run_lock.acquire(blocking=False)
    if acquired:
        _run_lock.release()
    return not acquired


# ---------------------------------------------------------------------------
# Grafana deeplink builder
# ---------------------------------------------------------------------------

def _grafana_base() -> str:
    """Reads GRAFANA_URL from environment at request time. Never cached at module level."""
    return os.getenv("GRAFANA_URL", "").rstrip("/")


def _build_ledger_entries(tool_logs: list) -> list:
    """
    Converts raw tool_logs from the orchestrator into ledger rows for the UI.

    Each row has:
      - timestamp: ISO string (approximated from wall-clock order)
      - operation: human-readable label
      - detail: concise summary
      - href: absolute Grafana URL, or None if identifier missing
      - link_label: visible link text (never the URL)
    """
    grafana = _grafana_base()
    entries = []

    # Pair calls with their subsequent responses by name
    pending_calls: Dict[str, Dict[str, Any]] = {}

    for entry in tool_logs:
        entry_type = entry.get("type")
        name = entry.get("name", "unknown")

        if entry_type == "call":
            pending_calls[name] = entry
            row = _call_to_ledger_row(name, entry.get("args", {}), grafana)
            entries.append(row)

        elif entry_type == "response":
            row = _response_to_ledger_row(name, entry.get("response", {}), grafana)
            if row:
                entries.append(row)

    return entries


def _call_to_ledger_row(name: str, args: dict, grafana: str, timestamp: Optional[str] = None) -> dict:
    """Converts a tool call entry into a ledger display row."""
    op_labels = {
        "create_incident": "Open Incident",
        "add_activity_to_incident": "Post Activity",
        "create_annotation": "Create Annotation",
        "alerting_manage_rules": "Manage Alert Rule",
    }
    label = op_labels.get(name, name)

    detail = ""
    if name == "create_incident":
        detail = args.get("title", "")
    elif name == "add_activity_to_incident":
        body = args.get("body", "")
        detail = body[:80] + "…" if len(body) > 80 else body
    elif name == "create_annotation":
        detail = args.get("text", "")
    elif name == "alerting_manage_rules":
        op = args.get("operation", "")
        title = args.get("title", "")
        detail = f"{op}: {title}" if title else op

    return {
        "timestamp": timestamp or _now_iso(),
        "phase": "call",
        "operation": label,
        "detail": detail,
        "href": None,
        "link_label": None,
    }


def _response_to_ledger_row(name: str, response: Any, grafana: str, timestamp: Optional[str] = None) -> Optional[dict]:
    """
    Converts a tool response entry into a ledger display row with a Grafana deeplink.
    Returns None for responses that add no useful ledger information.
    """
    from agents.orchestrator import unwrap_mcp_response  # noqa: E402
    data = unwrap_mcp_response(response)

    href = None
    link_label = None

    if name == "create_incident" and isinstance(data, dict):
        inc_obj = data.get("incident") if isinstance(data.get("incident"), dict) else data
        inc_id = inc_obj.get("incidentID") or inc_obj.get("id")
        if inc_id and grafana:
            href = f"{grafana}/a/grafana-irm-app/incidents/{inc_id}"
            link_label = f"Incident #{inc_id}"
        detail = f"Created: {inc_obj.get('title', '')}"

    elif name == "create_annotation" and isinstance(data, dict):
        payload = data.get("Payload") if isinstance(data.get("Payload"), dict) else data
        ann_id = payload.get("id")
        dash_uid = "first-pass-delivery-readiness"
        if ann_id and grafana:
            href = f"{grafana}/d/{dash_uid}"
            link_label = f"Annotation #{ann_id} on Dashboard"
        detail = f"Annotation id={ann_id}"

    elif name == "alerting_manage_rules" and isinstance(data, dict):
        rule_uid = data.get("uid") or data.get("id")
        if rule_uid and grafana:
            href = f"{grafana}/alerting/{rule_uid}/view"
            link_label = "Alert rule"
        title = data.get("title", "")
        detail = f"Rule: {title}" if title else f"uid={rule_uid}"

    elif name == "add_activity_to_incident" and isinstance(data, dict):
        act_id = data.get("activityItemID") or data.get("id")
        inc_id = data.get("incidentID")
        if inc_id and grafana:
            href = f"{grafana}/a/grafana-irm-app/incidents/{inc_id}"
            link_label = f"Activity on Incident #{inc_id}"
        detail = f"activityItemID={act_id}"
    else:
        return None

    return {
        "timestamp": timestamp or _now_iso(),
        "phase": "response",
        "operation": "↳ Result",
        "detail": detail,
        "href": href,
        "link_label": link_label,
    }


def _tool_entry_to_ledger_row(entry: dict, grafana: str) -> Optional[dict]:
    """Converts a tool event dict to a ledger row, using its event timestamp."""
    entry_type = entry.get("type")
    name = entry.get("name", "unknown")
    timestamp = entry.get("timestamp")
    if entry_type == "call":
        return _call_to_ledger_row(name, entry.get("args", {}), grafana, timestamp=timestamp)
    elif entry_type == "response":
        return _response_to_ledger_row(name, entry.get("response", {}), grafana, timestamp=timestamp)
    elif entry_type == "verified_rule":
        rule_uid = entry.get("rule_uid")
        title = entry.get("title", "")
        href = f"{grafana}/alerting/{rule_uid}/view" if rule_uid and grafana else None
        return {
            "timestamp": timestamp or _now_iso(),
            "phase": "verified",
            "operation": "Verify Alert Rule",
            "detail": f"Verified rule '{title}' (UID: {rule_uid})" if title else f"Rule UID: {rule_uid}",
            "href": href,
            "link_label": "Alert rule",
        }
    return None


def _now_iso() -> str:
    """Returns current UTC time as an ISO 8601 string."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


# ---------------------------------------------------------------------------
# Background pipeline runner
# ---------------------------------------------------------------------------

def _run_pipeline(run_id: str, master_path: str) -> None:
    """
    Executes run_delivery_qc in a background thread.
    Updates the run store live as tool calls are observed.
    The orchestrator's internal asyncio.run() creates its own event loop
    in this thread, safely isolated from FastAPI's event loop.
    """
    global _last_run_at

    def _on_tool_event(entry: dict) -> None:
        row = _tool_entry_to_ledger_row(entry, _grafana_base())
        if row:
            with _runs_lock:
                if run_id in _runs:
                    new_ledger = list(_runs[run_id]["ledger"])
                    new_ledger.append(row)
                    _runs[run_id]["ledger"] = new_ledger

    try:
        report = run_delivery_qc(master_path, SPEC_PATH, on_tool_event=_on_tool_event)

        findings = report.get("findings", [])
        readiness = report.get("readiness", {})
        india_mode = report.get("india_mode")
        adk_error = report.get("adk_result", {}).get("error") if isinstance(report.get("adk_result"), dict) else None

        with _runs_lock:
            if not _runs[run_id]["ledger"]:
                tool_logs = report.get("adk_result", {}).get("tool_logs", []) if isinstance(report.get("adk_result"), dict) else []
                _runs[run_id]["ledger"] = _build_ledger_entries(tool_logs)

            evaluations = report.get("evaluations", [])
            _runs[run_id].update({
                "status": "done",
                "verdict": report.get("verdict", "UNKNOWN"),
                "blocker_count": report.get("blocker_count", 0),
                "warning_count": report.get("warning_count", 0),
                "master_id": report.get("master_id", ""),
                "spec_id": report.get("spec_id", ""),
                "findings": list(findings),
                "evaluations": list(evaluations),
                "readiness": dict(readiness) if isinstance(readiness, dict) else {},
                "india_mode": india_mode,
                "error": adk_error,
            })
    except Exception as exc:
        logger.exception("Pipeline run %s failed in check engine: %s", run_id, exc)
        with _runs_lock:
            if run_id in _runs:
                _runs[run_id].update({
                    "status": "failed",
                    "verdict": None,
                    "blocker_count": 0,
                    "warning_count": 0,
                    "master_id": "",
                    "findings": [],
                    "readiness": {},
                    "india_mode": None,
                    "error": "Quality check could not be completed. The master or spec data is invalid.",
                })
    finally:
        _last_run_at = time.time()
        _run_lock.release()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="First Pass — Operator Console", docs_url=None, redoc_url=None)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Renders the operator console page."""
    masters = _list_masters()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"masters": masters},
    )


@app.post("/api/fixture")
async def load_fixture(request: Request) -> JSONResponse:
    """
    DEV ONLY — Seeds a pre-computed check-engine state into the run store.

    Accepts a JSON body matching the check-engine report structure plus an
    optional 'master' key (file basename). Returns a run_id that can be
    polled via /api/run/{run_id}. Does NOT start the pipeline or make any
    Vertex AI or Grafana calls.

    Gated by CONSOLE_DEV_FIXTURES=1 environment variable.
    """
    if os.environ.get("CONSOLE_DEV_FIXTURES") != "1":
        raise HTTPException(status_code=404, detail="Not found")

    body = await request.json()
    run_id = str(uuid.uuid4())
    state = {
        "status": body.get("status", "done"),
        "verdict": body.get("verdict", "UNKNOWN"),
        "blocker_count": body.get("blocker_count", 0),
        "warning_count": body.get("warning_count", 0),
        "master_id": body.get("master_id", body.get("master", "")),
        "spec_id": body.get("spec_id", ""),
        "findings": list(body.get("findings", [])),
        "evaluations": list(body.get("evaluations", [])),
        "readiness": dict(body.get("readiness", {})),
        "india_mode": body.get("india_mode"),
        "ledger": list(body.get("ledger", [])),
        "error": body.get("error"),
    }
    with _runs_lock:
        _runs[run_id] = state
    return JSONResponse({"run_id": run_id})


@app.get("/api/masters")
async def list_masters() -> JSONResponse:
    """Returns available master files as JSON."""
    return JSONResponse({"masters": _list_masters()})


@app.post("/api/run")
async def start_run(request: Request) -> JSONResponse:
    """
    Starts a pipeline run in a background thread.

    Returns 409 if a run is currently in progress (single-flight guard).
    Returns 429 if inside the cooldown window between runs.
    Returns {"run_id": "..."} on success.
    """
    global _last_run_at

    body = await request.json()
    master_name = body.get("master")
    if not master_name:
        raise HTTPException(status_code=400, detail="'master' field is required")

    allowed_masters = _list_masters()
    if master_name not in allowed_masters:
        raise HTTPException(status_code=400, detail=f"Invalid master file: {master_name}")

    master_path = MASTERS_DIR / master_name

    # Cooldown check (before lock acquisition to give a clear error message)
    cooldown = _cooldown_seconds()
    elapsed = time.time() - _last_run_at
    if _last_run_at > 0 and elapsed < cooldown:
        retry_after = int(cooldown - elapsed) + 1
        return JSONResponse(
            status_code=429,
            content={
                "detail": f"Cooldown active. Wait {retry_after}s before the next run.",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    # Single-flight check
    acquired = _run_lock.acquire(blocking=False)
    if not acquired:
        return JSONResponse(
            status_code=409,
            content={"detail": "A run is already in progress. Wait for it to complete."},
        )

    run_id = str(uuid.uuid4())
    with _runs_lock:
        _runs[run_id] = {
            "status": "running",
            "verdict": None,
            "blocker_count": 0,
            "warning_count": 0,
            "master_id": master_name,
            "spec_id": "",
            "findings": [],
            "evaluations": [],
            "readiness": {},
            "india_mode": None,
            "ledger": [],
            "error": None,
        }

    _executor.submit(_run_pipeline, run_id, str(master_path))
    return JSONResponse({"run_id": run_id})


@app.get("/api/run/{run_id}")
async def poll_run(run_id: str) -> JSONResponse:
    """Returns the current status and (when done) full result of a run."""
    with _runs_lock:
        state = _runs.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail=f"Run ID not found: {run_id}")
        state_copy = dict(state)
        state_copy["ledger"] = list(state.get("ledger", []))
        state_copy["findings"] = list(state.get("findings", []))
        state_copy["evaluations"] = list(state.get("evaluations", []))
    return JSONResponse(state_copy)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _list_masters() -> list:
    """Returns sorted list of master JSON filenames from data/masters/."""
    if not MASTERS_DIR.exists():
        return []
    return sorted(p.name for p in MASTERS_DIR.glob("*.json"))
