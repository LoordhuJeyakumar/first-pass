# Project Instructions — First Pass

**Agents that catch a film delivery rejection before the platform does.**

`AGENTS.md` is read directly by Gemini CLI and Antigravity, and is the single source of truth for project instructions and hard constraints. No bridge file is needed.

## Non-Negotiable Hard Constraints

1. **Runtime AI is Google-only.** Accepted packages in runtime code: `google-adk`, `google-genai`, `google-generativeai`, `google-cloud-aiplatform`. Never import or reference third-party AI SDKs (OpenAI, Anthropic, LangChain, CrewAI, etc.).
2. **Public / Internal Disclosure Boundary.** Files under `docs/internal/` are gitignored and stay local. Never commit or quote files under `docs/internal/` into tracked files in the public repository.
3. **Secret Hygiene.** Real credentials, API keys, Grafana service-account tokens, GCP project IDs, VM hostnames, and stack URLs live in `.env` (gitignored) and Secret Manager. Never hardcode or commit real credentials. Placeholders in `.env.example` must remain uppercase (e.g. `glsa_REPLACE_ME`) to pass pre-commit disclosure checks.
4. **Grafana MCP Architecture.** The MCP server MUST be self-hosted (`grafana/mcp-grafana` Docker image on a VM) using streamable-http and a Grafana service-account token. (The hosted Grafana Cloud MCP endpoint uses interactive OAuth only and lacks a machine-token path for unattended agents).
5. **Deterministic Check Engine.** The LLM orchestrates, interprets specs, and explains findings, but **never computes measurements**. Audio loudness, True Peak, HDR metadata, subtitle coverage, and packaging checks must be computed by pure, deterministic Python code.
6. **Telemetry & Series Cap.** Prometheus metrics use fixed, low-cardinality label sets (`qc_check_total{domain,result}`, `qc_loudness_deviation_lufs{language}`, `qc_blockers_current`, `qc_readiness_ratio{language}`). Unique run IDs belong in Loki log payloads, never in Prometheus metric labels.
7. **AI Orchestration Package Denylist.** The following packages are explicitly prohibited in runtime code — they duplicate what `google-adk` already provides and would introduce non-Google AI dependencies: `langchain`, `langgraph`, `crewai`, `autogen`, `llama-index`, `openai`, `anthropic`, `semantic-kernel`, `haystack`. Non-AI third-party packages (`fastapi`, `requests`, `jinja2`, etc.) are unrestricted.
8. **Commit Convention.** Every commit must follow Conventional Commits 1.0.0 (see below). The commit body must record what was **Verified**, not only what changed — this is the audit trail for Definition of Done point 3.

## Repository Structure

```
agents/      ADK agents (orchestrator, interpreter, analysts, actuator, remediation)
mcp/         Docker compose and deployment config for mcp-grafana
data/        Synthetic master metadata (masters/*.json) and spec definitions (specs/*.json)
frontend/    Operator web console (FastAPI / HTML / JS)
scripts/     Verification scripts and disclosure checks
tests/       pytest test suite for deterministic check engine and telemetry emitters
docs/        Public documentation (README.md, ARCHITECTURE.md, DOMAIN.md, DISCLOSURE.md)
```

## Definition of Done (per Increment)

- Runs end-to-end from a single command.
- Deterministic logic passes `pytest`.
- Output verified directly in Grafana Cloud or UI console.
- Disclosure check passes: `./scripts/check-disclosure.sh --all`.
- Zero secrets committed; 100% Google AI SDK compliant.

## Commit Convention

Format: `<type>(<scope>): <description>`

**Types:** `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `build`, `ci`, `perf`, `revert`

**Scopes** come from the top-level directories: `agents`, `mcp`, `data`, `docs`, `scripts`, `tests`, `frontend`. Scope is optional but preferred.

**Subject rules:** imperative mood, lowercase description, no trailing period, under 50 characters.

**Breaking changes:** append `!` before the colon (`feat(agents)!: ...`) or add a `BREAKING CHANGE:` footer.

**Body — Verified section (required when Definition of Done point 3 applies):** the body must include a `Verified:` line recording the observable outcome you personally saw, not an inference from logs.

```
feat(agents): add observability-actuator MCP write calls

Implement create_incident, create_annotation, and alerting_manage_rules
calls triggered when the check engine returns blocker findings.

Verified: incident #1412 visible in the Grafana Cloud stack with title
"Delivery Blocker: STRM-2026-0142-BLOCKERS (2 non-conformances)",
annotated with clause A-2.1 (+3.0 LUFS deviation, ta-IN track).
14 tests pass in .venv; ./scripts/check-disclosure.sh --all clean.
```
