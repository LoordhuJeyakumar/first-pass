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
- `GRAFANA_PUBLIC_DASHBOARD_URL` — Externally shared Delivery Readiness URL (no login); set after enabling public share
- `GRAFANA_SERVICE_ACCOUNT_TOKEN` — Service Account Token with Editor plus `dashboards.public:write` (or Admin / `fixed:dashboards.public:writer`)
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

### Run against Clean Master (StreamOne PASS)
```bash
.venv/bin/python -m agents.orchestrator --master data/masters/master_clean.json
```

### Run against HallArc Clean Master (HallArc PASS)
```bash
.venv/bin/python -m agents.orchestrator --master data/masters/master_hallarc_clean.json --spec data/specs/hallarc.json
```

Against StreamOne this file is REJECT (6 blockers): five `A-2.1` loudness findings (−24 LUFS is +3 LU vs −27) plus `V-1.3` (BT.709 ≠ BT.2020).

### Run against Blockers Master (REJECT — 3 Blockers)
```bash
.venv/bin/python -m agents.orchestrator --master data/masters/master_blockers.json
```

### Run against Warnings Master (PASS with warnings)
```bash
.venv/bin/python -m agents.orchestrator --master data/masters/master_warnings.json
```

### Verdict matrix

| Master | HallArc | StreamOne |
|---|---|---|
| `master_blockers.json` | REJECT (6 blockers) | REJECT (3 blockers) |
| `master_clean.json` | REJECT (6 blockers) | PASS (0 blockers) |
| `master_warnings.json` | REJECT (2 blockers / 1 warning) | PASS (0 blockers / 1 warning) |
| `master_hallarc_clean.json` | PASS (0 blockers) | REJECT (6 blockers) |

### CLI Options

- `--master`, `-m`: Path to custom master metadata JSON file.
- `--spec`: Path to spec JSON file (default: `data/specs/streamone.json`).
- `--verbose`, `-v`: Enable verbose DEBUG logging.
- `--plain`: Use plain stdlib logging output instead of Rich formatting.
- `--help`: Display CLI help message:

```bash
.venv/bin/python -m agents.orchestrator --help
```

## Real measurement (file → master JSON → verdict)

`agents/measure.py` generates a short lavfi 1 kHz sine WAV (pcm_s24le), measures integrated loudness and true peak with ffmpeg `ebur128`, and maps ffprobe audio structure into master JSON. The adapter also reads real delivery containers (MXF, MOV, WAV); loudness is measured from the audio essence — the container is irrelevant to the number, and we test that. Video colour/resolution/frame rate, packaging, timed text, and certification are **declared** — they are not guessed from the file.

Requires system `ffmpeg`/`ffprobe` (not a pip package). Example MXF (same codecs as a typical HD delivery; both lavfi inputs use an explicit 6s duration):

```bash
ffmpeg -y \
  -f lavfi -i "testsrc=size=1920x1080:rate=25:duration=6" \
  -f lavfi -i "sine=frequency=1000:duration=6:sample_rate=48000" \
  -af volume=-3dB -c:v mpeg2video -b:v 20M -c:a pcm_s24le -f mxf out.mxf
.venv/bin/python -m agents.measure out.mxf --evaluate
```

### Generate a failing loudness clip and evaluate

```bash
.venv/bin/python -m agents.measure --generate fail --evaluate \
  --master-id STRM-MEAS-001 \
  --declare-language ta-IN \
  --declare-role original \
  --declare-timed-text ta-IN \
  --declare-cert ta-IN=cleared \
  --declare-naming-ok true \
  --declare-color-primaries BT.2020 \
  --declare-transfer PQ \
  --declare-resolution 3840x2160 \
  --declare-frame-rate 24
```

`--generate pass` uses `volume=-6dB` (~-27.1 LUFS). Point the first argument at an existing WAV, MXF, or MOV instead of `--generate` to measure a file. Output prints the ebur128 Summary, the adapter JSON, undeclared-field notices, and the engine verdict.
