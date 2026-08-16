# First Pass

[![CI](https://github.com/LoordhuJeyakumar/first-pass/actions/workflows/ci.yml/badge.svg)](https://github.com/LoordhuJeyakumar/first-pass/actions/workflows/ci.yml)

**Agents that catch a film delivery rejection before the platform does.**

Roughly a quarter of film masters fail platform Quality Control (QC) on first submission — most often for mundane, preventable reasons such as an audio mix delivered at theatrical loudness (~−24 LUFS) against a streaming platform spec requiring ~−27 LUFS. Every rejection incurs redelivery fees and risks missing an announced premiere date.

First Pass treats delivery readiness as an **observability and automated action problem**. Driven by a deterministic Python check engine and powered by Google ADK with Gemini on Vertex AI, a single bounded agent evaluates technical master metadata against a platform's delivery specification. Python deterministically pushes the **Delivery Readiness** dashboard directly to Grafana Cloud (`POST /api/dashboards/db`), while the agent acts through four allowlisted Grafana MCP tools:

- Opens an **incident** when delivery blockers are detected (`create_incident`).
- Posts finding details and clause non-conformances to the incident activity log (`add_activity_to_incident`).
- Attaches timeline **annotations** to the delivery readiness dashboard (`create_annotation`).
- Configures or verifies provisioned **alert rules** for delivery blocker detection (`alerting_manage_rules`).

The operator gets a clear operational answer: **PASS** or **REJECT — N blockers**, each traced back to its specific spec clause, with a live action ledger of every agent action linking into Grafana. For pan-India releases, the deterministic check engine evaluates Central Board of Film Certification (CBFC) regulatory gating dependencies across simultaneous multi-language releases.

Built for the **Agentic Cinema Hackathon (Grafana Track)**, 2026.

## Architecture & System Design

Full details are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

In brief:
- **Orchestration**: A single Google ADK agent (Python) runs on Google Cloud Platform with Gemini models via Vertex AI.
- **Grafana MCP Server**: A self-hosted `grafana/mcp-grafana` server running in Docker on a virtual machine, configured with `-t streamable-http` and authenticated using a Grafana service-account token.
- **Unattended Authentication**: The MCP server is self-hosted rather than using Grafana Cloud's hosted endpoint because the hosted endpoint authenticates via interactive OAuth 2.1 without a machine-token path. Self-hosting with a service-account token allows unattended agents to operate reliably without human manual authentication prompts.
- **Deterministic Check Engine**: All measurements and clause checks are performed in pure, deterministic Python code. The LLM orchestrates workflows, interprets specs, and executes Grafana write actions, but never invents or calculates numbers.

For an introduction to film delivery QC terminology, see [`docs/DOMAIN.md`](docs/DOMAIN.md).

## Operator Console

A FastAPI + Jinja2 + vanilla JS single-page console in [`frontend/`](frontend/) provides:

- **Verdict banner** — PASS (green) or REJECT — N blockers (red), readable from across a room.
- **Fix list** — every spec non-conformance with clause ID, severity, measured vs expected, language, and message.
- **Action ledger** — live log of every MCP write the agent performs, each row linking into Grafana (incident, annotation, alert rule).
- **Trigger button** — runs the full pipeline on demand against any of the three masters. Judges can click RUN to generate fresh telemetry regardless of the 14-day Grafana retention window.

```bash
# One-time setup:
pip install -r frontend/requirements.txt

# Start the console:
./run_console.sh        # http://localhost:8080
```

See [`frontend/README.md`](frontend/README.md) for full setup, environment variable reference, and API documentation.

## Setup & Environment

1. Copy the template and set up your environment variables (never commit `.env`):
   ```bash
   cp .env.example .env
   ```
2. **Grafana Cloud**: Create a Grafana Cloud stack. Ensure an administrator accepts the Grafana Assistant terms, create a service account with the Editor role, and generate a service-account token.
3. **Google Cloud**: Create a GCP project, activate billing, and enable the Vertex AI API (`aiplatform.googleapis.com`).
4. **MCP Server**: Refer to [`mcp/`](mcp/) for Docker Compose instructions to deploy `grafana/mcp-grafana` on your GCE Virtual Machine.
5. **Agents**: Requires Python 3.14 (pinned in `.python-version`). Refer to [`agents/`](agents/) for virtualenv creation (`.venv`), package installation (`agents/requirements.txt`), and run instructions.

## Data Provenance

All master metadata and delivery specifications in [`data/`](data/) are **synthetic**, authored specifically for this project and modeled after publicly documented structures (such as MediaInfo/ffprobe structures and public delivery specs). The platform "StreamOne" is entirely fictional. No proprietary studio assets, confidential rejection reports, or copyrighted third-party media assets are used.

## Contributing & Disclosure Rules

Instructions for AI coding agents live in [`AGENTS.md`](AGENTS.md).

Before committing code, arm the disclosure pre-commit hook (git does not propagate hooks automatically):

```bash
git config core.hooksPath .githooks
./scripts/check-disclosure.sh --all
```

Every push and pull request runs the same disclosure audit and `./scripts/check-invariants.sh` (16 checks, no secrets) on GitHub Actions.

For full details on public documentation boundaries and secret management rules, see [`docs/DISCLOSURE.md`](docs/DISCLOSURE.md).

## License

This project is licensed under the MIT License — see [`LICENSE`](LICENSE).
