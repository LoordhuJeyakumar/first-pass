"""
Unit tests for document conformance and invariant check helper module (scripts/doc_conformance.py).
"""

import os
import pytest
from scripts.doc_conformance import (
    parse_roadmap_heading_states,
    check_spec_conformance,
    check_directory_claims,
)


def test_roadmap_heading_states_nested_subheadings():
    """
    Verifies that nested subheadings (more #) inside a roadmap section preserve
    the in_roadmap exemption, while same-or-shallower headings end it.
    """
    doc_lines = [
        "# Core Features\n",  # line 0: level 1, active
        "Some active prose\n",  # line 1: active
        "## Planned Architecture\n",  # line 2: level 2, roadmap keyword 'planned'
        "Line inside planned section\n",  # line 3: roadmap
        "### Subcomponent A\n",  # line 4: level 3 (deeper) -> SHOULD STAY IN ROADMAP
        "Subcomponent prose\n",  # line 5: roadmap
        "#### Sub-sub detail\n",  # line 6: level 4 (deeper) -> SHOULD STAY IN ROADMAP
        "Sub-sub detail prose\n",  # line 7: roadmap
        "## Implemented Features\n",  # line 8: level 2 (same as 2) -> ENDS ROADMAP
        "Implemented prose\n",  # line 9: active
    ]

    states = parse_roadmap_heading_states(doc_lines)

    assert states[0] is False
    assert states[1] is False
    assert states[2] is True
    assert states[3] is True
    assert states[4] is True  # Nested level 3 preserves roadmap exemption!
    assert states[5] is True
    assert states[6] is True  # Nested level 4 preserves roadmap exemption!
    assert states[7] is True
    assert states[8] is False  # Level 2 heading ends roadmap exemption!
    assert states[9] is False


def test_spec_conformance_current_codebase():
    """
    Verifies Check 15 passes against current AGENTS.md, check_engine.py, and telemetry.py.
    """
    violations = check_spec_conformance(
        agents_md_path="AGENTS.md",
        check_engine_path="agents/check_engine.py",
        telemetry_path="agents/telemetry.py",
    )
    assert violations == [], f"Expected 0 spec conformance violations, got: {violations}"


def test_directory_claims_nonexistent_detection(tmp_path):
    """
    Verifies Check 16 detects non-existent directory references in tables or tree blocks.
    """
    dummy_doc = tmp_path / "test_doc.md"
    dummy_doc.write_text(
        "```\n"
        "agents/  Active source code\n"
        "nonexistent_dir_xyz/  Fake directory\n"
        "```\n"
        "| `fake_dir_abc/` | Description |\n"
    )

    violations = check_directory_claims([str(dummy_doc)])
    assert len(violations) == 2
    assert "nonexistent_dir_xyz/" in violations[0]
    assert "fake_dir_abc/" in violations[1]
