from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from localdev_mlx.config import ModelProfile
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.schemas import (
    FileEdit,
    ImplementationResult,
    ReviewResult,
    RiskLevel,
    TriageResult,
    WorkUnit,
)

T = TypeVar("T", bound=BaseModel)


class MockStructuredProvider(StructuredProvider):
    """Deterministic provider used by tests and the offline demonstration."""

    def complete_structured(
        self,
        *,
        profile: ModelProfile,
        system_prompt: str,
        user_prompt: str,
        response_model: type[T],
        schema_name: str,
    ) -> T:
        if response_model is TriageResult:
            value = TriageResult(
                task_summary="Fix the intentionally incorrect subtraction implementation.",
                risk=RiskLevel.LOW,
                confidence=0.99,
                should_escalate=False,
                reproduction_plan=["Run the unit test suite."],
                relevant_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                work_units=[
                    WorkUnit(
                        title="Correct subtraction",
                        goal="Return a minus b and preserve existing API behavior.",
                        allowed_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                        read_paths=["src/samplecalc/core.py", "tests/test_core.py"],
                        acceptance_criteria=["subtract(7, 2) returns 5"],
                        test_focus=["subtraction unit tests"],
                    )
                ],
            )
        elif response_model is ImplementationResult:
            value = ImplementationResult(
                summary="Correct subtraction operator.",
                edits=[
                    FileEdit(
                        operation="replace_text",
                        path="src/samplecalc/core.py",
                        old_text="return a + b  # intentional demo bug",
                        new_text="return a - b",
                        reason="Subtraction must subtract the second operand.",
                    )
                ],
                tests_added_or_changed=[],
            )
        elif response_model is ReviewResult:
            value = ReviewResult(
                approved=True,
                confidence=0.99,
                summary="The change is minimal, correct, and covered by the existing tests.",
            )
        else:
            raise AssertionError(f"Mock response not defined for {response_model.__name__}")
        return response_model.model_validate(value.model_dump())
