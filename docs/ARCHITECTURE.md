# Architecture

This document describes the architectural design of First Pass, detailing how the single bounded agent system operates, integrates with Grafana Cloud via the Model Context Protocol (MCP), relies on a deterministic check engine, and defines system scope.

## The Operational Mapping

Film delivery involves submitting a complete package (video, multi-language audio, subtitles, and metadata) to a streaming platform against strict specification clauses. Traditionally, this is verified manually by comparing technical specs against a static PDF document.

First Pass maps delivery quality control directly into modern software observability concepts:

| Film Delivery Concept | Observability Concept |
|---|---|
| Audio loudness deviation (e.g. LUFS) | Metric measurement with threshold limits |
| Delivery specification clause | Spec clause check condition |
| Technical spec violation / blocker | Operational incident with timeline annotation |
| Multi-language delivery status | Delivery readiness dashboard |
| Overall pass / bounce decision | System health status banner |

Building on Grafana Cloud provides unified metrics, structured logs, incidents, and timeline annotations within a single operational workflow.

## System Overview

```
[Master Metadata JSON] + [StreamOne Platform Spec JSON]
                        │
                        ▼
┌────────────────────────────────────────────────────────┐
│ Deterministic Python Check Engine                      │
│  (Evaluates LUFS, HDR, Timed Text, CBFC)               │
└──────────────┬─────────────────────────┬───────────────┘
               │ Structured Findings     │ Telemetry (Prometheus & Loki)
               ▼                         │
┌────────────────────────────────────────┴───────────────┐
│ Single Google ADK LlmAgent (Gemini 2.5 Flash)          │
│  (Holds McpToolset scoped by explicit tool_filter)     │
└──────────────┬─────────────────────────────────────────┘
               │                                │
               │ Grafana Ruler REST Read        │ MCP (Streamable HTTP)
               │ (Pre-queries GET /api/ruler)   │ (WRITES: create/update)
               ▼                                ▼
┌────────────────────────────────────────────────────────┐
│ grafana/mcp-grafana / Grafana REST API                 │
│  (Authenticated via Service Account Token)             │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Grafana Cloud Stack                                    │
│  (Dashboards · Incidents · Alert Rules · Annotations)  │
└──────────────────────────┬─────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────┐
│ Operator Console (local / GCE VM)                      │
│  (Verdict Banner · Fix List · Action Ledger · Trigger) │
└──────────────────────────┴─────────────────────────────┘
```

## Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| **LLM Orchestration** | Gemini 2.5 Flash via Vertex AI | Executes automated operational actions through native ADK tool calling. |
| **Agent Architecture** | Single Google ADK `LlmAgent` | Bounded execution with explicit `tool_filter` permissions on top of deterministic checks. |
| **Observability Platform** | Grafana Cloud | Unified platform for metrics, logs, dashboards, incidents, and annotations. |
| **MCP Server** | Self-hosted `grafana/mcp-grafana` in Docker | Designed for GCE VM deployment (currently executed locally) with `-t streamable-http` and Grafana service-account token authentication. |
| **Agent ↔ MCP Transport** | `McpToolset` + `StreamableHTTPConnectionParams` | Standard static-bearer-token authentication path for unattended systems. |
| **Metrics Pipeline** | Prometheus Remote-Write | Ingests numerical QC telemetry (`qc_checks`, `qc_loudness_deviation_lufs`, `qc_blockers_current`) with fixed, low-cardinality label sets. |
| **Logs Pipeline** | Loki Push API | Ingests structured JSON log lines per check finding containing high-cardinality metadata (`run_id`, clause details). |
| **Check Engine** | Pure, deterministic Python | Ensures all numerical calculations and spec evaluations are reproducible prior to LLM invocation. |
| **Operator Console** | FastAPI + Jinja2 + vanilla JS | Single-page app serving verdict, fix list, readiness grid, and action ledger. Runs in the same Python process as the agent via a `ThreadPoolExecutor` for event-loop isolation. |
| **Hosting** | GCE VM & Secret Manager | Containerized deployment for agent service, MCP server, and web console on a GCE VM with secure secret management. |

## The Unattended Authentication Architecture

Grafana Cloud provides a hosted MCP endpoint. However, the hosted endpoint relies exclusively on interactive OAuth 2.1 authentication, which requires user interaction in a browser and issues short-lived tokens.

