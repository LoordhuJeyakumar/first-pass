"""
First Pass — Document Conformance & Invariant Helpers

Utilities for parsing markdown documentation structure, tracking roadmap heading exemptions,
verifying spec conformance (Check 15), and checking directory claims (Check 16).
"""

import os
import re
import glob
from typing import List, Dict, Tuple, Optional, Set

ROADMAP_KEYWORDS = ["planned", "roadmap", "future", "vision", "target", "next", "proposed", "upcoming"]


def is_heading_roadmap(heading_text: str) -> bool:
    """Returns True if heading text contains any roadmap keyword."""
    heading_low = heading_text.lower()
    return any(kw in heading_low for kw in ["planned", "roadmap", "future"])


def get_heading_info(line: str) -> Optional[Tuple[int, str]]:
    """
    If line is a Markdown heading, returns (heading_level, heading_text).
    Otherwise returns None.
    """
    stripped = line.strip()
    if stripped.startswith("#"):
        level = len(stripped) - len(stripped.lstrip("#"))
        text = stripped.lstrip("#").strip()
        return (level, text)
    return None


def parse_roadmap_heading_states(lines: List[str]) -> List[bool]:
    """
    Computes line-by-line in_roadmap state for a list of document lines.
    A heading containing roadmap keywords sets in_roadmap=True at its heading level.
    A deeper heading (more #) inside a roadmap section preserves in_roadmap=True.
    A same-or-shallower heading (equal or fewer #) ends the roadmap section.
    """
    in_roadmap_states = []
    roadmap_depth: Optional[int] = None
    in_roadmap = False

    for line in lines:
        heading_info = get_heading_info(line)
        if heading_info is not None:
            level, text = heading_info
            if is_heading_roadmap(text):
                roadmap_depth = level
                in_roadmap = True
            else:
                if roadmap_depth is not None and level > roadmap_depth:
                    # Deeper subheading within roadmap section — preserve exemption
                    in_roadmap = True
                else:
                    # Same or shallower level heading — end roadmap section
                    roadmap_depth = None
                    in_roadmap = False

        in_roadmap_states.append(in_roadmap)

    return in_roadmap_states


def check_spec_conformance(
    agents_md_path: str = "AGENTS.md",
    check_engine_path: str = "agents/check_engine.py",
    telemetry_path: str = "agents/telemetry.py",
) -> List[str]:
    """
    Check 15: Verifies code implementation against spec constraints named in AGENTS.md.
    i. Asserts evaluator functions exist in agents/check_engine.py for constraint 5 domains.
    ii. Asserts metric names in constraint 6 appear in ALLOWED_LABEL_SETS in agents/telemetry.py and are emitted.
    """
    violations = []
    if not os.path.exists(agents_md_path):
        return [f"Spec conformance check error: {agents_md_path} missing"]

    with open(agents_md_path, "r", encoding="utf-8") as f:
        agents_content = f.read()

    # i. Check 5 domains vs check_engine.py evaluators
    if not os.path.exists(check_engine_path):
        violations.append(f"Spec conformance error: {check_engine_path} missing")
    else:
        with open(check_engine_path, "r", encoding="utf-8") as f:
            engine_content = f.read()

        # Constraint 5 domains mapping to required evaluator functions
        required_evaluators = {
            "audio loudness": "evaluate_audio_loudness",
            "True Peak": "evaluate_audio_true_peak",
            "HDR metadata": "evaluate_video_color_primaries",
            "subtitle coverage": "evaluate_timed_text_coverage",
            "packaging checks": "evaluate_packaging_naming",
        }

        for domain_name, func_name in required_evaluators.items():
            pattern = rf"def\s+{func_name}\s*\("
            if not re.search(pattern, engine_content):
                violations.append(
                    f"AGENTS.md Constraint 5 specifies domain '{domain_name}' but evaluator function '{func_name}' is missing in {check_engine_path}"
                )

    # ii. Constraint 6 metrics vs telemetry.py ALLOWED_LABEL_SETS & emission
    if not os.path.exists(telemetry_path):
        violations.append(f"Spec conformance error: {telemetry_path} missing")
    else:
        with open(telemetry_path, "r", encoding="utf-8") as f:
            telemetry_content = f.read()

        # Extract metric names from AGENTS.md Constraint 6
        c6_match = re.search(r"6\.\s+\*\*Telemetry.*?\n", agents_content)
        if c6_match:
            c6_text = c6_match.group(0)
            metrics_found = re.findall(r"\b(qc_[a-z0-9_]+)", c6_text)
        else:
            metrics_found = ["qc_checks", "qc_loudness_deviation_lufs", "qc_blockers_current", "qc_readiness_ratio"]

        for metric in set(metrics_found):
            # Check ALLOWED_LABEL_SETS
            if f'"{metric}"' not in telemetry_content and f"'{metric}'" not in telemetry_content:
                violations.append(
                    f"AGENTS.md Constraint 6 specifies metric '{metric}' but it is missing from ALLOWED_LABEL_SETS in {telemetry_path}"
                )
            # Check emission in telemetry logic
            if metric not in telemetry_content:
                violations.append(
                    f"AGENTS.md Constraint 6 specifies metric '{metric}' but it is not emitted in {telemetry_path}"
                )

    return violations


def check_directory_claims(doc_paths: List[str]) -> List[str]:
    """
    Check 16: Every top-level directory named in a TREE BLOCK or COMPONENT TABLE
    across README.md, AGENTS.md, and docs/*.md must exist on disk unless the line or section
    is marked planned/roadmap/future.
    """
    violations = []
    top_dir_pattern = re.compile(r"(?:^|[`\s|])([a-zA-Z0-9_-]+)/(?=\s|`|$|[\s|,])")

    for doc in doc_paths:
        if not os.path.exists(doc):
            continue
        with open(doc, "r", encoding="utf-8") as f:
            lines = f.readlines()

        roadmap_states = parse_roadmap_heading_states(lines)
        in_fenced_block = False

        for line_num, (line, line_in_roadmap) in enumerate(zip(lines, roadmap_states), 1):
            stripped = line.strip()

            if stripped.startswith("```"):
                in_fenced_block = not in_fenced_block

            # Only check lines inside fenced code blocks or table rows
            is_table_row = "|" in line
            if not (in_fenced_block or is_table_row):
                continue

            # Check if line itself has planned/future keywords
            line_low = line.lower()
            if line_in_roadmap or any(kw in line_low for kw in ROADMAP_KEYWORDS):
                continue

            matches = top_dir_pattern.findall(line)
            for d in matches:
                # Ignore non-directory paths or relative indicators
                if d in (".", "..", "http", "https"):
                    continue
                # If directory does not exist on disk
                if not os.path.exists(d) or not os.path.isdir(d):
                    violations.append(
                        f"{doc}:{line_num} references directory '{d}/' which does not exist on disk and is not marked planned/roadmap/future"
                    )

    return violations
