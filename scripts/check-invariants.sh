#!/usr/bin/env bash
set -euo pipefail

# Deterministic Invariant Checker for First Pass.
# Enforces non-negotiable architectural constraints, safety bounds, and quality floors.
#
# Usage:
#   ./scripts/check-invariants.sh

ERRORS=0

echo "🔍 Running deterministic invariant audit..."

# Check 1: Every non-stdlib package in agents/requirements.txt is imported somewhere in the codebase.
echo "  [Check 1/16] Verifying all non-stdlib packages in agents/requirements.txt are imported..."
C1_OUT=$(python3 -c '
import os, sys, glob, re

req_file = "agents/requirements.txt"
if not os.path.exists(req_file):
    print("MISSING_REQ_FILE")
    sys.exit(1)

with open(req_file, "r") as f:
    lines = f.readlines()

pkgs = []
for line in lines:
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    pkg = re.split(r"[=<>]", line)[0].strip()
    if pkg:
        pkgs.append(pkg)

PACKAGE_IMPORT_MAP = {
    "google-adk": "google.adk",
    "google-genai": "google.genai",
    "google-generativeai": "google.generativeai",
    "google-cloud-aiplatform": "google.cloud.aiplatform",
    "pytest-cov": "pytest_cov",
}

py_files = glob.glob("**/*.py", recursive=True)

unimported = []
for pkg in pkgs:
    import_name = PACKAGE_IMPORT_MAP.get(pkg, pkg.replace("-", "_"))
    
    found = False
    for py_file in py_files:
        if ".venv" in py_file or "__pycache__" in py_file:
            continue
        try:
            with open(py_file, "r", encoding="utf-8") as pf:
                content = pf.read()
            if import_name in content or pkg in content:
                found = True
                break
        except Exception:
            pass
    if not found:
        unimported.append(pkg)

if unimported:
    print("UNIMPORTED:" + ",".join(unimported))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C1_OUT" == "OK" ]]; then
  echo "  ✅ Check 1 passed: All requirements.txt packages are imported."
else
  echo "  ❌ ERROR Check 1 failed: $C1_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 2: At least one accepted Google AI package is imported under agents/
echo "  [Check 2/16] Verifying accepted Google AI SDK import under agents/..."
C2_OUT=$(python3 -c '
import glob, sys, re

accepted_patterns = [
    r"(import|from)\s+google\.adk\b",
    r"(import|from)\s+google\.genai\b",
    r"(import|from)\s+google\.generativeai\b",
    r"(import|from)\s+google\.cloud\.aiplatform\b",
]
agent_files = glob.glob("agents/**/*.py", recursive=True)

found = False
for f in agent_files:
    if "__pycache__" in f:
        continue
    with open(f, "r", encoding="utf-8") as pf:
        lines = pf.readlines()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        for pat in accepted_patterns:
            if re.search(pat, stripped):
                found = True
                break
        if found:
            break
    if found:
        break

if found:
    print("OK")
else:
    print("NO_GOOGLE_AI_IMPORT")
    sys.exit(1)
' 2>&1 || true)

if [[ "$C2_OUT" == "OK" ]]; then
  echo "  ✅ Check 2 passed: Accepted Google AI package is imported under agents/."
else
  echo "  ❌ ERROR Check 2 failed: No accepted Google AI package imported under agents/."
  ERRORS=$((ERRORS + 1))
fi

# Check 3: No import of any denylisted package
echo "  [Check 3/16] Verifying no denylisted AI orchestration packages are imported..."
C3_OUT=$(python3 -c '
import glob, sys, re

denylist = ["langchain", "langgraph", "crewai", "autogen", "llama_index", "llama-index", "openai", "anthropic", "semantic_kernel", "semantic-kernel", "haystack"]
py_files = glob.glob("**/*.py", recursive=True)

violations = []
for f in py_files:
    if ".venv" in f or "__pycache__" in f:
        continue
    with open(f, "r", encoding="utf-8") as pf:
        content = pf.read()
    for item in denylist:
        import_pattern = r"(import\s+" + re.escape(item) + r"|from\s+" + re.escape(item) + r")"
        if re.search(import_pattern, content):
            violations.append(f"{f}:{item}")

if violations:
    print("DENYLIST_VIOLATIONS:" + ",".join(violations))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C3_OUT" == "OK" ]]; then
  echo "  ✅ Check 3 passed: No denylisted packages imported."
else
  echo "  ❌ ERROR Check 3 failed: Denylisted package imported: $C3_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 4: agents/check_engine.py imports only from stdlib allowlist
echo "  [Check 4/16] Verifying agents/check_engine.py stdlib allowlist compliance..."
C4_OUT=$(python3 -c '
import ast, sys

allowlist = {
    "typing", "math", "json", "sys", "os", "re", "datetime",
    "collections", "itertools", "dataclasses", "enum", "pathlib",
    "functools", "abc", "random", "hashlib", "copy", "time", "string"
}

target = "agents/check_engine.py"
with open(target, "r", encoding="utf-8") as f:
    tree = ast.parse(f.read(), filename=target)

illegal = []
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            mod = alias.name.split(".")[0]
            if mod not in allowlist:
                illegal.append(mod)
    elif isinstance(node, ast.ImportFrom):
        if node.module:
            mod = node.module.split(".")[0]
            if mod not in allowlist:
                illegal.append(mod)

if illegal:
    print("ILLEGAL_IMPORTS:" + ",".join(illegal))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C4_OUT" == "OK" ]]; then
  echo "  ✅ Check 4 passed: agents/check_engine.py imports strictly from stdlib allowlist."
else
  echo "  ❌ ERROR Check 4 failed: Non-stdlib import in check_engine.py: $C4_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 5: Every file path referenced in README.md and docs/*.md actually exists
echo "  [Check 5/16] Verifying all file paths referenced in README.md and docs/*.md exist..."
C5_OUT=$(python3 -c '
import glob, re, os, sys

md_files = ["README.md"] + glob.glob("docs/*.md")
missing_paths = []

for mdf in md_files:
    if "docs/internal/" in mdf:
        continue
    if not os.path.exists(mdf):
        continue
    dir_of_file = os.path.dirname(mdf) or "."
    with open(mdf, "r", encoding="utf-8") as f:
        content = f.read()

    links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", content)
    for text, target in links:
        if target.startswith("http://") or target.startswith("https://") or target.startswith("mailto:") or target.startswith("#"):
            continue
        clean_target = target.split("#")[0]
        if not clean_target:
            continue
        p1 = os.path.normpath(clean_target)
        p2 = os.path.normpath(os.path.join(dir_of_file, clean_target))
        if not (os.path.exists(p1) or os.path.exists(p2)):
            missing_paths.append(f"{mdf} -> {target}")

if missing_paths:
    print("MISSING_PATHS:" + " | ".join(missing_paths))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C5_OUT" == "OK" ]]; then
  echo "  ✅ Check 5 passed: All referenced file paths in documentation exist."
else
  echo "  ❌ ERROR Check 5 failed: Referenced file path does not exist: $C5_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 6: Every environment variable required via os.getenv() appears in .env.example
echo "  [Check 6/16] Verifying all os.getenv() variables appear in .env.example..."
C6_OUT=$(python3 -c '
import glob, re, os, sys

env_example = ".env.example"
if not os.path.exists(env_example):
    print("MISSING_ENV_EXAMPLE")
    sys.exit(1)

with open(env_example, "r", encoding="utf-8") as f:
    example_content = f.read()

example_keys = set(re.findall(r"^\s*([A-Z0-9_]+)=", example_content, re.MULTILINE))

py_files = glob.glob("**/*.py", recursive=True)
used_vars = set()

for py_file in py_files:
    if ".venv" in py_file or "__pycache__" in py_file:
        continue
    with open(py_file, "r", encoding="utf-8") as pf:
        content = pf.read()
    matches = re.findall(r"os\.getenv\(\s*[\"\x27]([A-Z0-9_]+)[\"\x27]", content)
    for m in matches:
        used_vars.add(m)

missing = sorted(list(used_vars - example_keys))
if missing:
    print("MISSING_VARS:" + ",".join(missing))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C6_OUT" == "OK" ]]; then
  echo "  ✅ Check 6 passed: All os.getenv() variables exist in .env.example."
else
  echo "  ❌ ERROR Check 6 failed: os.getenv() variable missing from .env.example: $C6_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 7: No :latest image tag in any compose file or Dockerfile
echo "  [Check 7/16] Verifying no :latest image tags in compose files or Dockerfiles..."
C7_OUT=$(python3 -c '
import glob, re, sys

candidates = glob.glob("**/docker-compose*.yml", recursive=True) + \
             glob.glob("**/docker-compose*.yaml", recursive=True) + \
             glob.glob("**/Dockerfile*", recursive=True)

unpinned = []
for f in candidates:
    if ".venv" in f or "node_modules" in f:
        continue
    with open(f, "r", encoding="utf-8") as cf:
        content = cf.read()
    if re.search(r":latest\b", content, re.IGNORECASE):
        unpinned.append(f)

if unpinned:
    print("UNPINNED_LATEST:" + ",".join(unpinned))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C7_OUT" == "OK" ]]; then
  echo "  ✅ Check 7 passed: No :latest image tags found."
else
  echo "  ❌ ERROR Check 7 failed: Found :latest image tag in: $C7_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 8: At most one progress log exists in the tree
echo "  [Check 8/16] Verifying at most one progress log exists in tree..."
C8_OUT=$(python3 -c '
import glob, os, sys

logs = glob.glob("**/*PROGRESS*.md", recursive=True) + glob.glob("**/*progress*.md", recursive=True)
logs = sorted(list(set(os.path.normpath(p) for p in logs if ".git" not in p and "node_modules" not in p)))

if len(logs) <= 1:
    print("OK")
else:
    print(f"INVALID_PROGRESS_COUNT:{len(logs)} -> {logs}")
    sys.exit(1)
' 2>&1 || true)

if [[ "$C8_OUT" == "OK" ]]; then
  echo "  ✅ Check 8 passed: At most one progress log exists."
else
  echo "  ❌ ERROR Check 8 failed: Progress log count invariant broken: $C8_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 9: Any HTTP request to the MCP server URL carries an Authorization header
echo "  [Check 9/16] Verifying MCP HTTP calls carry Authorization header..."
C9_OUT=$(python3 -c '
import glob, re, sys

files = glob.glob("**/*.py", recursive=True) + glob.glob("**/*.sh", recursive=True)

unauthorized = []
for f in files:
    if ".venv" in f or "check-invariants.sh" in f:
        continue
    with open(f, "r", encoding="utf-8") as pf:
        content = pf.read()
    if "MCP_SERVER_URL" in content or "MCP_URL" in content or "StreamableHTTPConnectionParams" in content:
        if "Authorization" not in content and "authorization" not in content:
            unauthorized.append(f)

if unauthorized:
    print("MISSING_AUTH_HEADER:" + ",".join(unauthorized))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C9_OUT" == "OK" ]]; then
  echo "  ✅ Check 9 passed: MCP HTTP requests include Authorization header."
else
  echo "  ❌ ERROR Check 9 failed: MCP HTTP request missing Authorization header in: $C9_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 10: All three masters produce documented verdicts
echo "  [Check 10/16] Verifying all three master files produce documented verdicts via check engine..."
C10_OUT=$(python3 -c '
import json, sys
from agents.check_engine import evaluate_master_against_spec

try:
    spec = json.load(open("data/specs/streamone.json", encoding="utf-8"))
    
    c_m = json.load(open("data/masters/master_clean.json", encoding="utf-8"))
    c_rep = evaluate_master_against_spec(c_m, spec)
    if c_rep["verdict"] != "PASS" or c_rep["blocker_count"] != 0:
        print("master_clean mismatch: verdict=" + str(c_rep["verdict"]) + ", blockers=" + str(c_rep["blocker_count"]))
        sys.exit(1)

    b_m = json.load(open("data/masters/master_blockers.json", encoding="utf-8"))
    b_rep = evaluate_master_against_spec(b_m, spec)
    if b_rep["verdict"] != "REJECT" or b_rep["blocker_count"] != 3:
        print("master_blockers mismatch: verdict=" + str(b_rep["verdict"]) + ", blockers=" + str(b_rep["blocker_count"]))
        sys.exit(1)

    w_m = json.load(open("data/masters/master_warnings.json", encoding="utf-8"))
    w_rep = evaluate_master_against_spec(w_m, spec)
    if w_rep["verdict"] != "PASS" or w_rep["blocker_count"] != 0 or w_rep["warning_count"] < 1:
        print("master_warnings mismatch: verdict=" + str(w_rep["verdict"]) + ", blockers=" + str(w_rep["blocker_count"]) + ", warnings=" + str(w_rep["warning_count"]))
        sys.exit(1)

    print("OK")
except Exception as exc:
    print(f"EXCEPTION:{exc}")
    sys.exit(1)
' 2>&1 || true)

if [[ "$C10_OUT" == "OK" ]]; then
  echo "  ✅ Check 10 passed: All three masters produce documented verdicts."
else
  echo "  ❌ ERROR Check 10 failed: $C10_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 11: agents/check_engine.py and agents/telemetry.py line coverage floor
echo "  [Check 11/16] Verifying agents/check_engine.py and agents/telemetry.py test coverage is at 100% floor..."
PYTHON_EXEC=".venv/bin/python"

if ! "$PYTHON_EXEC" -c "import pytest_cov" > /dev/null 2>&1; then
  echo "  ❌ ERROR Check 11 failed: pytest-cov package is missing from environment $PYTHON_EXEC."
  ERRORS=$((ERRORS + 1))
elif "$PYTHON_EXEC" -m pytest --cov=agents.check_engine --cov=agents.telemetry --cov-fail-under=100 tests/ > /dev/null 2>&1; then
  echo "  ✅ Check 11 passed: agents/check_engine.py & agents/telemetry.py line coverage >= 100% floor."
else
  if "$PYTHON_EXEC" -m pytest tests/ > /dev/null 2>&1; then
    echo "  ❌ ERROR Check 11 failed: agents/check_engine.py & agents/telemetry.py line coverage is below the 100% floor."
  else
    echo "  ❌ ERROR Check 11 failed: pytest test suite execution failed."
  fi
  ERRORS=$((ERRORS + 1))
fi

# Check 12: Verifying no subprocess module usage in agents/orchestrator.py
echo "  [Check 12/16] Verifying no subprocess module usage in agents/orchestrator.py..."
C12_OUT=$(python3 -c '
import ast, sys
target = "agents/orchestrator.py"
with open(target, "r", encoding="utf-8") as f:
    tree = ast.parse(f.read(), filename=target)
for node in ast.walk(tree):
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name == "subprocess":
                print("SUBPROCESS_IMPORTED")
                sys.exit(1)
    elif isinstance(node, ast.ImportFrom):
        if node.module == "subprocess":
            print("SUBPROCESS_IMPORTED")
            sys.exit(1)
print("OK")
' 2>&1 || true)

if [[ "$C12_OUT" == "OK" ]]; then
  echo "  ✅ Check 12 passed: No subprocess module usage in agents/orchestrator.py."
else
  echo "  ❌ ERROR Check 12 failed: Found subprocess usage in agents/orchestrator.py: $C12_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 13: Every variable declared in .env.example is read somewhere in the codebase or docker configs
echo "  [Check 13/16] Verifying all .env.example variables are read in codebase or docker configs..."
C13_OUT=$(python3 -c '
import glob, re, os, sys

env_example = ".env.example"
if not os.path.exists(env_example):
    print("MISSING_ENV_EXAMPLE")
    sys.exit(1)

with open(env_example, "r", encoding="utf-8") as f:
    example_content = f.read()

example_keys = set(re.findall(r"^\s*([A-Z0-9_]+)=", example_content, re.MULTILINE))

code_files = glob.glob("**/*.py", recursive=True) + \
             glob.glob("**/docker-compose*.yml", recursive=True) + \
             glob.glob("**/docker-compose*.yaml", recursive=True) + \
             glob.glob("**/Dockerfile*", recursive=True) + \
             glob.glob("**/*.sh", recursive=True)

all_content = ""
for cf in code_files:
    if ".venv" in cf or "__pycache__" in cf or ".env.example" in cf:
        continue
    try:
        with open(cf, "r", encoding="utf-8") as f:
            all_content += "\n" + f.read()
    except Exception:
        pass

unread = []
for key in sorted(list(example_keys)):
    patterns = [
        r"[\"\x27]" + re.escape(key) + r"[\"\x27]",
        r"\$\{?" + re.escape(key) + r"\}?",
        r"\b" + re.escape(key) + r"\b"
    ]
    found = False
    for pat in patterns:
        if re.search(pat, all_content):
            found = True
            break
    if not found:
        unread.append(key)

if unread:
    print("UNREAD_VARS:" + ",".join(unread))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C13_OUT" == "OK" ]]; then
  echo "  ✅ Check 13 passed: All .env.example variables are read in codebase or docker configs."
else
  echo "  ❌ ERROR Check 13 failed: .env.example variable not read anywhere: $C13_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 14: Verifying public documentation capability claims against codebase implementation
echo "  [Check 14/16] Verifying public documentation capability claims against codebase implementation..."
C14_OUT=$(python3 -c '
import glob, os, sys, re, json, ast

orch_path = "agents/orchestrator.py"
tool_filter = set()
agent_count = 0
has_timeseries = False

orch_content = ""
if os.path.exists(orch_path):
    with open(orch_path, "r", encoding="utf-8") as f:
        orch_content = f.read()
    m = re.search(r"tool_filter\s*=\s*\[(.*?)\]", orch_content, re.DOTALL)
    if m:
        tool_filter = set(re.findall(r"[\"\x27]([^\x27\"]+)[\"\x27]", m.group(1)))

    tree = ast.parse(orch_content, filename=orch_path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ("Agent", "LlmAgent"):
                agent_count += 1
            elif isinstance(node.func, ast.Attribute) and node.func.attr in ("Agent", "LlmAgent"):
                agent_count += 1

    has_timeseries = "timeseries" in orch_content

check_engine_path = "agents/check_engine.py"
engine_content = ""
if os.path.exists(check_engine_path):
    with open(check_engine_path, "r", encoding="utf-8") as f:
        engine_content = f.read()

has_true_peak = "true_peak" in engine_content.lower()

if not has_true_peak:
    for spec_file in glob.glob("data/specs/*.json"):
        try:
            with open(spec_file, "r", encoding="utf-8") as f:
                spec_data = json.load(f)
            clauses = spec_data.get("clauses", [])
            for c in clauses:
                chk = json.dumps(c.get("check", {})).lower()
                if "true_peak" in chk:
                    has_true_peak = True
                    break
        except Exception:
            pass

known_mcp_tools = {
    "create_incident", "add_activity_to_incident", "create_annotation",
    "update_dashboard", "alerting_manage_rules", "search_dashboards",
    "get_dashboard_summary", "query_prometheus", "query_loki_logs",
    "list_incidents", "generate_deeplink", "alerting_manage_routing"
}

named_sub_agents = [
    "multi-agent", "agent crew", "sub-agent",
    "Spec-Interpreter", "Release-Coordinator", "QC-Analyst",
    "Observability-Actuator", "Remediation"
]

roadmap_kw = ["planned", "roadmap", "future", "vision", "target", "next", "proposed", "upcoming"]

public_docs = ["README.md", "AGENTS.md"] + [p for p in glob.glob("docs/*.md") if "docs/internal/" not in p]
violations = []
eval_verbs = ["evaluat", "check", "validat", "verif", "measur", "assert", "enforc", "inspect"]

from scripts.doc_conformance import parse_roadmap_heading_states

for doc in public_docs:
    if not os.path.exists(doc):
        continue
    with open(doc, "r", encoding="utf-8") as f:
        lines = f.readlines()

    roadmap_states = parse_roadmap_heading_states(lines)

    for line_num, (line, in_roadmap) in enumerate(zip(lines, roadmap_states), 1):
        if in_roadmap:
            continue

        # NOTE: Check 14 verifies documentation claims against code implementation (e.g., active tools/operations),
        # but cannot catch code behaviors that are absent from documentation (e.g., unmentioned direct REST API calls).
        for tool in known_mcp_tools:
            if tool in line:
                if tool not in tool_filter:
                    violations.append(f"{doc}:{line_num} mentions tool \x27{tool}\x27 not in tool_filter")
                elif tool == "alerting_manage_rules":
                    has_write_op = bool(re.search(r"operation\s*:\s*\\?[\"\x27]?(create|update)", orch_content, re.IGNORECASE))
                    if not has_write_op:
                        violations.append(f"{doc}:{line_num} mentions active capability \x27{tool}\x27 but orchestrator.py lacks a WRITE operation (create/update)")

        if ("True Peak" in line or "dBTP" in line) and any(v in line.lower() for v in eval_verbs) and not has_true_peak:
            violations.append(f"{doc}:{line_num} asserts True Peak / dBTP capability but check engine / specs do not evaluate true_peak")

        if ("timeseries" in line.lower() or "timeline annotation" in line.lower()) and not has_timeseries:
            violations.append(f"{doc}:{line_num} asserts timeseries/timeline annotation but orchestrator lacks timeseries panel")

        if agent_count <= 1:
            line_low = line.lower()
            if not any(r in line_low for r in roadmap_kw):
                for kw in named_sub_agents:
                    if kw.lower() in line_low:
                        violations.append(f"{doc}:{line_num} asserts agent structure claim \x27{kw}\x27 but orchestrator only has {agent_count} agent(s)")

if violations:
    print("CAPABILITY_VIOLATIONS:" + " | ".join(violations))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C14_OUT" == "OK" ]]; then
  echo "  ✅ Check 14 passed: Public documentation capability claims match codebase implementation."
else
  echo "  ❌ ERROR Check 14 failed: $C14_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 15: Verifying code implementation against spec constraints in AGENTS.md (Spec Conformance)
echo "  [Check 15/16] Verifying code implementation against spec constraints in AGENTS.md..."
C15_OUT=$(python3 -c '
import sys
from scripts.doc_conformance import check_spec_conformance
violations = check_spec_conformance()
if violations:
    print("SPEC_CONFORMANCE_VIOLATIONS: " + " | ".join(violations))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C15_OUT" == "OK" ]]; then
  echo "  ✅ Check 15 passed: Codebase implements all spec constraints in AGENTS.md."
else
  echo "  ❌ ERROR Check 15 failed: $C15_OUT"
  ERRORS=$((ERRORS + 1))
fi

# Check 16: Verifying top-level directory claims across public documentation exist on disk
echo "  [Check 16/16] Verifying top-level directory claims in public documentation exist..."
C16_OUT=$(python3 -c '
import sys, glob
from scripts.doc_conformance import check_directory_claims
public_docs = ["README.md", "AGENTS.md"] + [p for p in glob.glob("docs/*.md") if "docs/internal/" not in p]
violations = check_directory_claims(public_docs)
if violations:
    print("DIRECTORY_CLAIM_VIOLATIONS: " + " | ".join(violations))
    sys.exit(1)
else:
    print("OK")
' 2>&1 || true)

if [[ "$C16_OUT" == "OK" ]]; then
  echo "  ✅ Check 16 passed: All top-level directory references in documentation exist."
else
  echo "  ❌ ERROR Check 16 failed: $C16_OUT"
  ERRORS=$((ERRORS + 1))
fi

if [[ $ERRORS -eq 0 ]]; then
  echo "✅ All invariant checks passed cleanly."
  exit 0
else
  echo "❌ Invariant audit failed with $ERRORS error(s)."
  exit 1
fi


