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


def test_wire_schema_requires_operation_specific_fields() -> None:
    variants = FileEdit.model_json_schema()["anyOf"]
    whole_file = next(
        v for v in variants if v["properties"]["operation"]["const"] == "replace_file"
    )
    assert "content" in whole_file["required"]
    assert "old_text" not in whole_file["properties"]
    text_edit = next(v for v in variants if v["properties"]["operation"]["const"] == "replace_text")
    assert {"old_text", "new_text"} <= set(text_edit["required"])


def test_wire_plan_requires_controller_authority_and_verification() -> None:
    schema = TriageResult.model_json_schema()
    assert {"reproduction_plan", "relevant_paths"} <= set(schema["required"])
    variants = schema["$defs"]["WorkUnit"]["anyOf"]
    for unit in variants:
        assert {"allowed_paths", "acceptance_criteria", "test_focus", "dependencies"} <= set(
            unit["required"]
        )
        assert unit["properties"]["acceptance_criteria"]["minItems"] == 1
        assert unit["properties"]["test_focus"]["minItems"] == 1
        authority = unit["properties"]["allowed_paths"]
        if unit["properties"]["mode"]["const"] == "edit":
            assert authority["minItems"] == 1
        else:
            assert authority["maxItems"] == 0


def test_bare_summary_is_not_an_implementation() -> None:
    with pytest.raises(ValidationError):
        ImplementationResult(summary="Inspection found no edit to apply")
    result = ImplementationResult(
        result_type="no_change",
        summary="Already correct",
        notes=["The targeted acceptance test covers the existing behavior."],
    )
    assert not result.edits


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
