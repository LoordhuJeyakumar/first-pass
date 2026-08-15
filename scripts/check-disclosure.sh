#!/usr/bin/env bash
set -euo pipefail

# Disclosure audit script for First Pass.
# Prevents accidental leaks of internal docs, secrets, tokens, or personal info.
#
# Usage:
#   ./scripts/check-disclosure.sh            # audit git index / staged files (hook mode)
#   ./scripts/check-disclosure.sh --all      # audit all tracked files in working tree
#   ./scripts/check-disclosure.sh --history  # scan every commit ever made

MODE="staged"
if [[ "${1:-}" == "--all" ]]; then
  MODE="all"
elif [[ "${1:-}" == "--history" ]]; then
  MODE="history"
fi

ERRORS=0

echo "🔍 Running disclosure audit (mode: $MODE)..."

# Function to check file list
check_files() {
  local files="$1"
  if [[ -z "$files" ]]; then
    return 0
  fi

  # 1. Check for docs/internal/
  while IFS= read -r f; do
    if [[ "$f" =~ ^docs/internal/ ]]; then
      echo "❌ ERROR: Internal path staged/tracked: $f"
      ERRORS=$((ERRORS + 1))
    fi
  done <<< "$files"

  # 2. Check for secret file names
  while IFS= read -r f; do
    local base
    base=$(basename "$f")
    if [[ "$base" == ".env" ]] || [[ "$base" =~ ^\.env\. ]] && [[ "$base" != ".env.example" ]]; then
      echo "❌ ERROR: Real .env file staged/tracked: $f"
      ERRORS=$((ERRORS + 1))
    fi
    if [[ "$base" =~ \.token$ ]] || [[ "$base" =~ credentials.*\.json$ ]] || [[ "$base" =~ service-account.*\.json$ ]] || [[ "$base" == "GEMINI.local.md" ]]; then
      echo "❌ ERROR: Secret/token filename staged/tracked: $f"
      ERRORS=$((ERRORS + 1))
    fi
  done <<< "$files"
}

if [[ "$MODE" == "staged" ]]; then
  FILES=$(git diff --cached --name-only || true)
  check_files "$FILES"
elif [[ "$MODE" == "all" ]]; then
  FILES=$(git ls-files || true)
  check_files "$FILES"
elif [[ "$MODE" == "history" ]]; then
  echo "Scanning commit history for prohibited filenames or patterns..."
  if git log --name-only --format="" | grep -E "^docs/internal/|^\.env$" > /dev/null 2>&1; then
    echo "❌ ERROR: Found historical commits containing docs/internal/ or .env files!"
    ERRORS=$((ERRORS + 1))
  fi
fi

# Secret pattern checks across tracked files
if command -v git &> /dev/null; then
  # Check for Grafana service account token pattern (glsa_ followed by 30+ alnum)
  if git grep -iE 'glsa_[A-Za-z0-9_]{20,}' -- ':!scripts/check-disclosure.sh' > /dev/null 2>&1; then
    echo "❌ ERROR: Grafana service account token pattern matched in tracked files!"
    ERRORS=$((ERRORS + 1))
  fi

  # Check for Google API key pattern
  if git grep -E 'AIzaSy[A-Za-z0-9_-]{33}' -- ':!scripts/check-disclosure.sh' > /dev/null 2>&1; then
    echo "❌ ERROR: Google API key pattern matched in tracked files!"
    ERRORS=$((ERRORS + 1))
  fi

  # Check for private key blocks
  if git grep -E '-----BEGIN PRIVATE KEY-----' -- ':!scripts/check-disclosure.sh' > /dev/null 2>&1; then
    echo "❌ ERROR: Private key block matched in tracked files!"
    ERRORS=$((ERRORS + 1))
  fi

  # Check for Grafana Cloud stack hostname pattern ([a-z0-9-]+\.grafana\.net) excluding legitimate placeholders
  if git grep -iE '\b[a-z0-9-]+\.grafana\.net\b' -- ':!scripts/check-disclosure.sh' | grep -ivE '(your-stack|YOUR-REGION|prod-01|us-central1)\.grafana\.net' > /dev/null 2>&1; then
    echo "❌ ERROR: Real Grafana Cloud stack hostname matched in tracked files!"
    ERRORS=$((ERRORS + 1))
  fi
fi

# Check optional project-specific tripwires if present
TRIPWIRE_FILE="docs/internal/disclosure-phrases.txt"
if [[ -f "$TRIPWIRE_FILE" ]] && command -v git &> /dev/null; then
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    label="${line%%|*}"
    regex="${line#*|}"
    if git grep -iE "$regex" -- ':!docs/internal/' ':!scripts/check-disclosure.sh' > /dev/null 2>&1; then
      echo "❌ ERROR: Tripwire phrase '$label' ($regex) matched in public tracked files!"
      ERRORS=$((ERRORS + 1))
    fi
  done < "$TRIPWIRE_FILE"
fi

if [[ $ERRORS -eq 0 ]]; then
  echo "✅ Disclosure audit passed cleanly."
  exit 0
else
  echo "❌ Disclosure audit failed with $ERRORS error(s)."
  exit 1
fi
