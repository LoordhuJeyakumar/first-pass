# First Pass — Grafana MCP Server Setup

This directory contains the Docker Compose deployment configuration for the self-hosted `grafana/mcp-grafana` server.

## Overview & Architecture

First Pass uses a self-hosted `grafana/mcp-grafana` container running on a GCE Virtual Machine (or locally in Docker) connected via `streamable-http`. This architecture enables unattended AI agent execution using persistent Grafana Service Account Tokens.

## Required Environment Variables

The container reads environment variables directly from `.env` in the repository root:

- `GRAFANA_URL` — Base URL of your Grafana Cloud instance (e.g. `https://your-stack.grafana.net`)
- `GRAFANA_SERVICE_ACCOUNT_TOKEN` — Service Account Token with Editor permissions

## Running the MCP Server with Docker Compose

To bring up the MCP server container:

```bash
docker compose -f mcp/docker-compose.yml up -d
```

To stop the container:

```bash
docker compose -f mcp/docker-compose.yml down
```

To view live container logs:

```bash
docker compose -f mcp/docker-compose.yml logs -f
```

## Verifying the MCP Server

Verify that the MCP server is running, authenticated, and exposing allowlisted tools:

```bash
./scripts/verify_mcp.sh
```

The script initializes an authenticated session over HTTP with Bearer token authentication and verifies tool schemas (e.g. `create_incident`).
