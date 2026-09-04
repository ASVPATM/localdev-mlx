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


def test_non_escalating_implementation_requires_edits() -> None:
    with pytest.raises(ValidationError):
        ImplementationResult(summary="nothing")


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
