# Architecture

This document describes the architectural design of First Pass, detailing how the multi-agent system operates, integrates with Grafana Cloud via the Model Context Protocol (MCP), and handles delivery readiness checks.

## The Operational Mapping

Film delivery involves submitting a complete package (video, multi-language audio, subtitles, and metadata) to a streaming platform against strict specification clauses. Traditionally, this is verified manually by comparing technical specs against a static PDF document.

First Pass maps delivery quality control directly into modern software observability concepts:

| Film Delivery Concept | Observability Concept |
|---|---|
| Audio loudness deviation (e.g. LUFS) | Metric measurement with threshold limits |
| Delivery specification clause | Alert rule condition |
| Technical spec violation / blocker | Operational incident with timeline annotation |
| Multi-language delivery status | Delivery readiness dashboard |
| Overall pass / bounce decision | System health status banner |

Building on Grafana Cloud provides unified metrics, structured logs, alert rules, incidents, and timeline annotations within a single operational workflow.

## System Overview

```
[Master Metadata JSON] + [StreamOne Platform Spec JSON]
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Agent Crew (Google ADK Python + Gemini on Vertex AI)   │
│  ├─ Orchestrator                                       │
│  ├─ Spec-Interpreter      → Cached Constraint Set      │
│  ├─ QC-Analyst            → Deterministic Python Checks│
│  ├─ Observability-Actuator→ MCP Write Operations       │
│  └─ Remediation           → Ranked Fix Plan            │
└──────────────┬─────────────────────────┬───────────────┘
               │ Telemetry               │ MCP (Streamable HTTP)
   (Remote-Write & Loki Push)            ▼
               │             ┌───────────────────────────┐
               │             │ grafana/mcp-grafana       │  ← Docker on GCE VM
               │             │ (Service Account Token)   │
               │             └───────────┬───────────────┘
               ▼                         ▼
┌────────────────────────────────────────────────────────┐
│ Grafana Cloud Stack                                    │
│  (Dashboards · Alert Rules · Incidents · Annotations)  │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Operator Console (Cloud Run)                           │
│  (Verdict Banner · Clause Breakdown · Action Ledger)   │
└────────────────────────────────────────────────────────┘
```

## Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| **LLM Orchestration** | Gemini via Vertex AI | Provides strong multi-agent reasoning and instruction-following capability. |
| **Agent Framework** | Google ADK (Python) | First-class multi-agent network orchestration with native `MCPToolset` support. |
| **Observability Platform** | Grafana Cloud | Unified platform for metrics, logs, dashboards, alert management, and incidents. |
| **MCP Server** | Self-hosted `grafana/mcp-grafana` in Docker | Runs on GCE VM with `-t streamable-http` and Grafana service-account token authentication. |
| **Agent ↔ MCP Transport** | `MCPToolset` + `StreamableHTTPConnectionParams` | Standard static-bearer-token authentication path for unattended systems. |
| **Metrics Pipeline** | Prometheus Remote-Write | Ingests numerical QC telemetry with fixed, low-cardinality label sets. |
| **Logs Pipeline** | Loki Push API | Ingests structured JSON log lines per check finding containing high-cardinality metadata. |
| **Check Engine** | Pure, deterministic Python | Ensures all numerical calculations and spec evaluations are reproducible. |
| **Hosting** | Cloud Run & Secret Manager | Containerized deployment for agent service and web console with secure secret management. |

## The Unattended Authentication Architecture

Grafana Cloud provides a hosted MCP endpoint. However, the hosted endpoint relies exclusively on interactive OAuth 2.1 authentication, which requires user interaction in a browser and issues short-lived tokens.

For automated delivery workflows operating unattended in a CI/CD or delivery pipeline, interactive OAuth is unsuitable. Therefore, First Pass uses a **self-hosted `grafana/mcp-grafana` instance**:

- Deployed via Docker on a GCE Virtual Machine.
- Authenticated using a persistent Grafana Service Account Token (Editor role).
- Exposed over `streamable-http` transport.
- Configured with `--enabled-tools` to restrict write capabilities to required domains (dashboard, incident, alerting, annotation).
- Pinned to a verified, published Docker image release tag.

This self-hosted pattern is the officially recommended approach for unattended automated agents interacting with Grafana Cloud.

## Multi-Agent Crew Structure

| Agent | Input | Output | Tool Usage |
|---|---|---|---|
| **Orchestrator** | Master JSON + Spec JSON | Execution plan & overall verdict | Sub-agent routing and state delegation. |
| **Spec-Interpreter** | Spec Document | Machine-readable constraint set | Gemini reasoning (results cached to JSON). |
| **QC-Analyst** | Master Metadata + Constraints | Finding objects + Telemetry | Deterministic Python check engine, Prometheus remote-write, Loki push. |
| **Observability-Actuator** | Finding Objects | Dashboards, Alert Rules, Incidents | MCP write tools (`update_dashboard`, `alerting_manage_rules`, `create_incident`, `create_annotation`). |
| **Remediation** | Finding Objects | Ranked human-readable fix plan | MCP activity tool (`add_activity_to_incident`). |
| **Release-Coordinator** | Language Matrix + CBFC Status | Per-language readiness data | Telemetry emission & readiness dashboard updates. |

## Deterministic Computation Principle

The LLM is responsible for orchestration, spec interpretation, prose generation, and tool coordination. It **never computes numerical measurements**.

All measurement comparisons (e.g., verifying integrated loudness of −24.0 LUFS against a −27.0 ±2 LUFS clause) are executed by pure Python logic. This ensures:
1. Every verdict and blocker report is 100% reproducible and verifiable.
2. The core checking logic can be unit-tested via standard `pytest` without invoking LLM API calls.
3. Execution cost and latency remain low.

## Telemetry & Cardinality Design

- **Metrics (Prometheus)**: Fixed label sets with low cardinality:
  - `qc_check_total{domain, result}`
  - `qc_loudness_deviation_lufs{language}`
  - `qc_blockers_current`
  - `qc_readiness_ratio{language}`
- **Logs (Loki)**: High-cardinality metadata is stored in structured JSON log entries:
  - `{"run_id":"...","clause_id":"A-2.1","severity":"blocker","measured":"-24.0","expected":"-27 ±2","language":"ta-IN","message":"..."}`

By isolating run identifiers to Loki logs rather than Prometheus metric labels, the metric time series count remains strictly controlled within free-tier limits.

## MCP Tool Surface

- **Write Tools**: `update_dashboard`, `create_folder`, `create_incident`, `add_activity_to_incident`, `alerting_manage_rules`, `create_annotation`, `update_annotation`.
- **Read Tools**: `search_dashboards`, `get_dashboard_summary`, `query_prometheus`, `query_loki_logs`, `list_incidents`, `generate_deeplink`.

*Note*: For reading dashboards, `get_dashboard_summary` is preferred over `get_dashboard_by_uid` to reduce payload context size.

## Security & Repository Hygiene

- Secrets and tokens are loaded strictly from environment variables locally and Google Secret Manager in production.
- Pre-commit disclosure auditing (`scripts/check-disclosure.sh`) prevents accidental commits of credentials, private notes, or internal documentation.
