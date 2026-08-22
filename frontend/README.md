# Operator Console — First Pass

FastAPI + Jinja2 + vanilla JS web console. Single page with:

1. **Verdict banner** — PASS (green) or REJECT — N blockers (red), unmissable.
2. **Fix list** — every spec non-conformance with clause ID, severity, measured vs expected, language, and human-readable message.
3. **Per-language readiness grid** — ratio bar per language (audio ∩ subtitles ∩ certification).
4. **Action ledger** — live log of every MCP write the agent performed, each linking into Grafana.

A **RUN** button triggers the full pipeline against any of the four masters in `data/masters/`.

## One-time setup

Install the console dependencies into the project's shared virtual environment:

```bash
pip install -r frontend/requirements.txt
```

This must be run once after cloning, or after the `.venv` is recreated.
The packages (`fastapi`, `uvicorn`, `jinja2`) are non-AI libraries and do not conflict with
existing `agents/requirements.txt` packages.

## Running locally

```bash
./run_console.sh          # http://localhost:8080
PORT=9000 ./run_console.sh
```

All environment variables are read from `.env` at startup. Ensure `.env` is populated before
starting (copy `.env.example` and fill in real values — never commit `.env`).

## Guardrails

| Guardrail | Behaviour |
|---|---|
| **Single flight** | A second RUN click while a run is active returns HTTP 409. The button is also disabled in the UI. |
| **Cooldown** | A configurable quiet period between runs prevents duplicate incidents and excess Vertex AI spend. Controlled by `CONSOLE_COOLDOWN_SECONDS` (default: 30). Returns HTTP 429 with `Retry-After`. |
| **No credential exposure** | `GRAFANA_URL` / `GRAFANA_PUBLIC_DASHBOARD_URL` are read from the environment at request time and never rendered as visible text. Ledger links display as `"Incident #142 (Grafana sign-in)"` etc. — never the raw URL. |

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GRAFANA_URL` | Yes | Grafana Cloud stack base URL. Used to build ledger deeplinks. |
| `GRAFANA_PUBLIC_DASHBOARD_URL` | No | Externally shared Delivery Readiness URL (no login). Preferred for the orientation Dashboard link when set. |
| `GRAFANA_SERVICE_ACCOUNT_TOKEN` | Yes | Service account token for the pipeline (needs `dashboards.public:write` to enable the public share). |
| `GOOGLE_CLOUD_PROJECT` | Yes | GCP project ID for Vertex AI. |
| `MCP_SERVER_URL` | Yes | URL of the self-hosted mcp-grafana server. |
| `CONSOLE_COOLDOWN_SECONDS` | No | Cooldown in seconds between runs (default: 30). |

## API endpoints

```
GET  /                      Renders the console page
GET  /api/masters           Lists available master JSON files
POST /api/run               {"master": "master_blockers.json"} → {"run_id": "uuid"}
                            409 if locked, 429 if in cooldown
GET  /api/run/{run_id}      {"status": "running|done|failed", "verdict": ..., ...}
```
