"""
First Pass — Orchestrator Agent (Google ADK & Gemini on Vertex AI)

Main entry point for delivery master quality control evaluation.
Evaluates technical master metadata against platform specification using deterministic check engine,
and interacts with Grafana Cloud via self-hosted MCP server using Google ADK to create incidents.
"""

import os
import sys
import json
import time
import logging
import asyncio
import warnings
import argparse
from typing import Dict, Any, List, Tuple, Optional, Callable

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import mcp
import google.auth
from google.adk import Agent, Runner
from google.adk.models.google_llm import Gemini
from google.adk.sessions import InMemorySessionService
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

from agents.check_engine import evaluate_master_against_spec
from agents.telemetry import validate_telemetry_environment, emit_qc_telemetry


def setup_logging(verbose: bool = False, plain: bool = False) -> None:
    """
    Configures application logging with Rich formatting by default, or stdlib logging when --plain.
    Suppresses third-party noise (httpx, httpcore, ADK experimental warnings, mTLS warnings).
    """
    warnings.filterwarnings("ignore", category=UserWarning)
    warnings.filterwarnings("ignore", message=".*mTLS.*")

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    level = logging.DEBUG if verbose else logging.INFO

    if plain:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            force=True,
        )
    else:
        try:
            from rich.logging import RichHandler
            logging.basicConfig(
                level=level,
                format="%(message)s",
                datefmt="[%X]",
                handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
                force=True,
            )
        except ImportError:
            logging.basicConfig(
                level=level,
                format="%(asctime)s [%(levelname)s] %(message)s",
                force=True,
            )


# Initialize default logging on import
setup_logging()
logger = logging.getLogger("FirstPassOrchestrator")


SEVERITY_MAP = {
    "blocker": "critical",
    "warning": "warning",
}


def map_severity_to_grafana(severity: str) -> str:
    """
    Deterministically maps check_engine severity to Grafana Incident severity vocabulary.
    Fails loudly with ValueError if an unsupported severity is provided.
    """
    normalized = (severity or "").lower()
    if normalized not in SEVERITY_MAP:
        raise ValueError(f"Unsupported check engine severity '{severity}'. Must be one of {list(SEVERITY_MAP.keys())}")
    return SEVERITY_MAP[normalized]


def get_google_auth_credentials() -> Any:
    """
    Resolves Google Cloud Application Default Credentials (ADC).
    Fails loudly with RuntimeError if ADC credentials cannot be resolved.
    """
    try:
        creds, _ = google.auth.default()
        return creds
    except Exception as exc:
        raise RuntimeError(
            "Failed to resolve Google Cloud Application Default Credentials (ADC).\n"
            "Please run 'gcloud auth application-default login' or set GOOGLE_APPLICATION_CREDENTIALS."
        ) from exc


def validate_environment() -> Dict[str, Any]:
    """
    Validates required environment variables for Google Cloud AI, Grafana MCP auth, and Telemetry.
    Fails loudly with RuntimeError if required variables are missing.
    Automatically sets GOOGLE_GENAI_USE_VERTEXAI and GOOGLE_CLOUD_PROJECT for the Google AI SDK.
    """
    env_file = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if os.path.exists(env_file):
        try:
            from dotenv import load_dotenv
            load_dotenv(env_file)
        except ImportError:
            pass

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN")

    missing = []
    if not project_id:
        missing.append("GOOGLE_CLOUD_PROJECT")
    if not token or token == "glsa_REPLACE_ME":
        missing.append("GRAFANA_SERVICE_ACCOUNT_TOKEN")

    if missing:
        raise RuntimeError(
            f"Missing required environment variables for First Pass ADK Orchestrator:\n"
            + "\n".join(f" - {var}" for var in missing)
            + "\nPlease configure your .env file or export them before running."
        )

    # Validate Prometheus Remote-Write and Loki telemetry environment variables
    telemetry_cfg = validate_telemetry_environment()

    # Ensure Google GenAI SDK receives standard Vertex AI configuration
    use_vertex = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = use_vertex
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    os.environ["GOOGLE_CLOUD_LOCATION"] = location

    return {
        "project_id": project_id,
        "location": location,
        "token": token,
        "mcp_url": os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp"),
        "telemetry_cfg": telemetry_cfg,
    }




