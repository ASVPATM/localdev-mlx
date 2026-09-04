from __future__ import annotations

import pytest
from pydantic import ValidationError

from localdev_mlx.schemas import FileEdit, ImplementationResult, RiskLevel, TriageResult, WorkUnit


def test_file_edit_requires_safe_relative_path() -> None:
    with pytest.raises(ValidationError):
        FileEdit(operation="delete", path="../secret", reason="bad")


def test_replace_text_requires_both_values() -> None:
    with pytest.raises(ValidationError):
        FileEdit(operation="replace_text", path="x.py", old_text="x", reason="missing new")


def test_non_escalating_implementation_can_report_no_edits() -> None:
    result = ImplementationResult(summary="Inspection found no edit to apply")
    assert result.edits == []
    assert result.needs_escalation is False


def test_no_change_flag_cannot_be_combined_with_edits() -> None:
    with pytest.raises(ValidationError):
        ImplementationResult(
            summary="conflicting result",
            no_changes_needed=True,
            edits=[
                FileEdit(
                    operation="create",
                    path="x.py",
                    content="x = 1\n",
                    reason="test",
                )
            ],
        )


def test_work_unit_supports_read_only_analysis_mode() -> None:
    unit = WorkUnit(
        mode="analysis",
        title="Inspect current behavior",
        goal="Record findings for later edit units.",
        read_paths=["src/example.py"],
    )
    assert unit.mode == "analysis"
    assert unit.allowed_paths == []


def test_escalated_triage_requires_reason() -> None:
    unit = WorkUnit(title="x", goal="x", allowed_paths=["x.py"])
    with pytest.raises(ValidationError):
        TriageResult(
            task_summary="x",
            risk=RiskLevel.EXTERNAL,
            confidence=0.5,
            should_escalate=True,
            work_units=[unit],
        )


def test_non_escalating_triage_requires_work_units() -> None:
    with pytest.raises(ValidationError):
        TriageResult(
            task_summary="missing work",
            risk=RiskLevel.LOW,
            confidence=0.9,
            should_escalate=False,
        )


def test_escalating_triage_may_use_empty_work_units() -> None:
    triage = TriageResult(
        task_summary="needs independent review",
        risk=RiskLevel.EXTERNAL,
        confidence=0.7,
        should_escalate=True,
        escalation_reasons=["High-risk change"],
        work_units=[],
    )
    assert triage.work_units == []