For automated delivery workflows operating unattended in a CI/CD or delivery pipeline, interactive OAuth is unsuitable. Therefore, First Pass uses a **self-hosted `grafana/mcp-grafana` instance**:

- Designed for Docker deployment on a GCE Virtual Machine (currently executed locally).
- Authenticated using a persistent Grafana Service Account Token (Editor role).
- Exposed over `streamable-http` transport.
- Configured with `--enabled-tools` in Docker Compose to restrict MCP server capability to operational domains (`incident`, `dashboard`, `alerting`, `annotations`, `search`, `query`, `folder`, `datasource`), providing defense-in-depth alongside the agent's stricter four-tool `tool_filter` allowlist.
- Pinned to a verified, published Docker image release tag.

This self-hosted pattern is the officially recommended approach for unattended automated agents interacting with Grafana Cloud.

## Current System Architecture: Single Bounded Agent

The system runs as a two-stage operational pipeline:

1. **Deterministic Check Engine (`agents/check_engine.py`)**: Runs before any LLM interaction. It evaluates master metadata against target specification clauses, checks audio loudness, audio true peak, HDR color primaries, timed text language coverage, and CBFC regulatory gating. It outputs structured finding objects, pushes the **Delivery Readiness** dashboard directly to Grafana Cloud (`POST /api/dashboards/db`), and streams Prometheus metrics (`qc_checks`, `qc_loudness_deviation_lufs`, `qc_blockers_current`, `qc_readiness_ratio`) and Loki log entries.
2. **Single Bounded Agent (`agents/orchestrator.py`)**: Instantiates a single Google ADK `Agent` (`FirstPassOrchestrator`) using Gemini 2.5 Flash on Vertex AI. The agent holds an `McpToolset` strictly bounded by an explicit `tool_filter` containing four allowlisted MCP tools:
   - `create_incident`: Opens a Grafana Cloud incident when delivery blockers are present.
   - `add_activity_to_incident`: Posts clause non-conformance details to the incident timeline.
   - `create_annotation`: Attaches timeline annotations to the Delivery Readiness dashboard.
   - `alerting_manage_rules`: Creates and manages Grafana alert rules for delivery blocker conditions.

### Architectural Decision (2026-08-14): Deterministic Dashboard Publishing
Asking the LLM to carry a ~10KB dashboard JSON payload through tool calls proved unreliable, frequently emitting empty payloads or triggering HTTP 400 Bad Request errors. Following the principle that deterministic work belongs in code rather than prompt transport, Python pre-publishes the Delivery Readiness dashboard directly (`POST /api/dashboards/db`) prior to LLM orchestration. Moving JSON data plumbing to Python took orchestration reliability from 0/4 to 5/5 consecutive verified executions, keeping the agent strictly focused on operational judgment (incident narratives, annotation text, alert rule configuration).

### Grafana Ruler REST API Read Path
To guarantee alert rule idempotency without handing complex list-then-branch conditionals to the LLM, Python pre-queries Grafana Cloud directly via the Ruler REST API (`GET /api/ruler/grafana/api/v1/rules/first-pass-qc`) using `GRAFANA_URL` and `GRAFANA_SERVICE_ACCOUNT_TOKEN` before the LLM runs.

- **Purpose**: Determines whether an alert rule titled `"First Pass - Delivery Blockers Present"` already exists.
- **Deterministic Read-Only**: This pre-query is strictly a READ operation.
- **Non-Branching Agent Instruction**: If the rule exists, Python instructs the LLM with a flat `operation: "update"` containing its `uid`. If absent, Python instructs `operation: "create"`. If the pre-query fails, the alerting instruction is safely skipped for that run.
- **Write Path Security**: All state mutations and writes (`create`/`update`) remain strictly routed through the allowlisted MCP tool (`alerting_manage_rules`).

**Architectural Rationale**: The deterministic engine computes, Python resolves pre-query idempotency, the single bounded agent acts through a small allowlisted MCP tool surface, and every write is audited. A small, verified system is more defensible, auditable, and reliable than an unconstrained crew.

## Planned Architecture: System Scope & Agent Design

Agent count is deliberately one. It would change only if First Pass ingested third-party delivery specifications it did not author — unknown document structure is the one input class requiring model reasoning rather than deterministic parsing. No such requirement exists in the current scope.

## Deterministic Computation Principle

