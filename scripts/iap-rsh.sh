#!/usr/bin/env bash
# rsync -e helper: rsync is not a drop-in for gcloud compute ssh.
# rsync invokes: <this> [ssh-flags] HOST [remote command...]
# gcloud wants:  gcloud compute ssh HOST [gcloud flags] -- [remote command...]
set -euo pipefail

instance=""
remote=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    -l)
      shift
      [[ $# -gt 0 ]] && shift
      ;;
    -p|-o|-F)
      shift
      [[ $# -gt 0 ]] && shift
      ;;
    --)
      shift
      break
      ;;
    -*)
      shift
      ;;
    *)
      instance="$1"
      shift
      remote=("$@")
      break
      ;;
  esac
done

if [[ "$instance" == *@* ]]; then
  instance="${instance##*@}"
fi

[[ -n "$instance" ]] || {
  echo "iap-rsh: no instance in rsync remote-shell args" >&2
  exit 1
}
[[ -n "${GCE_ZONE:-}" ]] || {
  echo "iap-rsh: GCE_ZONE is not set" >&2
  exit 1
}
[[ -n "${DEPLOY_GCP_PROJECT:-}" ]] || {
  echo "iap-rsh: DEPLOY_GCP_PROJECT is not set" >&2
  exit 1
}

exec gcloud compute ssh "$instance" \
  --tunnel-through-iap \
  --zone "$GCE_ZONE" \
  --project "$DEPLOY_GCP_PROJECT" \
  --quiet \
  -- \
  "${remote[@]}"