def load_json_file(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def format_finding_ground_truth(finding: Dict[str, Any]) -> Dict[str, str]:
    """
    Formats exact ground-truth tokens for a finding object.
    Replaces None measured values with 'not present'.
    """
    clause_id = str(finding.get("clause_id", "N/A"))
    clause_text = str(finding.get("clause_text", ""))
    measured = finding.get("measured")
    measured_str = "not present" if measured is None else str(measured)
    expected = finding.get("expected")
    expected_str = "N/A" if expected is None else str(expected)

    return {
        "clause_id": clause_id,
        "severity": str(finding.get("severity", "blocker")),
        "clause_text": clause_text,
        "measured": measured_str,
        "expected": expected_str,
        "language": str(finding.get("language", "")),
        "message": str(finding.get("message", "")),
    }


def extract_text_and_tool_args_from_events(agent_events: List[Any]) -> str:
    """
    Extracts all text content and tool-call argument strings from ADK agent_events.
    """
    extracted_texts = []
    for event in agent_events:
        content = getattr(event, "content", None)
        if content and hasattr(content, "parts"):
            for part in content.parts:
                if hasattr(part, "text") and part.text:
                    extracted_texts.append(part.text)
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    if hasattr(fc, "args") and fc.args:
                        extracted_texts.append(json.dumps(fc.args))

        if hasattr(event, "get_function_calls"):
            try:
                calls = event.get_function_calls()
                for call in calls:
                    if hasattr(call, "args") and call.args:
                        extracted_texts.append(json.dumps(call.args))
            except Exception as exc:
                logger.error(f"Failed to extract function calls from event: {exc}", exc_info=True)
    return "\n".join(extracted_texts)


def extract_add_activity_bodies(agent_events: List[Any]) -> str:
    """Concatenates add_activity_to_incident tool-call body arguments only."""
    bodies: List[str] = []
    for event in agent_events:
        content = getattr(event, "content", None)
        if content and hasattr(content, "parts"):
            for part in content.parts:
                fc = getattr(part, "function_call", None)
                if fc is not None and getattr(fc, "name", None) == "add_activity_to_incident":
                    args = getattr(fc, "args", None) or {}
                    if isinstance(args, dict):
                        bodies.append(str(args.get("body", "")))
        if hasattr(event, "get_function_calls"):
            try:
                for call in event.get_function_calls() or []:
                    if getattr(call, "name", None) == "add_activity_to_incident":
                        args = getattr(call, "args", None) or {}
                        if isinstance(args, dict):
                            bodies.append(str(args.get("body", "")))
            except Exception as exc:
                logger.error(f"Failed to extract activity bodies from event: {exc}", exc_info=True)
    return "\n".join(bodies)


def check_existing_alert_rule(
    grafana_url: str,
    token: str,
    folder_uid: str,
    rule_group: str,
    title: str,
) -> Tuple[str, Optional[str]]:
    """
    Pre-queries Grafana Ruler REST API to check if an alert rule matching title exists in rule_group.
    Returns a tuple (status, uid):
      - ("found", "rule-uid"): Rule exists with specified string UID
      - ("absent", None): Confirmed rule does not exist in folder/group
      - ("failed", None): Pre-query failed (network timeout, HTTP non-200/202/404, invalid response JSON, missing creds)
    """
    if not grafana_url or not token:
        logger.warning("Grafana Ruler API pre-query skipped: missing GRAFANA_URL or GRAFANA_SERVICE_ACCOUNT_TOKEN.")
        return ("failed", None)
    try:
        import requests
        base_url = grafana_url.rstrip("/")
        headers = {"Authorization": f"Bearer {token}"}
        endpoint = f"{base_url}/api/ruler/grafana/api/v1/rules/{folder_uid}"
        resp = requests.get(endpoint, headers=headers, timeout=5)
        if resp.status_code in (200, 202):
            data = resp.json()
            groups = []
            if isinstance(data, dict):
                for k, v in data.items():
                    if k == rule_group and isinstance(v, list):
                        groups.extend(v)
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict) and item.get("name") == rule_group:
                                groups.append(item)
            elif isinstance(data, list):
                groups = [item for item in data if isinstance(item, dict) and item.get("name") == rule_group]

            for grp in groups:
                if isinstance(grp, dict) and "rules" in grp:
                    rules = grp.get("rules", [])
                elif isinstance(grp, dict):
                    rules = [grp]
                else:
                    rules = []
                for rule in rules:
                    if not isinstance(rule, dict):
                        continue
                    g_alert = rule.get("grafana_alert", {})
                    rule_title = g_alert.get("title") or rule.get("title")
                    rule_uid = g_alert.get("uid") or rule.get("uid")
                    if rule_title == title and rule_uid:
                        return ("found", str(rule_uid))
            return ("absent", None)
        elif resp.status_code == 404:
            logger.info("Grafana Ruler API pre-query returned HTTP 404 for folder '%s'; rule is confirmed absent.", folder_uid)
            return ("absent", None)
        else:
            logger.warning("Grafana Ruler API pre-query failed with HTTP status %s: %s", resp.status_code, resp.text)
            return ("failed", None)
    except Exception as exc:
        logger.warning("Grafana Ruler API pre-query failed with exception (%s): %s", type(exc).__name__, exc)
        return ("failed", None)


