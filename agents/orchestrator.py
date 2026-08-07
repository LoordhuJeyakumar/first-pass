"""
First Pass — Orchestrator Agent (Google ADK & Gemini on Vertex AI)

Main entry point for delivery master quality control evaluation.
Evaluates technical master metadata against platform specification using deterministic check engine,
and interacts with Grafana Cloud via self-hosted MCP server using Google ADK to create incidents.
"""

import os
import sys
import json
import logging
import asyncio
from typing import Dict, Any, List

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import google.auth
import google.oauth2.credentials
from google.adk import Agent, Runner
from google.adk.models.google_llm import Gemini
from google.adk.sessions import InMemorySessionService
from google.adk.tools import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams
from google.genai import types

from agents.check_engine import evaluate_master_against_spec

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
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
    Fails loudly with RuntimeError if Application Default Credentials cannot be resolved.
    """
    try:
        creds, _ = google.auth.default()
        return creds
    except Exception as exc:
        raise RuntimeError(
            "Failed to resolve Google Cloud Application Default Credentials (ADC).\n"
            "Please run 'gcloud auth application-default login' or set GOOGLE_APPLICATION_CREDENTIALS."
        ) from exc


def validate_environment() -> Dict[str, str]:
    """
    Validates required environment variables for Google Cloud AI and Grafana MCP auth.
    Fails loudly with RuntimeError if required variables are missing.
    Automatically sets GOOGLE_GENAI_USE_VERTEXAI and GOOGLE_CLOUD_PROJECT for the Google AI SDK.
    """
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

    # Ensure Google GenAI SDK receives standard Vertex AI configuration
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "true"
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    os.environ["GOOGLE_CLOUD_LOCATION"] = location

    return {
        "project_id": project_id,
        "location": location,
        "token": token,
        "mcp_url": os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp"),
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


def inspect_and_log_tool_calls(agent_events: List[Any]) -> List[Dict[str, Any]]:
    """
    Explicitly logs all tool calls captured in ADK agent_events:
    tool name, invocation arguments, and returned responses/results.
    Checks whether create_annotation was invoked and returned successfully.
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
            logger.info(f"[TOOL CALL] Invoked tool '{name}' with arguments: {json.dumps(args)}")
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
            logger.info(f"[TOOL RESPONSE] Tool '{name}' returned: {json.dumps(response_val)}")
            tool_logs.append({"type": "response", "name": name, "response": response_val})

    # Audit create_annotation calls explicitly
    annotation_calls = [t for t in tool_logs if t["type"] == "call" and t["name"] == "create_annotation"]
    annotation_responses = [t for t in tool_logs if t["type"] == "response" and t["name"] == "create_annotation"]

    if not annotation_calls:
        logger.error("AUDIT WARNING: 'create_annotation' tool was NEVER invoked by the agent during execution!")
    else:
        logger.info(f"AUDIT OK: 'create_annotation' tool invoked {len(annotation_calls)} time(s).")
        for resp in annotation_responses:
            resp_data = resp.get("response", {})
            if "error" in str(resp_data).lower() or "err" in str(resp_data).lower():
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


async def run_adk_orchestration(
    report: Dict[str, Any], env_cfg: Dict[str, str]
) -> Dict[str, Any]:
    """
    Asynchronously executes the Google ADK Agent workflow to create an incident on Grafana Cloud MCP.
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
        tool_filter=["create_incident", "add_activity_to_incident", "create_annotation"],
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

        user_prompt = f"""
You are the First Pass Quality Control Agent.
A technical master delivery evaluation completed with verdict: {report.get('verdict')}.

Master ID: {master_id}
Blockers Count: {blocker_count}

Structured Findings Ground Truth:
{prompt_json}

Instructions:
1. Examine the structured findings. If there are delivery blockers (blocker_count > 0):
2. Call `create_incident`:
   - `title`: "Delivery Blocker: {master_id} ({blocker_count} Spec Non-Conformances)"
   - `severity`: "{mapped_severity}"
   - `roomPrefix`: "first-pass"
3. Call `add_activity_to_incident` using the incident ID returned by `create_incident` (e.g. incidentID):
   - `incidentId`: the ID returned from create_incident
   - `body`: Detail each blocker finding. For every blocker, include its clause_id, spec clause_text, measured value, and expected value verbatim. Format requirement: put each blocker on its own separate line (or bullet point) with line breaks between blockers. Do NOT format multiple blockers into a single dense paragraph.
4. Call `create_annotation` to create a timeline annotation:
   - `text`: Timeline annotation describing the delivery blocker for {master_id}, explicitly referencing the violated clause IDs ({', '.join(clause_ids)}) and brief summary.
5. In your final response, explain the spec clause failures retaining the exact clause IDs, measured values, and expected values verbatim. Do not compute or alter any numeric or measured values.
"""

        logger.info("Initializing Google ADK Gemini model & LlmAgent (model: gemini-2.5-flash)...")
        creds = get_google_auth_credentials()
        gemini_model = Gemini(model="gemini-2.5-flash", client_kwargs={"credentials": creds})

        agent = Agent(
            name="FirstPassOrchestrator",
            model=gemini_model,
            instruction="You are an automated delivery Quality Control orchestrator for streaming film masters.",
            tools=[mcp_toolset],
        )

        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name="first-pass", session_service=session_service)

        session = await session_service.create_session(app_name="first-pass", user_id="operator")
        session_id = session.id

        new_message = types.Content(
            parts=[types.Part.from_text(text=user_prompt)], role="user"
        )

        logger.info("Executing Google ADK runner with Gemini 2.5 Flash on Vertex AI...")
        agent_events = []
        async for event in runner.run_async(
            user_id="operator", session_id=session_id, new_message=new_message
        ):
            agent_events.append(event)
            logger.info(f"ADK Event received: {event}")

        logger.info("ADK execution completed. Inspecting and logging all tool calls captured in agent_events...")
        tool_logs = inspect_and_log_tool_calls(agent_events)

        logger.info("Asserting ground truth preservation against model response and tool calls...")
        assert_ground_truth_preservation(agent_events, findings)

        logger.info("Ground truth preservation verified successfully.")
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

    logger.info(f"QC Run Finished. Verdict: {report['verdict']} (Blockers: {report['blocker_count']})")

    if report["verdict"] == "REJECT":
        logger.info("Delivery blockers detected. Triggering Google ADK Orchestrator workflow...")
        adk_result = asyncio.run(run_adk_orchestration(report, env_cfg))
        report["adk_result"] = adk_result

    return report


if __name__ == "__main__":
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    default_master = os.path.join(base_dir, "data", "masters", "master_blockers.json")
    default_spec = os.path.join(base_dir, "data", "specs", "streamone.json")

    report = run_delivery_qc(default_master, default_spec)
    print("\n" + "=" * 60)
    print(f"QC Evaluation Complete: {report['master_id']}")
    print(f"Verdict: {report['verdict']}")
    print(f"Blocker Count: {report['blocker_count']}")
    print("=" * 60)
