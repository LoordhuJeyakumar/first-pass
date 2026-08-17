#!/usr/bin/env bash
# Copy runtime trees to the GCE console host over IAP, restart the unit, verify TLS.
# Never prints project id, instance name, hostname, IP, or CONSOLE_URL.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

DEPLOY_GCP_PROJECT="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
if [[ -z "$DEPLOY_GCP_PROJECT" ]]; then
  DEPLOY_GCP_PROJECT="$(gcloud config get-value project 2>/dev/null || true)"
fi
export DEPLOY_GCP_PROJECT
export GCE_ZONE
export GCE_INSTANCE

fail() {
  echo "deploy failed: $1" >&2
  exit 1
}

[[ -n "${GCE_INSTANCE:-}" ]] || fail "GCE_INSTANCE is not set"
[[ -n "${GCE_ZONE:-}" ]] || fail "GCE_ZONE is not set"
[[ -n "${CONSOLE_URL:-}" ]] || fail "CONSOLE_URL is not set"
[[ -n "$DEPLOY_GCP_PROJECT" ]] || fail "GCP project is not set (env or gcloud config)"

command -v rsync >/dev/null || fail "rsync is not installed on this machine"
command -v gcloud >/dev/null || fail "gcloud is not installed on this machine"
command -v curl >/dev/null || fail "curl is not installed on this machine"

REMOTE_ROOT="/home/john/first-pass"
PROOF_REL="frontend/app.py"
COPY_DIRS=(agents frontend scripts data)

vm_ssh() {
  gcloud compute ssh "$GCE_INSTANCE" \
    --tunnel-through-iap \
    --zone "$GCE_ZONE" \
    --project "$DEPLOY_GCP_PROJECT" \
    --quiet \
    --command "$1"
}

echo "ensuring rsync is installed on the VM"
vm_ssh "sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y rsync >/dev/null"

# Direct `rsync -e "gcloud compute ssh … --"` fails: rsync injects ssh flags and
# gcloud never sees INSTANCE ("argument [USER@]INSTANCE: Must be specified").
# scripts/iap-rsh.sh reorders args. Still rsync with --delete; never scp.
RSYNC_RSH="${ROOT}/scripts/iap-rsh.sh"

echo "copying runtime directories (rsync --delete, per-directory)"
for dir in "${COPY_DIRS[@]}"; do
  echo "  syncing ${dir}/"
  rsync -az --delete \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    -e "$RSYNC_RSH" \
    "${dir}/" \
    "${GCE_INSTANCE}:${REMOTE_ROOT}/${dir}/" \
    || fail "rsync -e iap-rsh wrapper failed for ${dir}/ (no scp fallback)"
done

echo "removing private docs tree from the VM if present"
vm_ssh "rm -rf ${REMOTE_ROOT}/docs/internal && test ! -e ${REMOTE_ROOT}/docs/internal"

echo "verifying a runtime file transferred (byte size)"
[[ -f "$PROOF_REL" ]] || fail "local proof file missing"
local_bytes="$(wc -c < "$PROOF_REL" | tr -d '[:space:]')"
remote_bytes="$(vm_ssh "wc -c < ${REMOTE_ROOT}/${PROOF_REL}" | tr -d '[:space:]')"
[[ -n "$local_bytes" && -n "$remote_bytes" ]] || fail "could not read proof file sizes"
if [[ "$local_bytes" != "$remote_bytes" ]]; then
  fail "proof file size mismatch (copy did not land)"
fi
echo "  proof file sizes match (${local_bytes} bytes)"

echo "restarting first-pass-console"
vm_ssh "sudo systemctl restart first-pass-console && sudo systemctl is-active first-pass-console"

echo "checking hosted URL (TLS verify, expect HTTP 200)"
http_code=""
for _ in 1 2 3 4 5 6 7 8 9 10; do
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 "$CONSOLE_URL" || true)"
  if [[ "$http_code" == "200" ]]; then
    break
  fi
  sleep 2
done
[[ "$http_code" == "200" ]] || fail "hosted URL returned HTTP ${http_code:-none}, expected 200"

echo "deploy ok"