def ensure_delivery_readiness_dashboard(
    grafana_url: str,
    token: str,
    folder_uid: str,
    dashboard_template: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    """
    Pre-publishes/updates the Delivery Readiness dashboard directly to Grafana API via POST /api/dashboards/db.
    """
    if not grafana_url or not token:
        logger.warning("Direct dashboard push skipped: missing GRAFANA_URL or GRAFANA_SERVICE_ACCOUNT_TOKEN.")
        return (False, None)
    try:
        import requests
        base_url = grafana_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        payload = {
            "dashboard": dashboard_template,
            "folderUid": folder_uid,
            "overwrite": True,
            "message": "Update Delivery Readiness dashboard",
        }
        resp = requests.post(f"{base_url}/api/dashboards/db", json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            uid = data.get("uid") or dashboard_template.get("uid")
            logger.info("Directly published Delivery Readiness dashboard to Grafana (UID: %s)", uid)
            return (True, str(uid))
        else:
            logger.warning("Direct dashboard push returned HTTP %s: %s", resp.status_code, resp.text)
            return (False, None)
    except Exception as exc:
        logger.warning("Direct dashboard push failed with exception (%s): %s", type(exc).__name__, exc)
        return (False, None)


def parse_mcp_response_data(response_val: Any) -> Any:
    """Unwraps inner JSON or text from MCP response payloads."""
    if isinstance(response_val, dict) and "content" in response_val:
        content = response_val.get("content", [])
        if isinstance(content, list) and content:
            item = content[0]
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
def unwrap_mcp_response(response: Any) -> Any:
    """
    Unwraps MCP tool response envelope:
    {'content': [{'type': 'text', 'text': '<json_string>'}]} -> parsed dict, list, or text string.
    """
    if isinstance(response, dict):
        if "content" in response and isinstance(response["content"], list) and len(response["content"]) > 0:
            first = response["content"][0]
            if isinstance(first, dict) and "text" in first and isinstance(first["text"], str):
                text_val = first["text"].strip()
                try:
                    parsed = json.loads(text_val)
                    return parsed
                except Exception:
                    return text_val
        if "data" in response:
            return unwrap_mcp_response(response["data"])
        if "result" in response:
            return unwrap_mcp_response(response["result"])
    return response


def summarize_tool_response(name: str, response: Any) -> str:
    """Constructs a concise, single-line summary of tool response for INFO-level logging."""
    data = unwrap_mcp_response(response)

    if name == "update_dashboard" and isinstance(data, dict):
        status = data.get("status", "N/A")
        uid = data.get("uid") or data.get("id") or "N/A"
        url = data.get("url", "N/A")
        return f"[TOOL RESPONSE] Tool '{name}' returned: status='{status}', uid='{uid}', url='{url}'"

    if name == "create_incident" and isinstance(data, dict):
        inc_obj = data.get("incident") if isinstance(data.get("incident"), dict) else data
        inc_id = inc_obj.get("incidentID") or inc_obj.get("id") or "N/A"
        title = inc_obj.get("title") or "N/A"
        return f"[TOOL RESPONSE] Tool '{name}' returned: incidentID='{inc_id}', title='{title}'"

    if name == "add_activity_to_incident" and isinstance(data, dict):
        act_id = data.get("activityItemID") or data.get("id") or "N/A"
        inc_id = data.get("incidentID") or "N/A"
        kind = data.get("activityKind") or data.get("kind") or "N/A"
        return f"[TOOL RESPONSE] Tool '{name}' returned: activityItemID='{act_id}', incidentID='{inc_id}', kind='{kind}'"

    if name == "create_annotation" and isinstance(data, dict):
        payload = data.get("Payload") if isinstance(data.get("Payload"), dict) else data
        ann_id = payload.get("id") or "N/A"
        msg = payload.get("message") or data.get("message") or "Annotation added"
        return f"[TOOL RESPONSE] Tool '{name}' returned: id={ann_id}, message='{msg}'"

    if name == "alerting_manage_rules" and isinstance(data, dict):
        uid = data.get("uid") or data.get("id") or "N/A"
        title = data.get("title", "N/A")
        return f"[TOOL RESPONSE] Tool '{name}' returned: uid='{uid}', title='{title}'"

    dump_str = json.dumps(data) if isinstance(data, (dict, list)) else str(data)
    if len(dump_str) > 120:
        dump_str = dump_str[:117] + "..."
    return f"[TOOL RESPONSE] Tool '{name}' returned: {dump_str}"


def summarize_tool_call(name: str, args: Dict[str, Any]) -> str:
    """Constructs a concise, single-line summary of tool call arguments for INFO-level logging."""
    if not isinstance(args, dict):
        args_str = str(args)
        if len(args_str) > 120:
            args_str = args_str[:117] + "..."
        return f"[TOOL CALL] Invoked tool '{name}' with arguments: {args_str}"

    if name == "update_dashboard":
        folder_uid = args.get("folderUid", "N/A")
        overwrite = args.get("overwrite", False)
        dash = args.get("dashboard", {}) if isinstance(args.get("dashboard"), dict) else {}
        uid = dash.get("uid", "N/A")
        title = dash.get("title", "N/A")
        panel_count = len(dash.get("panels", [])) if isinstance(dash.get("panels"), list) else 0
        return (
            f"[TOOL CALL] Invoked tool '{name}' with arguments: "
            f"folderUid='{folder_uid}', overwrite={overwrite}, uid='{uid}', title='{title}', panels={panel_count}"
        )

    if name == "create_incident":
        title = args.get("title", "N/A")
        sev = args.get("severity", "N/A")
        room = args.get("roomPrefix", "N/A")
        return f"[TOOL CALL] Invoked tool '{name}' with arguments: title='{title}', severity='{sev}', roomPrefix='{room}'"

    if name == "add_activity_to_incident":
        inc_id = args.get("incidentId", "N/A")
        body = args.get("body", "")
        if len(body) > 80:
            body = body[:77] + "..."
        body_clean = body.replace("\n", " ")
        return f"[TOOL CALL] Invoked tool '{name}' with arguments: incidentId='{inc_id}', body='{body_clean}'"

    if name == "create_annotation":
        dash_uid = args.get("dashboardUid", "N/A")
        text = args.get("text", "N/A")
        ann_time = args.get("time", "N/A")
        return f"[TOOL CALL] Invoked tool '{name}' with arguments: dashboardUid='{dash_uid}', text='{text}', time={ann_time}"

    if name == "alerting_manage_rules":
        operation = args.get("operation", "N/A")
        rule_group = args.get("rule_group", "N/A")
        title = args.get("title", "N/A")
        uid = args.get("uid", "N/A")
        return f"[TOOL CALL] Invoked tool '{name}' with arguments: operation='{operation}', rule_group='{rule_group}', title='{title}', uid='{uid}'"

    dump_str = json.dumps(args)
    if len(dump_str) > 120:
        dump_str = dump_str[:117] + "..."
    return f"[TOOL CALL] Invoked tool '{name}' with arguments: {dump_str}"


def has_function_calls(events: List[Any]) -> bool:
    """Checks whether any ADK event in events contains a function call or response."""
    for ev in events:
        if hasattr(ev, "get_function_calls"):
            try:
                if ev.get_function_calls():
                    return True
            except Exception:
                pass
        if hasattr(ev, "get_function_responses"):
            try:
                if ev.get_function_responses():
                    return True
            except Exception:
                pass
        content = getattr(ev, "content", None)
        if content and hasattr(content, "parts"):
            for part in content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    return True
                if hasattr(part, "function_response") and part.function_response:
                    return True
        ev_str = str(ev)
        if "function_call" in ev_str or "function_response" in ev_str:
            return True
    return False


def extract_event_tool_entries(event: Any) -> List[Dict[str, Any]]:
    """Extracts tool call and response entries from a single ADK event, stamped with wall-clock time."""
    from datetime import datetime, timezone
    ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    entries = []

    calls = []
    if hasattr(event, "get_function_calls"):
        try:
            calls = event.get_function_calls() or []
        except Exception:
            pass
    elif hasattr(event, "content") and getattr(event.content, "parts", None):
        for part in event.content.parts:
            if hasattr(part, "function_call") and part.function_call:
                calls.append(part.function_call)

    for call in calls:
        name = getattr(call, "name", "unknown")
        args = getattr(call, "args", {})
        entries.append({"type": "call", "name": name, "args": args, "timestamp": ts_str})

    responses = []
    if hasattr(event, "get_function_responses"):
        try:
            responses = event.get_function_responses() or []
        except Exception:
            pass
    elif hasattr(event, "content") and getattr(event.content, "parts", None):
        for part in event.content.parts:
            if hasattr(part, "function_response") and part.function_response:
                responses.append(part.function_response)

    for resp in responses:
        name = getattr(resp, "name", "unknown")
        response_val = getattr(resp, "response", {})
        entries.append({"type": "response", "name": name, "response": response_val, "timestamp": ts_str})

    return entries


def inspect_and_log_tool_calls(agent_events: List[Any]) -> List[Dict[str, Any]]:
    """
    Explicitly logs all tool calls captured in ADK agent_events:
    tool name, invocation arguments, and returned responses/results.
    Summarizes tool arguments and responses cleanly at INFO, with raw payloads sent to DEBUG.
    """
    tool_logs = []
    for event in agent_events:
        event_entries = extract_event_tool_entries(event)
        for entry in event_entries:
            if entry["type"] == "call":
                logger.debug(f"[TOOL CALL RAW] Invoked tool '{entry['name']}' with arguments: {json.dumps(entry['args'])}")
                logger.info(summarize_tool_call(entry['name'], entry['args']))
            elif entry["type"] == "response":
                logger.debug(f"[TOOL RESPONSE RAW] Tool '{entry['name']}': {json.dumps(entry['response'])}")
                logger.info(summarize_tool_response(entry['name'], entry['response']))
            tool_logs.append(entry)

    # Audit tool call counts
    incident_calls = [t for t in tool_logs if t["type"] == "call" and t["name"] == "create_incident"]
    annotation_calls = [t for t in tool_logs if t["type"] == "call" and t["name"] == "create_annotation"]
    dashboard_calls = [t for t in tool_logs if t["type"] == "call" and t["name"] == "update_dashboard"]
    alerting_calls = [t for t in tool_logs if t["type"] == "call" and t["name"] == "alerting_manage_rules"]

    if len(incident_calls) > 1:
        logger.error(f"AUDIT FAILURE: Expected at most 1 'create_incident' call per run, found {len(incident_calls)}!")
        raise AssertionError(f"AUDIT FAILURE: Expected at most 1 'create_incident' call per run, found {len(incident_calls)}!")

    if dashboard_calls:
        for d_call in dashboard_calls:
            args = d_call.get("args", {}) if isinstance(d_call.get("args"), dict) else {}
            dash = args.get("dashboard", {}) if isinstance(args.get("dashboard"), dict) else {}
            panel_count = len(dash.get("panels", [])) if isinstance(dash.get("panels"), list) else 0
            uid = dash.get("uid")
            if panel_count == 0 or uid in (None, "N/A", ""):
                logger.error(f"AUDIT FAILURE: 'update_dashboard' called with empty spec or missing UID (panels={panel_count}, uid={uid})!")
                raise AssertionError(f"AUDIT FAILURE: 'update_dashboard' called with empty spec or missing UID (panels={panel_count}, uid={uid})!")
        logger.info(f"AUDIT OK: 'update_dashboard' tool invoked {len(dashboard_calls)} time(s) with valid panel spec.")
    else:
        logger.info("AUDIT INFO: 'update_dashboard' tool was not invoked by agent (dashboard updated directly by Python).")

    if not annotation_calls:
        logger.info("AUDIT INFO: 'create_annotation' tool was not invoked (zero blockers or not requested).")
    else:
        logger.info(f"AUDIT OK: 'create_annotation' tool invoked {len(annotation_calls)} time(s).")

    if not alerting_calls:
        logger.info("AUDIT INFO: 'alerting_manage_rules' tool was not invoked (zero blockers or not requested).")
    else:
        logger.info(f"AUDIT OK: 'alerting_manage_rules' tool invoked {len(alerting_calls)} time(s).")

    # Audit response validity per tool name (ensuring at least one execution succeeded)
    responses_by_tool: Dict[str, List[Any]] = {}
    for resp in [t for t in tool_logs if t["type"] == "response"]:
        r_name = resp["name"]
        responses_by_tool.setdefault(r_name, []).append(resp.get("response", {}))

    for r_name, resp_list in responses_by_tool.items():
        successful_resps = []
        for raw_resp in resp_list:
            data = unwrap_mcp_response(raw_resp)
            is_err = False
            if isinstance(raw_resp, dict) and raw_resp.get("isError"):
                is_err = True
            elif isinstance(data, dict) and (data.get("isError") or "error" in data):
                is_err = True

            if is_err:
                logger.warning(f"Tool '{r_name}' attempt returned error response: {raw_resp}")
                continue

            # Check identifier presence for successful response validation
            if r_name == "update_dashboard":
                status = data.get("status") if isinstance(data, dict) else None
                uid = data.get("uid") or data.get("id") if isinstance(data, dict) else None
                if not status or not uid or status == "N/A" or uid == "N/A":
                    logger.warning(f"Tool '{r_name}' attempt response missing identifiers: {raw_resp}")
                    continue

            if r_name == "create_incident":
                inc_obj = data.get("incident") if isinstance(data.get("incident"), dict) else data
                inc_id = inc_obj.get("incidentID") or inc_obj.get("id") if isinstance(inc_obj, dict) else None
                if not inc_id or inc_id == "N/A":
                    logger.warning(f"Tool '{r_name}' attempt response missing incidentID: {raw_resp}")
                    continue

            if r_name == "add_activity_to_incident":
                act_id = data.get("activityItemID") or data.get("id") if isinstance(data, dict) else None
                if not act_id or act_id == "N/A":
                    logger.warning(f"Tool '{r_name}' attempt response missing activityItemID: {raw_resp}")
                    continue

            if r_name == "create_annotation":
                payload = data.get("Payload") if isinstance(data.get("Payload"), dict) else data
                ann_id = payload.get("id") if isinstance(payload, dict) else None
                if ann_id is None or ann_id == "N/A":
                    logger.warning(f"Tool '{r_name}' attempt response missing annotation id: {raw_resp}")
                    continue

            if r_name == "alerting_manage_rules":
                uid = data.get("uid") or data.get("id") if isinstance(data, dict) else None
                if not uid or uid == "N/A":
                    logger.warning(f"Tool '{r_name}' attempt response missing rule uid: {raw_resp}")
                    continue

            successful_resps.append(raw_resp)

        if not successful_resps:
            logger.error(f"AUDIT FAILURE: Tool '{r_name}' failed all execution attempts. Responses: {resp_list}")
            raise AssertionError(f"AUDIT FAILURE: Tool '{r_name}' failed all execution attempts. Responses: {resp_list}")

        logger.info(f"AUDIT OK: Tool '{r_name}' had {len(successful_resps)}/{len(resp_list)} successful execution(s).")

    return tool_logs


def assert_ground_truth_preservation(agent_events: List[Any], findings: List[Dict[str, Any]]) -> None:
    """
    Asserts that every ground-truth clause ID and measured value appears verbatim in the agent's
    final response or captured tool-call arguments within agent_events, and specifically in
    add_activity_to_incident bodies (the ranked fix-plan activity).
    Raises AssertionError if any ground truth token is missing.
    """
    combined_output = extract_text_and_tool_args_from_events(agent_events)
    activity_bodies = extract_add_activity_bodies(agent_events)

    for finding in findings:
        truth = format_finding_ground_truth(finding)
        clause_id = truth["clause_id"]
        measured = truth["measured"]

        if clause_id not in combined_output:
            raise AssertionError(
                f"Ground-truth clause ID '{clause_id}' missing from agent response and captured tool calls!"
            )
        if measured not in combined_output and measured != "not present":
            raise AssertionError(
                f"Ground-truth measured value '{measured}' missing from agent response and captured tool calls!"
            )
        if clause_id not in activity_bodies:
            raise AssertionError(
                f"Ground-truth clause ID '{clause_id}' missing from add_activity_to_incident body!"
            )
        if measured not in activity_bodies and measured != "not present":
            raise AssertionError(
                f"Ground-truth measured value '{measured}' missing from add_activity_to_incident body!"
            )


def assert_dashboard_metrics_verbatim(
    agent_events: List[Any], user_prompt: str = "", dashboard_template: Optional[Dict[str, Any]] = None
) -> None:
    """
    Asserts that exact telemetry metric names and label keys appear verbatim in published
    dashboard spec, captured tool-call arguments, agent events, or user prompt.
    """
    dash_str = json.dumps(dashboard_template) if dashboard_template else ""
    combined_output = dash_str + "\n" + user_prompt + "\n" + extract_text_and_tool_args_from_events(agent_events)
    required_tokens = [
        "qc_blockers_current",
        "qc_checks",
        "domain",
        "result",
        "qc_loudness_deviation_lufs",
        "qc_readiness_ratio",
        "language",
        "job",
        "first-pass-qc",
    ]
    for token in required_tokens:
        if token not in combined_output:
            raise AssertionError(
                f"Required telemetry token '{token}' missing from dashboard spec, agent response, and captured tool calls!"
            )


async def run_adk_orchestration(
    report: Dict[str, Any],
    env_cfg: Dict[str, str],
    on_tool_event: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Asynchronously executes the Google ADK Agent workflow to manage dashboards and incidents on Grafana Cloud MCP.
    """
    mcp_url = env_cfg["mcp_url"]
    token = env_cfg["token"]

    logger.info(f"Connecting Google ADK McpToolset to Grafana MCP at {mcp_url}...")
    connection_params = StreamableHTTPConnectionParams(
        url=mcp_url,
        headers={"Authorization": f"Bearer {token}"},
    )
    mcp_toolset = McpToolset(
        connection_params=connection_params,
        tool_filter=[
            "create_incident",
            "add_activity_to_incident",
            "create_annotation",
            "alerting_manage_rules",
        ],
    )

    try:
        master_id = report.get("master_id", "UNKNOWN")
        blocker_count = report.get("blocker_count", 0)
        findings = report.get("findings", [])

        # Programmatically map highest severity
        has_blockers = blocker_count > 0
        mapped_severity = map_severity_to_grafana("blocker" if has_blockers else "warning")

        # Ground truth prompt context
        formatted_findings = [format_finding_ground_truth(f) for f in findings]
        clause_ids = [f["clause_id"] for f in formatted_findings]
        prompt_data = {
            "master_id": master_id,
            "verdict": report.get("verdict"),
            "blocker_count": blocker_count,
            "mapped_severity": mapped_severity,
            "findings": formatted_findings,
            "ranked_fix_plan": report.get("ranked_fix_plan") or {"jobs": []},
            "room_prefix": "first-pass",
        }

        prompt_json = json.dumps(prompt_data, indent=2)

        # Dashboard JSON template with range queries, human-readable legend formats, and formatted log table
        dashboard_template = {
            "uid": "first-pass-delivery-readiness",
            "title": "Delivery Readiness",
            "schemaVersion": 36,
            "editable": True,
            "time": {"from": "now-24h", "to": "now"},
            "annotations": {
                "list": [
                    {
                        "builtIn": 1,
                        "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                        "enable": True,
                        "hide": False,
                        "name": "Annotations & Alerts",
                        "type": "dashboard",
                    }
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "title": "Current Delivery Blockers",
                    "type": "stat",
                    "gridPos": {"x": 0, "y": 0, "w": 6, "h": 6},
                    "targets": [
                        {
                            "datasource": {"type": "prometheus", "uid": "grafanacloud-prom"},
                            "expr": "qc_blockers_current",
                            "refId": "A",
                        }
                    ],
                    "fieldConfig": {
                        "defaults": {
                            "thresholds": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "green", "value": None},
                                    {"color": "red", "value": 1},
                                ],
                            }
                        }
                    },
                },
                {
                    "id": 2,
                    "title": "QC Checks by Domain & Outcome",
                    "type": "barchart",
                    "gridPos": {"x": 6, "y": 0, "w": 9, "h": 6},
                    "targets": [
                        {
                            "datasource": {"type": "prometheus", "uid": "grafanacloud-prom"},
                            "expr": "last_over_time(qc_checks[$__range])",
                            "instant": True,
                            "legendFormat": "{{domain}} · {{result}}",
                            "refId": "A",
                        }
                    ],
                },
                {
                    "id": 3,
                    "title": "Audio Loudness Deviation (LUFS)",
                    "description": "Integrated loudness deviation against -27.0 ± 2.0 LUFS target",
                    "type": "bargauge",
                    "gridPos": {"x": 15, "y": 0, "w": 9, "h": 6},
                    "options": {"orientation": "horizontal", "displayMode": "lcd"},
                    "targets": [
                        {
                            "datasource": {"type": "prometheus", "uid": "grafanacloud-prom"},
                            "expr": "last_over_time(qc_loudness_deviation_lufs[$__range])",
                            "instant": True,
                            "legendFormat": "{{language}}",
                            "refId": "A",
                        }
                    ],
                    "fieldConfig": {
                        "defaults": {
                            "unit": "LUFS",
                            "decimals": 1,
                            "thresholds": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "red", "value": None},
                                    {"color": "green", "value": -2.0},
                                    {"color": "red", "value": 2.001},
                                ],
                            },
                        },
                        "overrides": [
                            {
                                "matcher": {"id": "byName", "options": "ta-IN"},
                                "properties": [
                                    {"id": "prefix", "value": "+"}
                                ],
                            }
                        ],
                    },
                },
                {
                    "id": 6,
                    "title": "Per-Language Delivery Readiness",
                    "description": "Delivery readiness ratio per language (0.0 to 1.0)",
                    "type": "bargauge",
                    "gridPos": {"x": 0, "y": 6, "w": 12, "h": 6},
                    "options": {"orientation": "horizontal", "displayMode": "basic"},
                    "targets": [
                        {
                            "datasource": {"type": "prometheus", "uid": "grafanacloud-prom"},
                            "expr": "last_over_time(qc_readiness_ratio[$__range])",
                            "instant": True,
                            "legendFormat": "{{language}}",
                            "refId": "A",
                        }
                    ],
                    "fieldConfig": {
                        "defaults": {
                            "min": 0.0,
                            "max": 1.0,
                            "decimals": 3,
                            "color": {"mode": "thresholds"},
                            "thresholds": {
                                "mode": "absolute",
                                "steps": [
                                    {"color": "red", "value": None},
                                    {"color": "yellow", "value": 0.667},
                                    {"color": "green", "value": 1.0},
                                ],
                            },
                        },
                    },
                },
                {
                    "id": 5,
                    "title": "Active Blockers History",
                    "type": "timeseries",
                    "gridPos": {"x": 12, "y": 6, "w": 12, "h": 6},
                    "targets": [
                        {
                            "datasource": {"type": "prometheus", "uid": "grafanacloud-prom"},
                            "expr": "qc_blockers_current",
                            "legendFormat": "Blockers",
                            "refId": "A",
                        }
                    ],
                    "fieldConfig": {
                        "defaults": {
                            "custom": {
                                "drawStyle": "line",
                                "lineInterpolation": "linear",
                                "showPoints": "always",
                                "pointSize": 6,
                                "spanNulls": False,
                            }
                        }
                    },
                },
                {
                    "id": 4,
                    "title": "QC Findings Log Stream",
                    "type": "table",
                    "gridPos": {"x": 0, "y": 12, "w": 24, "h": 10},
                    "options": {
                        "cellHeight": "md",
                        "footer": {"show": False},
                        "showHeader": True,
                        "wrapText": True,
                    },
                    "fieldConfig": {
                        "defaults": {
                            "custom": {
                                "align": "auto",
                                "inspect": True,
                            }
                        },
                        "overrides": [
                            {
                                "matcher": {"id": "byName", "options": "Time"},
                                "properties": [
                                    {"id": "custom.width", "value": 170}
                                ],
                            },
                            {
                                "matcher": {"id": "byName", "options": "Clause"},
                                "properties": [
                                    {"id": "custom.width", "value": 80}
                                ],
                            },
                            {
                                "matcher": {"id": "byName", "options": "Severity"},
                                "properties": [
                                    {"id": "custom.width", "value": 90}
                                ],
                            },
                            {
                                "matcher": {"id": "byName", "options": "Measured"},
                                "properties": [
                                    {"id": "custom.width", "value": 110}
                                ],
                            },
                            {
                                "matcher": {"id": "byName", "options": "Expected"},
                                "properties": [
                                    {"id": "custom.width", "value": 180}
                                ],
                            },
                            {
                                "matcher": {"id": "byName", "options": "Language"},
                                "properties": [
                                    {"id": "custom.width", "value": 90}
                                ],
                            },
                            {
                                "matcher": {"id": "byName", "options": "Finding Description"},
                                "properties": [
                                    {"id": "custom.minWidth", "value": 450}
                                ],
                            },
                        ],
                    },
                    "targets": [
                        {
                            "datasource": {"type": "loki", "uid": "grafanacloud-logs"},
                            "expr": '{job="first-pass-qc"} | json',
                            "refId": "A",
                        }
                    ],
                    "transformations": [
                        {
                            "id": "extractFields",
                            "options": {"source": "Line", "format": "json"},
                        },
                        {
                            "id": "organize",
                            "options": {
                                "excludeByName": {
                                    "Line": True,
                                    "tsNs": True,
                                    "id": True,
                                    "labels": True,
                                    "labelTypes": True,
                                    "run_id": True,
                                    "traceID": True,
                                    "traceID (field)": True,
                                },
                                "indexByName": {
                                    "Time": 0,
                                    "clause_id": 1,
                                    "severity": 2,
                                    "measured": 3,
                                    "expected": 4,
                                    "language": 5,
                                    "message": 6,
                                },
                                "renameByName": {
                                    "Time": "Time",
                                    "clause_id": "Clause",
                                    "severity": "Severity",
                                    "measured": "Measured",
                                    "expected": "Expected",
                                    "language": "Language",
                                    "message": "Finding Description",
                                },
                            },
                        },
                        {
                            "id": "sortBy",
                            "options": {
                                "fields": {},
                                "sort": [
                                    {
                                        "field": "Time",
                                        "desc": True,
                                    }
                                ],
                            },
                        },
                    ],
                },
            ],
        }

        dashboard_json = json.dumps(dashboard_template, indent=2)

        # Ground truth findings lines for prompt
        findings_bullets = "\n".join(
            f"- [{f['clause_id']}] measured {f['measured']}, expected {f['expected']}"
            for f in formatted_findings
        )
        findings_summary = ", ".join(
            f"{f['clause_id']} measured {f['measured']}" for f in formatted_findings
        )

        grafana_url = os.getenv("GRAFANA_URL", "")
        ensure_delivery_readiness_dashboard(
            grafana_url=grafana_url,
            token=token,
            folder_uid="first-pass-qc",
            dashboard_template=dashboard_template,
        )

        rule_title = "First Pass - Delivery Blockers Present"
        rule_status, existing_rule_uid = check_existing_alert_rule(
            grafana_url=grafana_url,
            token=token,
            folder_uid="first-pass-qc",
            rule_group="first-pass-alerts",
            title=rule_title,
        )

        alert_rule_data = [
            {
                "refId": "A",
                "datasourceUid": "grafanacloud-prom",
                "model": {"expr": "qc_blockers_current"},
            },
            {
                "refId": "B",
                "datasourceUid": "__expr__",
                "model": {
                    "type": "threshold",
                    "expression": "A",
                    "conditions": [{"evaluator": {"type": "gt", "params": [0]}}],
                },
            },
        ]

        if rule_status == "found":
            logger.info(
                f"Alert rule '{rule_title}' already exists in Grafana (UID: {existing_rule_uid}). "
                "Skipping duplicate 'alerting_manage_rules' write call to prevent provisioning API 409 Conflict."
            )
            alerting_instruction = (
                f"Do NOT call `alerting_manage_rules` for this run because alert rule '{rule_title}' "
                f"already exists in Grafana (UID: {existing_rule_uid})."
            )
            if on_tool_event and existing_rule_uid:
                try:
                    from datetime import datetime, timezone
                    ts_str = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                    on_tool_event({
                        "type": "verified_rule",
                        "name": "alerting_manage_rules",
                        "rule_uid": existing_rule_uid,
                        "title": rule_title,
                        "timestamp": ts_str,
                    })
                except Exception as exc:
                    logger.warning(f"Error emitting verified_rule event: {exc}")
        elif rule_status == "absent":
            alerting_instruction = (
                f"Call `alerting_manage_rules` with operation: \"create\", "
                f"title=\"{rule_title}\", folder_uid=\"first-pass-qc\", rule_group=\"first-pass-alerts\", "
                f"condition=\"B\", for=\"1m\", org_id=1, no_data_state=\"OK\", exec_err_state=\"Alerting\", "
                f"data={json.dumps(alert_rule_data)}"
            )
        else:
            logger.warning(
                "Grafana Ruler API pre-query status is 'failed'. Skipping alerting_manage_rules instruction "
                "for this run to prevent duplicate alert rule creation."
            )
            alerting_instruction = "Skip calling `alerting_manage_rules` for this run because Grafana Ruler API pre-query failed."

        ranked_plan_json = json.dumps(report.get("ranked_fix_plan") or {"jobs": []}, indent=2)

        user_prompt = f"""
A technical master delivery evaluation completed for master ID '{master_id}' with verdict {report.get('verdict')} ({blocker_count} blockers).

Ground Truth Findings:
{findings_bullets}

Ranked Fix Plan (deterministic JSON — do not reorder, invent, or drop jobs or items):
{ranked_plan_json}

The Delivery Readiness dashboard (UID: 'first-pass-delivery-readiness') has already been updated directly by Python.

Please execute the following tool calls in order:

1. If blocker_count > 0 ({blocker_count} blockers present):
   a. Call `create_incident` tool with title "Delivery Blocker: {master_id} ({blocker_count} Spec Non-Conformances)", severity "{mapped_severity}", roomPrefix "first-pass".
   b. Call `add_activity_to_incident` tool using the returned incidentID. Body must be an operator-readable ranked fix plan that follows the Ranked Fix Plan JSON in the same job order, with every clause_id and every measured/expected value copied exactly. Do not reorder, invent, or drop items.
   c. Call `create_annotation` tool with arguments:
      - dashboardUID: "first-pass-delivery-readiness"
      - text: "Violated clauses: {', '.join(clause_ids)}"
      - time: {int(time.time() * 1000)}
   d. {alerting_instruction}
   If blocker_count == 0, do NOT call create_incident, add_activity_to_incident, create_annotation, or alerting_manage_rules.

2. In your final response, summarize the actions taken, retaining exact clause IDs ({', '.join(clause_ids)}), metric names, measured values, and expected values verbatim.
"""

        logger.info("Initializing Google ADK Gemini model & LlmAgent (model: gemini-2.5-flash)...")
        creds = get_google_auth_credentials()
        gemini_model = Gemini(model="gemini-2.5-flash", client_kwargs={"credentials": creds})

        forced_tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.ANY,
                allowed_function_names=None,
            )
        )
        gen_content_config = types.GenerateContentConfig(tool_config=forced_tool_config)

        turn_counter = [0]

        def before_model_callback(callback_context: Any, llm_request: Any) -> None:
            time.sleep(1)
            turn_counter[0] += 1
            current_turn = turn_counter[0]

            if (
                getattr(llm_request, "config", None)
                and getattr(llm_request.config, "tool_config", None)
                and getattr(llm_request.config.tool_config, "function_calling_config", None)
            ):
                fcc = llm_request.config.tool_config.function_calling_config
                if blocker_count > 0 and current_turn == 1:
                    fcc.mode = types.FunctionCallingConfigMode.ANY
                    fcc.allowed_function_names = None
                else:
                    fcc.mode = types.FunctionCallingConfigMode.AUTO
                    fcc.allowed_function_names = None
                logger.info(f"[TURN {current_turn}] Function calling mode set to: {fcc.mode}")

        agent = Agent(
            name="FirstPassOrchestrator",
            model=gemini_model,
            instruction=(
                "You are an automated delivery Quality Control orchestrator. Execute actions strictly by calling the provided tools. "
                "Do NOT respond in plain prose or conversational text. You MUST call the tools (`create_incident`, `add_activity_to_incident`, `create_annotation`, `alerting_manage_rules`) as instructed. "
                "If any tool returns an error, ignore the tool error and continue executing the remaining tool calls."
            ),
            tools=[mcp_toolset],
            generate_content_config=gen_content_config,
            before_model_callback=before_model_callback,
        )

        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="first-pass", session_service=session_service)

        max_attempts = 2
        agent_events = []
        agent_events = []

        for attempt in range(1, max_attempts + 1):
            logger.info(
                f"Executing Google ADK runner with Gemini 2.5 Flash on Vertex AI (attempt {attempt}/{max_attempts})..."
            )
            session = await session_service.create_session(app_name="first-pass", user_id=f"operator-{attempt}-{int(time.time())}")
            session_id = session.id

            new_message = types.Content(
                parts=[types.Part.from_text(text=user_prompt)], role="user"
            )

            attempt_events = []
            try:
                async for event in runner.run_async(
                    user_id=session.user_id, session_id=session_id, new_message=new_message
                ):
                    attempt_events.append(event)
                    logger.debug(f"ADK Event received: {event}")
                    if on_tool_event:
                        for entry in extract_event_tool_entries(event):
                            try:
                                on_tool_event(entry)
                            except Exception as exc:
                                logger.warning(f"Error in on_tool_event callback: {exc}")
            except Exception as exc:
                if attempt < max_attempts:
                    wait_sec = 60 * attempt
                    logger.warning(
                        f"ADK Agent attempt {attempt}/{max_attempts} hit transient error ({type(exc).__name__}: {exc}). "
                        f"Backing off for {wait_sec}s before retrying..."
                    )
                    await asyncio.sleep(wait_sec)
                    continue
                raise

            if blocker_count == 0 or has_function_calls(attempt_events):
                agent_events = attempt_events
                break

            if attempt < max_attempts:
                logger.warning(
                    f"ADK Agent attempt {attempt}/{max_attempts} produced zero tool calls (prose response detected). "
                    f"Retrying orchestration (attempt {attempt + 1}/{max_attempts})..."
                )
                await asyncio.sleep(5)
            else:
                agent_events = attempt_events
                logger.error(f"ADK Agent made no tool calls after {max_attempts} attempts.")
                raise RuntimeError(f"ADK Agent made no tool calls after {max_attempts} attempts.")

        logger.info("ADK execution completed. Inspecting and logging all tool calls captured in agent_events...")
        tool_logs = inspect_and_log_tool_calls(agent_events)

        logger.info("Asserting ground truth preservation against model response and tool calls...")
        assert_ground_truth_preservation(agent_events, findings)

        logger.info("Asserting dashboard metric tokens verbatim in tool calls...")
        assert_dashboard_metrics_verbatim(agent_events, user_prompt, dashboard_template)

        logger.info("Ground truth and metric tokens verified successfully.")
        return {"status": "ok", "events_count": len(agent_events), "tool_logs": tool_logs}

    finally:
        logger.info("Closing McpToolset connection...")
        await mcp_toolset.close()


def run_delivery_qc(
    master_path: str,
    spec_path: str,
    on_tool_event: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Executes a complete delivery QC evaluation run.
    """
    env_cfg = validate_environment()

    logger.info(f"Ingesting master metadata: {master_path}")
    master = load_json_file(master_path)

    logger.info(f"Ingesting delivery specification: {spec_path}")
    spec = load_json_file(spec_path)

    logger.info("Executing deterministic QC check engine...")
    report = evaluate_master_against_spec(master, spec)

    logger.info("Emitting QC telemetry to Grafana Cloud (Prometheus remote-write & Loki push)...")
    telemetry_res = emit_qc_telemetry(report, env_cfg=env_cfg.get("telemetry_cfg"))
    report["telemetry_result"] = telemetry_res

    logger.info(f"QC Run Finished. Verdict: {report['verdict']} (Blockers: {report['blocker_count']})")

    logger.info("Triggering Google ADK Orchestrator workflow for folder/dashboard and incident management...")
    try:
        adk_result = asyncio.run(run_adk_orchestration(report, env_cfg, on_tool_event=on_tool_event))
        report["adk_result"] = adk_result
    except Exception as exc:
        logger.error(
            f"ADK Orchestration hit an error ({type(exc).__name__}: {exc}). "
            "Preserving deterministic QC check engine verdict and report.",
            exc_info=True,
        )
        report["adk_result"] = {"status": "error", "error": str(exc), "tool_logs": []}

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="First Pass Quality Control ADK Orchestrator")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose DEBUG logging")
    parser.add_argument("--plain", action="store_true", help="Use plain stdlib logging output instead of Rich formatting")
    parser.add_argument("--master", "-m", help="Path to master JSON metadata file")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose, plain=args.plain)

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    master_file = os.path.abspath(args.master) if args.master else os.path.join(base_dir, "data", "masters", "master_blockers.json")
    default_spec = os.path.join(base_dir, "data", "specs", "streamone.json")

    report = run_delivery_qc(master_file, default_spec)
    print("\n" + "=" * 60)
    print(f"QC Evaluation Complete: {report['master_id']}")
    print(f"Verdict: {report['verdict']}")
    print(f"Blocker Count: {report['blocker_count']}")
    plan = report.get("ranked_fix_plan") or {}
    jobs = plan.get("jobs") or []
    if jobs:
        print("Ranked fix plan:")
        for i, job in enumerate(jobs, start=1):
            ids = ", ".join(str(c) for c in job.get("clause_ids") or [])
            print(
                f"  {i}. {job.get('remediation_stage')} "
                f"(severity={job.get('severity')}, fanout={job.get('language_fanout')}): {ids}"
            )
    print("=" * 60)


