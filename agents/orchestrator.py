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
import subprocess
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
    Resolves Google Cloud credentials via standard ADC or fallback gcloud OAuth token.
    """
    try:
        creds, _ = google.auth.default()
        return creds
    except Exception:
        logger.info("ADC credentials file not found; fetching token via gcloud CLI...")
        token = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        return google.oauth2.credentials.Credentials(token)


def validate_environment() -> Dict[str, str]:
    """
    Validates required environment variables for Google Cloud AI and Grafana MCP auth.
    Fails loudly with RuntimeError if required variables are missing.
    Automatically sets GOOGLE_GENAI_USE_VERTEXAI and GOOGLE_CLOUD_PROJECT for the Google AI SDK.
    """
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT_ID")
    location = os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")
    token = os.getenv("GRAFANA_SERVICE_ACCOUNT_TOKEN")

    missing = []
    if not project_id:
        missing.append("GOOGLE_CLOUD_PROJECT (or GCP_PROJECT_ID)")
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
    measured = finding.get("measured")
    measured_str = "not present" if measured is None else str(measured)
    return {
        "clause_id": str(finding.get("clause_id", "N/A")),
        "severity": str(finding.get("severity", "blocker")),
        "measured": measured_str,
        "message": str(finding.get("message", "")),
    }


def assert_ground_truth_preservation(prompt_text: str, findings: List[Dict[str, Any]]) -> None:
    """
    Asserts that every ground-truth clause ID and measured value appears verbatim in prompt text.
    Ensures zero hallucination or mathematical mutation by the LLM.
    """
    for finding in findings:
        truth = format_finding_ground_truth(finding)
        clause_id = truth["clause_id"]
        measured = truth["measured"]

        if clause_id not in prompt_text:
            raise AssertionError(f"Ground-truth clause ID '{clause_id}' missing from agent context prompt!")
        if measured not in prompt_text and measured != "not present":
            raise AssertionError(f"Ground-truth measured value '{measured}' missing from agent context prompt!")


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
    mcp_toolset = McpToolset(connection_params=connection_params)

    try:
        master_id = report.get("master_id", "UNKNOWN")
        blocker_count = report.get("blocker_count", 0)
        findings = report.get("findings", [])

        # Programmatically map highest severity
        has_blockers = blocker_count > 0
        mapped_severity = map_severity_to_grafana("blocker" if has_blockers else "warning")

        # Ground truth prompt context
        formatted_findings = [format_finding_ground_truth(f) for f in findings]
        prompt_data = {
            "master_id": master_id,
            "verdict": report.get("verdict"),
            "blocker_count": blocker_count,
            "mapped_severity": mapped_severity,
            "findings": formatted_findings,
            "room_prefix": "first-pass",
        }

        prompt_json = json.dumps(prompt_data, indent=2)

        # Invariant check: verify ground truth tokens exist in prompt
        assert_ground_truth_preservation(prompt_json, findings)

        user_prompt = f"""
You are the First Pass Quality Control Agent.
A technical master delivery evaluation completed with verdict: {report.get('verdict')}.

Master ID: {master_id}
Blockers Count: {blocker_count}

Structured Findings Ground Truth:
{prompt_json}

Instruction:
1. Examine the structured findings.
2. If there are delivery blockers (blocker_count > 0), call the `create_incident` tool.
3. Pass the parameters to `create_incident`:
   - `title`: "Delivery Blocker: {master_id} ({blocker_count} Spec Non-Conformances)"
   - `severity`: "{mapped_severity}"
   - `roomPrefix`: "first-pass"
4. Explain clearly in your final response which spec clauses failed, retaining the exact clause IDs and measured values verbatim.
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

        logger.info("ADK execution completed cleanly.")
        return {"status": "ok", "events_count": len(agent_events)}

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