The LLM is responsible for orchestration, spec interpretation, prose generation, and tool coordination. It **never computes numerical measurements**.

All measurement comparisons (e.g., verifying integrated loudness of −24.0 LUFS against a −27.0 ±2 LUFS clause) are executed by pure Python logic. This ensures:
1. Every verdict and blocker report is 100% reproducible and verifiable.
2. The core checking logic can be unit-tested via standard `pytest` without invoking LLM API calls.
3. Execution cost and latency remain low.

## Telemetry & Cardinality Design

- **Metrics (Prometheus)**: Fixed label sets with low cardinality:
  - `qc_checks{domain, result}`
  - `qc_loudness_deviation_lufs{language}`
  - `qc_blockers_current`
  - `qc_readiness_ratio{language}`
- **Logs (Loki)**: High-cardinality metadata is stored in structured JSON log entries:
  - `{"run_id":"...","clause_id":"A-2.1","severity":"blocker","measured":"-24.0","expected":"-27 ±2","language":"ta-IN","message":"..."}`

By isolating run identifiers to Loki logs rather than Prometheus metric labels, the metric time series count remains strictly controlled within free-tier limits.

## MCP Tool Surface

### Active Allowlisted Tools (`tool_filter`)
The active agent operates exclusively through a bounded `McpToolset` restricted to four allowlisted MCP tools:
- `create_incident`: Opens an incident in Grafana Cloud when delivery blockers are detected.
- `add_activity_to_incident`: Appends structured clause violation findings to the incident timeline.
- `create_annotation`: Posts timeline annotations on the Delivery Readiness dashboard.
- `alerting_manage_rules`: Creates and manages Grafana alert rules for delivery blocker conditions.

### Planned Tools (Roadmap)
Future multi-agent expansions will evaluate expanding `tool_filter` permissions to include:
- Read tools (`search_dashboards`, `get_dashboard_summary`, `query_prometheus`, `query_loki_logs`, `list_incidents`, `generate_deeplink`) for interactive agent inspection.

## Operator Console

The operator console (`frontend/`) is a FastAPI + Jinja2 + vanilla JavaScript single-page application that renders the output of the deterministic check engine without recomputing it.

### Three panels

1. **Verdict Banner** — colour-coded PASS / REJECT, scaled for readability from across a room at 1080p.
2. **Fix List** — every finding with clause ID (monospace, left-aligned), severity badge (glyph + label, not colour alone), measured vs expected in tabular-figure monospace, language, and human-readable message.
3. **Action Ledger** — incremental live log of every MCP write the agent performs. Rows append with a CSS keyframe animation. Each row links into Grafana (incident, annotation, alert rule) using the identifier returned by the MCP tool response. The Grafana hostname is read from `GRAFANA_URL` at request time and never rendered as visible text — link text reads `"Incident #142"`, not the URL.

### Trigger button

A RUN button lets operators (and judges) re-run the pipeline against any of the three masters without CLI access. Each run produces fresh telemetry in Grafana Cloud, ensuring the dashboard is populated during judging regardless of the 14-day retention window.

### Guardrails

| Guardrail | Mechanism |
|---|---|
| Single flight | `threading.Lock` — `POST /api/run` returns 409 while the lock is held |
| Cooldown | `CONSOLE_COOLDOWN_SECONDS` (default 30 s) — `POST /api/run` returns 429 with `Retry-After` |
| Event-loop isolation | Pipeline runs in a `ThreadPoolExecutor`; `asyncio.run()` inside `run_delivery_qc` gets a clean thread-local event loop, isolated from FastAPI's own loop |
| No hostname in DOM | Grafana deeplinks are built server-side; link text is always a human label, never the raw URL |

### API

```
GET  /                  Renders the console page
GET  /api/masters       Lists master JSON files from data/masters/
POST /api/run           {"master": "..."}  → {"run_id": "uuid"} | 409 | 429
GET  /api/run/{run_id}  {"status": "running|done|failed", "verdict": ..., ...}
```

The page polls `GET /api/run/{run_id}` every 1.5 seconds while a run is active, appending only newly-arrived ledger entries on each tick.

## Security & Repository Hygiene

- Secrets and tokens are loaded strictly from environment variables locally and Google Secret Manager in production.
- Pre-commit disclosure auditing (`scripts/check-disclosure.sh`) prevents accidental commits of credentials, private notes, or internal documentation.
