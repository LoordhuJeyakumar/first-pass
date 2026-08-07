"""
First Pass — Orchestrator Agent

Main entry point for delivery master quality control evaluation.
Evaluates technical master metadata against platform specification using deterministic check engine,
and interacts with Grafana Cloud via self-hosted MCP server to create incidents and post fix plans.
"""

import os
import sys
import json
import logging
import requests
from typing import Dict, Any

# Ensure project root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agents.check_engine import evaluate_master_against_spec

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("FirstPassOrchestrator")


def load_json_file(file_path: str) -> Dict[str, Any]:
    with open(file_path, "r", encoding="utf-8") as f:
        return json.load(f)


def send_mcp_request(mcp_url: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Sends a tool invocation request to self-hosted grafana/mcp-grafana over Streamable HTTP.
    Performs initialization handshake to acquire Mcp-Session-Id before calling tools.
    """
    headers = {"Content-Type": "application/json"}
    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "first-pass-orchestrator", "version": "1.0"},
        },
    }

    try:
        init_resp = requests.post(mcp_url, json=init_payload, headers=headers, timeout=10)
        session_id = init_resp.headers.get("Mcp-Session-Id")

        call_headers = {"Content-Type": "application/json"}
        if session_id:
            call_headers["Mcp-Session-Id"] = session_id

        call_payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }

        response = requests.post(mcp_url, json=call_payload, headers=call_headers, timeout=10)
        response.raise_for_status()
        try:
            return response.json()
        except Exception:
            return {"status": "ok", "http_code": response.status_code, "text": response.text}
    except Exception as e:
        import traceback
        logger.warning(f"MCP server interaction error ({mcp_url}): {e}\n{traceback.format_exc()}")
        return {"error": str(e)}


def generate_remediation_plan(report: Dict[str, Any]) -> str:
    """
    Generates a ranked, human-readable remediation fix plan for detected delivery blockers/warnings.
    """
    verdict = report.get("verdict", "UNKNOWN")
    master_id = report.get("master_id", "UNKNOWN")
    spec_id = report.get("spec_id", "UNKNOWN")
    findings = report.get("findings", [])

    lines = [
        f"# First Pass Remediation Plan: {master_id}",
        f"**Target Specification**: `{spec_id}`",
        f"**Verdict**: `{verdict}` ({report.get('blocker_count', 0)} blockers, {report.get('warning_count', 0)} warnings)",
        "",
    ]

    if verdict == "PASS":
        lines.append("✅ **All technical clauses passed**. Package is ready for delivery submission.")
    else:
        lines.append("### Priority Remediation Actions:")
        for idx, finding in enumerate(findings, start=1):
            severity = finding.get("severity", "blocker").upper()
            clause_id = finding.get("clause_id", "N/A")
            msg = finding.get("message", "")
            lines.append(f"{idx}. **[{severity}] Clause {clause_id}**: {msg}")

        lines.extend([
            "",
            "### Recommended Next Steps for Post-Production Team:",
            "- Re-render audio tracks to target loudness (-27 LUFS ±2 LU) using standard LUFS limiter.",
            "- Ensure matching Timed Text (IMSC1 subtitle) files are included for all delivered audio dubs.",
            "- Verify package naming metadata aligns with StreamOne packaging conventions.",
        ])

    return "\n".join(lines)


def run_delivery_qc(master_path: str, spec_path: str, mcp_url: str = None) -> Dict[str, Any]:
    """
    Executes a complete delivery QC evaluation run.
    """
    logger.info(f"Ingesting master metadata: {master_path}")
    master = load_json_file(master_path)

    logger.info(f"Ingesting delivery specification: {spec_path}")
    spec = load_json_file(spec_path)

    logger.info("Executing deterministic QC check engine...")
    report = evaluate_master_against_spec(master, spec)

    remediation_plan = generate_remediation_plan(report)
    report["remediation_plan"] = remediation_plan

    logger.info(f"QC Run Finished. Verdict: {report['verdict']} (Blockers: {report['blocker_count']})")

    # Post to Grafana MCP if URL provided or found in environment
    mcp_endpoint = mcp_url or os.getenv("MCP_SERVER_URL", "http://localhost:8000/mcp")
    if report["verdict"] == "REJECT" and mcp_endpoint:
        logger.info(f"Attempting MCP incident creation on {mcp_endpoint}...")
        incident_params = {
            "title": f"Delivery Blocker: {report['master_id']} ({report['blocker_count']} Spec Non-Conformances)",
            "severity": "critical",
        }
        mcp_res = send_mcp_request(mcp_endpoint, "create_incident", incident_params)
        report["mcp_response"] = mcp_res

    return report


if __name__ == "__main__":
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    default_master = os.path.join(base_dir, "data", "masters", "master_blockers.json")
    default_spec = os.path.join(base_dir, "data", "specs", "streamone.json")

    report = run_delivery_qc(default_master, default_spec)
    print("\n" + "=" * 60)
    print(report["remediation_plan"])
    print("=" * 60)
