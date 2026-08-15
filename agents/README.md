# First Pass — ADK Agent & QC Engine Setup

This directory contains the Google ADK orchestrator agent (`agents/orchestrator.py`), deterministic check engine (`agents/check_engine.py`), and telemetry emitters (`agents/telemetry.py`).

## Requirements & Python Environment

This project requires **Python 3.14** (pinned in `.python-version`).

### 1. Create Virtual Environment

From the repository root directory:

```bash
python3 -m venv .venv
```

### 2. Install Dependencies

```bash
.venv/bin/pip install -r agents/requirements.txt
```

Alternatively, activate the virtual environment before installing:

```bash
source .venv/bin/activate
pip install -r agents/requirements.txt
```

## Required Environment Variables

Configure these variables in your `.env` file (copied from `.env.example`). Variable names are listed below; never commit real credentials:

- `GRAFANA_URL` — Base URL of your Grafana Cloud instance (e.g. `https://your-stack.grafana.net`)
- `GRAFANA_SERVICE_ACCOUNT_TOKEN` — Service Account Token with Editor permissions
- `PROM_REMOTE_WRITE_URL` — Prometheus remote-write endpoint
- `PROM_USERNAME` — Prometheus instance ID / username
- `LOKI_PUSH_URL` — Loki push API endpoint
- `LOKI_USERNAME` — Loki instance ID / username
- `GRAFANA_CLOUD_API_KEY` — Grafana Cloud API key / token
- `MCP_SERVER_URL` — URL of the self-hosted MCP server (default: `http://localhost:8000/mcp`)
- `GOOGLE_CLOUD_PROJECT` — GCP Project ID for Vertex AI Gemini API calls
- `GOOGLE_CLOUD_LOCATION` — GCP region (e.g. `us-central1`)

## Running a QC Pass (CLI)

Run the orchestrator CLI against synthetic master metadata files in `data/masters/` using `.venv/bin/python`:

### Run against Clean Master (PASS)
```bash
.venv/bin/python -m agents.orchestrator --master data/masters/master_clean.json
```

### Run against Blockers Master (REJECT — 3 Blockers)
```bash
.venv/bin/python -m agents.orchestrator --master data/masters/master_blockers.json
```

### Run against Warnings Master (PASS with warnings)
```bash
.venv/bin/python -m agents.orchestrator --master data/masters/master_warnings.json
```

### CLI Options

- `--master`, `-m`: Path to custom master metadata JSON file.
- `--verbose`, `-v`: Enable verbose DEBUG logging.
- `--plain`: Use plain stdlib logging output instead of Rich formatting.
- `--help`: Display CLI help message:

```bash
.venv/bin/python -m agents.orchestrator --help
```
