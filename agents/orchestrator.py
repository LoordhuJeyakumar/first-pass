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
from typing import Dict, Any, List

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
                raise

    return "\n".join(extracted_texts)


def parse_mcp_response_data(response_val: Any) -> Any:
    """Unwraps inner JSON or text from MCP response payloads."""
    if isinstance(response_val, dict) and "content" in response_val:
        content = response_val.get("content", [])
        if isinstance(content, list) and content:
            item = content[0]
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text", "")
                try:
                    return json.loads(text)
                except Exception:
                    return text
    return response_val


def summarize_tool_response(name: str, response_val: Any) -> str:
    """Constructs a concise, single-line summary of a tool response for INFO-level logging."""
    data = parse_mcp_response_data(response_val)

    if name == "create_incident" and isinstance(data, dict):
        inc_id = data.get("incidentID", "N/A")
        sev = data.get("severity", "N/A")
        status = data.get("status", "N/A")
        creator = data.get("createdByUser", {}).get("name", "N/A")
        return f"[TOOL RESPONSE] Tool '{name}' returned: incidentID={inc_id}, severity={sev}, status={status}, createdBy='{creator}'"

    if name == "update_dashboard" and isinstance(data, dict):
        status = data.get("status", "N/A")
        uid = data.get("uid", "N/A")
        folder_uid = data.get("folderUid", "N/A")
        url = data.get("url", "")
        return f"[TOOL RESPONSE] Tool '{name}' returned: status={status}, uid='{uid}', folderUid='{folder_uid}', url='{url}'"

    if name == "add_activity_to_incident" and isinstance(data, dict):
        act_id = data.get("activityItemID", "N/A")
        inc_id = data.get("incidentID", "N/A")
        kind = data.get("activityKind", "N/A")
        return f"[TOOL RESPONSE] Tool '{name}' returned: activityItemID='{act_id}', incidentID='{inc_id}', kind='{kind}'"

    if name == "create_annotation" and isinstance(data, dict):
        payload = data.get("Payload", data)
        ann_id = payload.get("id", "N/A") if isinstance(payload, dict) else "N/A"
        msg = payload.get("message", "Annotation added") if isinstance(payload, dict) else str(payload)
        return f"[TOOL RESPONSE] Tool '{name}' returned: id={ann_id}, message='{msg}'"

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

    dump_str = json.dumps(args)
    if len(dump_str) > 120:
        dump_str = dump_str[:117] + "..."
    return f"[TOOL CALL] Invoked tool '{name}' with arguments: {dump_str}"


def has_function_calls(events: List[Any]) -> bool:
    """Checks whether any ADK event in events contains a function call."""
    for ev in events:
        if hasattr(ev, "get_function_calls"):
            try:
                calls = ev.get_function_calls()
                if calls:
                    return True
            except Exception:
                pass
        content = getattr(ev, "content", None)
        if content and hasattr(content, "parts"):
            for part in content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    return True
    return False


