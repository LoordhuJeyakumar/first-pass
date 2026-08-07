#!/usr/bin/env bash
set -euo pipefail

# Verification script for Grafana MCP Server (Step 2 of Increment 1)
# Tests connection and tool availability on the self-hosted mcp-grafana HTTP endpoint.

MCP_URL="${MCP_SERVER_URL:-http://localhost:8000/mcp}"

echo "📡 Initializing session with Grafana MCP Server at $MCP_URL..."

HEADERS_FILE=$(mktemp)
INIT_RESPONSE=$(curl -s -i -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
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

echo "✅ Session established: $SESSION_ID"

echo "📡 Requesting tools list with session header..."
TOOLS_RESPONSE=$(curl -s -X POST "$MCP_URL" \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/list",
    "params": {}
  }')

if echo "$TOOLS_RESPONSE" | grep -q "result"; then
  echo "✅ MCP Server tools/list responded successfully."
  echo "✅ MCP Layer verification completed."
  exit 0
else
  echo "❌ MCP Server tools/list failed!"
  echo "$TOOLS_RESPONSE"
  exit 1
fi
