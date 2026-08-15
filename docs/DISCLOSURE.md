# Disclosure & Security Policy

This repository is public. Because git history is permanent, boundary rules and secret management policies are strictly defined and automatically enforced.

## Disclosure Boundary Rule

**`docs/` is written for public readers. `docs/internal/` is written for maintainers and is never committed.**

Public documentation is authored specifically for outside readers, judges, and developers. Internal notes, raw research, and personal planning materials remain in `docs/internal/` (which is gitignored and managed in a separate private repository).

## Repository Contents

| Component | Description |
|---|---|
| `README.md` | System overview, setup guide, architecture summary, and data provenance. |
| `AGENTS.md` | Single source of truth for AI agent system instructions and constraints. |
| `docs/ARCHITECTURE.md` | Technical design, single-agent MCP architecture, deterministic check engine, and planned roadmap. |
| `docs/DOMAIN.md` | Primer on film delivery QC concepts and multi-language dependencies. |
| `docs/DISCLOSURE.md` | Secret hygiene and disclosure policy enforcement rules. |
| `docs/evidence.md` | Verified citations supporting domain facts. |
| `agents/ mcp/ data/ frontend/ (Planned) scripts/ tests/` | Source code, synthetic data, verification scripts, and test suite. |
| `LICENSE`, `.env.example` | MIT License; environment variable template with uppercase placeholders. |

## Secret Hygiene & Environment Rules

1. **No Credentials Committed**: Real API keys, Grafana service-account tokens, OAuth credentials, or private keys must never be committed.
2. **Local Environment**: Real secrets live exclusively in `.env` (gitignored) locally or in Google Secret Manager in production.
3. **Placeholder Format**: Placeholders in `.env.example` must use uppercase strings (e.g. `glsa_REPLACE_ME`, `YOUR-STACK.grafana.net`). Lowercase hostnames or realistic token formats will trigger pre-commit disclosure tripwires.

## Automated Enforcement

Disclosure compliance is verified using the audit script:

```bash
./scripts/check-disclosure.sh            # Audit staged files (pre-commit hook)
./scripts/check-disclosure.sh --all      # Audit all tracked files in working tree
./scripts/check-disclosure.sh --history  # Scan full git commit history
```

The pre-commit hook automatically blocks commits containing:
- Any file path matching `docs/internal/`.
- Real `.env` files (or improperly named token files).
- Credential patterns (Grafana tokens, GCP API keys, private key blocks).
- Configured secret tripwire phrases.

### Enabling the Git Hook

Git hooks are not enabled automatically upon clone. Enable the hook once per workspace:

```bash
git config core.hooksPath .githooks
```