def inspect_and_log_tool_calls(agent_events: List[Any]) -> List[Dict[str, Any]]:
    """
    Explicitly logs all tool calls captured in ADK agent_events:
    tool name, invocation arguments, and returned responses/results.
    Summarizes tool arguments and responses cleanly at INFO, with raw payloads sent to DEBUG.
    """
    tool_logs = []
    for event in agent_events:
        # Check function calls
        calls = []
        if hasattr(event, "get_function_calls"):
            try:
                calls = event.get_function_calls() or []
            except Exception as exc:
                logger.error(f"Error calling get_function_calls on event: {exc}", exc_info=True)
                raise
        elif hasattr(event, "content") and getattr(event.content, "parts", None):
            for part in event.content.parts:
                if hasattr(part, "function_call") and part.function_call:
                    calls.append(part.function_call)

        for call in calls:
            name = getattr(call, "name", "unknown")
            args = getattr(call, "args", {})
            logger.debug(f"[TOOL CALL RAW] Invoked tool '{name}' with arguments: {json.dumps(args)}")
            logger.info(summarize_tool_call(name, args))
            tool_logs.append({"type": "call", "name": name, "args": args})

        # Check function responses
        responses = []
        if hasattr(event, "get_function_responses"):
            try:
                responses = event.get_function_responses() or []
            except Exception as exc:
                logger.error(f"Error calling get_function_responses on event: {exc}", exc_info=True)
                raise
        elif hasattr(event, "content") and getattr(event.content, "parts", None):
            for part in event.content.parts:
                if hasattr(part, "function_response") and part.function_response:
                    responses.append(part.function_response)

        for resp in responses:
            name = getattr(resp, "name", "unknown")
            response_val = getattr(resp, "response", {})
            logger.debug(f"[TOOL RESPONSE RAW] Tool '{name}': {json.dumps(response_val)}")
            logger.info(summarize_tool_response(name, response_val))
            tool_logs.append({"type": "response", "name": name, "response": response_val})

    # Audit create_annotation and update_dashboard calls explicitly
    annotation_calls = [t for t in tool_logs if t["type"] == "call" and t["name"] == "create_annotation"]
    annotation_responses = [t for t in tool_logs if t["type"] == "response" and t["name"] == "create_annotation"]
    dashboard_calls = [t for t in tool_logs if t["type"] == "call" and t["name"] == "update_dashboard"]

    if not dashboard_calls:
        logger.error("AUDIT WARNING: 'update_dashboard' tool was NEVER invoked by the agent during execution!")
    else:
        logger.info(f"AUDIT OK: 'update_dashboard' tool invoked {len(dashboard_calls)} time(s).")

    if not annotation_calls:
        logger.info("AUDIT INFO: 'create_annotation' tool was not invoked (zero blockers or not requested).")
    else:
        logger.info(f"AUDIT OK: 'create_annotation' tool invoked {len(annotation_calls)} time(s).")
        for resp in annotation_responses:
            resp_data = resp.get("response", {})
            is_err = False
            if isinstance(resp_data, dict):
                if resp_data.get("isError") or "error" in resp_data:
                    is_err = True
            elif "error" in str(resp_data).lower() and "message" not in str(resp_data).lower():
                is_err = True

            if is_err:
                logger.error(f"AUDIT FAILURE: 'create_annotation' returned an error: {resp_data}")

    return tool_logs


def assert_ground_truth_preservation(agent_events: List[Any], findings: List[Dict[str, Any]]) -> None:
    """
    Asserts that every ground-truth clause ID and measured value appears verbatim in the agent's
    final response or captured tool-call arguments within agent_events.
    Raises AssertionError if any ground truth token is missing.
    """
    combined_output = extract_text_and_tool_args_from_events(agent_events)

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


def assert_dashboard_metrics_verbatim(agent_events: List[Any]) -> None:
    """
    Asserts that exact telemetry metric names and label keys appear verbatim in captured
    tool-call arguments for update_dashboard or agent events.
    """
    combined_output = extract_text_and_tool_args_from_events(agent_events)
    required_tokens = [
        "qc_blockers_current",
        "qc_checks",
        "domain",
        "result",
        "qc_loudness_deviation_lufs",
        "language",
        "job",
        "first-pass-qc",
    ]
    for token in required_tokens:
        if token not in combined_output:
            raise AssertionError(
                f"Required telemetry token '{token}' missing from agent response and captured tool calls!"
            )


