#!/usr/bin/env bash
set -euo pipefail

# Verification script for Grafana MCP Server
# Tests connection, authentication, and tool availability on the self-hosted mcp-grafana HTTP endpoint.

if [[ -z "${GRAFANA_SERVICE_ACCOUNT_TOKEN:-}" ]]; then
  echo "❌ Error: GRAFANA_SERVICE_ACCOUNT_TOKEN is not set in environment."
  echo "Please export GRAFANA_SERVICE_ACCOUNT_TOKEN or source .env before running this script."
  exit 1
fi

MCP_URL="${MCP_SERVER_URL:-http://localhost:8000/mcp}"

echo "📡 Initializing authenticated session with Grafana MCP Server at $MCP_URL..."

HEADERS_FILE=$(mktemp)
INIT_RESPONSE=$(curl -s -i -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${GRAFANA_SERVICE_ACCOUNT_TOKEN}" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2024-11-05",
      "capabilities": {},
      "clientInfo": {"name": "verify-mcp-script", "version": "1.0"}
    }
  }' -D "$HEADERS_FILE")

SESSION_ID=$(grep -i "mcp-session-id" "$HEADERS_FILE" | tr -d '\r' | awk '{print $2}')
rm -f "$HEADERS_FILE"

if [[ -z "$SESSION_ID" ]]; then
  echo "❌ Failed to obtain Mcp-Session-Id from MCP Server."
  echo "$INIT_RESPONSE"
  exit 1
fi

echo "✅ Authenticated session established: $SESSION_ID"

echo "📡 Requesting tools list with session header & Bearer token..."
TOOLS_RESPONSE=$(curl -s -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer ${GRAFANA_SERVICE_ACCOUNT_TOKEN}" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }')

if echo "$TOOLS_RESPONSE" | grep -q "result"; then
  echo "✅ MCP Server tools/list responded successfully."
  echo "🔍 Extracting schema for 'create_incident' tool..."
  echo "$TOOLS_RESPONSE" | python3 -c "
import sys, json

text = sys.stdin.read()
# Handle Streamable HTTP SSE payload (prefixed with 'data: ')
for line in text.splitlines():
    line_clean = line.strip()
    if line_clean.startswith('data:'):
        line_clean = line_clean[5:].strip()
    if not line_clean:
        continue
    try:
        data = json.loads(line_clean)
        tools = data.get('result', {}).get('tools', [])
        incident_tool = next((t for t in tools if t.get('name') == 'create_incident'), None)
        if incident_tool:
            print('\n--- create_incident Schema ---')
            print(json.dumps(incident_tool, indent=2))
            sys.exit(0)
    except Exception:
        continue
print('⚠️ create_incident tool not found in list!')
"
  echo "✅ MCP Layer verification completed."
  exit 0
else
  echo "❌ MCP Server tools/list failed!"
  echo "$TOOLS_RESPONSE"
  exit 1
fi