async def run_adk_orchestration(
    report: Dict[str, Any], env_cfg: Dict[str, str]
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
            "update_dashboard",
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
                    "id": 4,
                    "title": "QC Findings Log Stream",
                    "type": "table",
                    "gridPos": {"x": 0, "y": 6, "w": 24, "h": 10},
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

        dashboard_json = json.dumps(dashboard_template)

        # Ground truth findings lines for prompt
        findings_bullets = "\n".join(
            f"- [{f['clause_id']}] measured {f['measured']}, expected {f['expected']}"
            for f in formatted_findings
        )

        user_prompt = f"""
A technical master delivery evaluation completed for master ID '{master_id}' with verdict {report.get('verdict')} ({blocker_count} blockers).

Ground Truth Findings:
{findings_bullets}

Please execute the following tool calls in order:

1. Call `update_dashboard` tool with arguments:
   - folderUid: "first-pass-qc"
   - overwrite: true
   - message: "Update Delivery Readiness dashboard for master {master_id}"
   - dashboard: {dashboard_json}

2. If blocker_count > 0 ({blocker_count} blockers present):
   a. Call `create_incident` tool with title "Delivery Blocker: {master_id} ({blocker_count} Spec Non-Conformances)", severity "{mapped_severity}", roomPrefix "first-pass".
   b. Call `add_activity_to_incident` tool using the returned incidentID with findings details verbatim.
   c. Call `create_annotation` tool with arguments:
      - dashboardUID: "first-pass-delivery-readiness"
      - text: "Violated clauses: {', '.join(clause_ids)}"
      - time: {int(time.time() * 1000)}
   If blocker_count == 0, do NOT call create_incident, add_activity_to_incident, or create_annotation.

3. In your final response, summarize the actions taken, retaining exact clause IDs ({', '.join(clause_ids)}), metric names, measured values, and expected values verbatim.
"""

        logger.info("Initializing Google ADK Gemini model & LlmAgent (model: gemini-2.5-flash)...")
        creds = get_google_auth_credentials()
        gemini_model = Gemini(model="gemini-2.5-flash", client_kwargs={"credentials": creds})

        agent = Agent(
            name="FirstPassOrchestrator",
            model=gemini_model,
            instruction=(
                "You are an automated delivery Quality Control orchestrator. Execute actions strictly by making native tool function calls. "
                "If any tool returns an error (such as HTTP 412 indicating a folder already exists), ignore the tool error and continue executing the remaining tool calls."
            ),
            tools=[mcp_toolset],
        )

        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="first-pass", session_service=session_service)

        max_attempts = 2
        agent_events = []

        for attempt in range(1, max_attempts + 1):
            logger.info(
                f"Executing Google ADK runner with Gemini 2.5 Flash on Vertex AI (attempt {attempt}/{max_attempts})..."
            )
            session = await session_service.create_session(app_name="first-pass", user_id="operator")
            session_id = session.id

            new_message = types.Content(
                parts=[types.Part.from_text(text=user_prompt)], role="user"
            )

            attempt_events = []
            async for event in runner.run_async(
                user_id="operator", session_id=session_id, new_message=new_message
            ):
                attempt_events.append(event)
                logger.debug(f"ADK Event received: {event}")

            if has_function_calls(attempt_events):
                agent_events = attempt_events
                break

            if attempt < max_attempts:
                logger.warning(
                    f"ADK Agent attempt {attempt}/{max_attempts} produced zero tool calls (prose response detected). "
                    f"Retrying orchestration (attempt {attempt + 1}/{max_attempts})..."
                )
            else:
                agent_events = attempt_events
                logger.error(f"ADK Agent made no tool calls after {max_attempts} attempts.")
                raise RuntimeError(f"ADK Agent made no tool calls after {max_attempts} attempts.")

        logger.info("ADK execution completed. Inspecting and logging all tool calls captured in agent_events...")
        tool_logs = inspect_and_log_tool_calls(agent_events)

        logger.info("Asserting ground truth preservation against model response and tool calls...")
        assert_ground_truth_preservation(agent_events, findings)

        logger.info("Asserting dashboard metric tokens verbatim in tool calls...")
        assert_dashboard_metrics_verbatim(agent_events)

        logger.info("Ground truth and metric tokens verified successfully.")
        return {"status": "ok", "events_count": len(agent_events), "tool_logs": tool_logs}

    finally:
        logger.info("Closing McpToolset connection...")
        await mcp_toolset.close()


def run_delivery_qc(master_path: str, spec_path: str) -> Dict[str, Any]:
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
    adk_result = asyncio.run(run_adk_orchestration(report, env_cfg))
    report["adk_result"] = adk_result

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="First Pass Quality Control ADK Orchestrator")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable verbose DEBUG logging")
    parser.add_argument("--plain", action="store_true", help="Use plain stdlib logging output instead of Rich formatting")
    args = parser.parse_args()

    setup_logging(verbose=args.verbose, plain=args.plain)

    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    default_master = os.path.join(base_dir, "data", "masters", "master_blockers.json")
    default_spec = os.path.join(base_dir, "data", "specs", "streamone.json")

    report = run_delivery_qc(default_master, default_spec)
    print("\n" + "=" * 60)
    print(f"QC Evaluation Complete: {report['master_id']}")
    print(f"Verdict: {report['verdict']}")
    print(f"Blocker Count: {report['blocker_count']}")
    print("=" * 60)


