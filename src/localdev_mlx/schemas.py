from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskKind(StrEnum):
    IDEA = "idea"
    BUG = "bug"
    FEATURE = "feature"
    TWEAK = "tweak"
    AUDIT = "audit"
    RELEASE = "release"


class TaskStatus(StrEnum):
    CREATED = "created"
    TRIAGING = "triaging"
    DEFERRED = "deferred"
    ESCALATED = "escalated"
    IMPLEMENTING = "implementing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    APPROVED = "approved"
    INTEGRATED = "integrated"
    FAILED = "failed"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTERNAL = "external"


class ExternalReviewState(StrEnum):
    NONE = "none"
    PENDING = "pending"
    BUNDLED = "bundled"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class ExternalReviewCategory(StrEnum):
    USER_DEFERRED = "user_deferred"
    PLANNER_RISK = "planner_risk"
    PLANNER_INVALID = "planner_invalid"
    WORKER_LIMIT = "worker_limit"
    VALIDATION_FAILURE = "validation_failure"
    REVIEW_REJECTED = "review_rejected"
    QUEUE_EXTERNAL = "queue_external"
    WORKFLOW_ERROR = "workflow_error"


class DeferredIssue(StrictModel):
    """One intentionally deferred issue loaded from a JSON backlog file."""

    kind: Literal["bug", "feature", "tweak", "audit"]
    description: str = Field(min_length=1, max_length=10000)
    reason: str | None = Field(default=None, max_length=3000)


class FrontierBatchStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class WorkUnit(StrictModel):
    mode: Literal["edit", "analysis"] = "edit"
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=3000)
    allowed_paths: list[str] = Field(default_factory=list, max_length=30)
    read_paths: list[str] = Field(default_factory=list, max_length=40)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=30)
    test_focus: list[str] = Field(default_factory=list, max_length=20)
    dependencies: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("allowed_paths", "read_paths")
    @classmethod
    def validate_relative_paths(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            value = raw.strip().replace("\\", "/")
            path = Path(value)
            if not value or path.is_absolute() or ".." in path.parts:
                raise ValueError(f"Path must be repository-relative and safe: {raw!r}")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned


class TriageResult(StrictModel):
    task_summary: str = Field(min_length=1, max_length=3000)
    risk: RiskLevel
    confidence: float = Field(ge=0, le=1)
    should_escalate: bool
    escalation_reasons: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    reproduction_plan: list[str] = Field(default_factory=list, max_length=30)
    relevant_paths: list[str] = Field(default_factory=list, max_length=60)
    work_units: list[WorkUnit] = Field(default_factory=list, min_length=1, max_length=12)
    recommended_test_profile: Literal["quick", "full"] = "quick"
    external_questions: list[str] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def escalation_consistency(self) -> TriageResult:
        if self.should_escalate and not self.escalation_reasons:
            raise ValueError("Escalated triage must include at least one escalation reason")
        return self


class FileEdit(StrictModel):
    operation: Literal["create", "replace_file", "replace_text", "delete"]
    path: str = Field(min_length=1, max_length=500)
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    expected_replacements: int = Field(default=1, ge=1, le=100)
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Edit path must be repository-relative")
        return normalized

    @model_validator(mode="after")
    def validate_operation_fields(self) -> FileEdit:
        if self.operation in {"create", "replace_file"} and self.content is None:
            raise ValueError(f"{self.operation} requires content")
        if self.operation == "replace_text":
            if self.old_text is None or self.new_text is None:
                raise ValueError("replace_text requires old_text and new_text")
            if not self.old_text:
                raise ValueError("replace_text old_text cannot be empty")
        return self


class ImplementationResult(StrictModel):
    summary: str = Field(min_length=1, max_length=3000)
    edits: list[FileEdit] = Field(default_factory=list, max_length=30)
    tests_added_or_changed: list[str] = Field(default_factory=list, max_length=30)
    notes: list[str] = Field(default_factory=list, max_length=30)
    no_changes_needed: bool = False
    needs_escalation: bool = False
    escalation_reason: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def escalation_consistency(self) -> ImplementationResult:
        if self.needs_escalation and not self.escalation_reason:
            raise ValueError("needs_escalation requires escalation_reason")
        if self.needs_escalation and self.no_changes_needed:
            raise ValueError("An implementation cannot request escalation and no-change completion")
        if self.no_changes_needed and self.edits:
            raise ValueError("no_changes_needed cannot be combined with file edits")
        return self


class ReviewIssue(StrictModel):
    severity: Literal["info", "low", "medium", "high", "critical"]
    path: str | None = Field(default=None, max_length=500)
    description: str = Field(min_length=1, max_length=2000)
    required_fix: str = Field(min_length=1, max_length=2000)


class ReviewResult(StrictModel):
    approved: bool
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=3000)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=40)
    should_escalate: bool = False
    escalation_reasons: list[str] = Field(default_factory=list, max_length=30)
    additional_test_requests: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def validate_review(self) -> ReviewResult:
        if self.approved and any(i.severity in {"high", "critical"} for i in self.issues):
            raise ValueError("Approved review cannot include high or critical issues")
        if self.should_escalate and not self.escalation_reasons:
            raise ValueError("Escalated review requires reasons")
        return self


class TestCommandResult(StrictModel):
    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class TestRunResult(StrictModel):
    profile: str
    commands: list[TestCommandResult]

    @property
    def passed(self) -> bool:
        return bool(self.commands) and all(command.passed for command in self.commands)


class ProjectDesign(StrictModel):
    project_brief_markdown: str = Field(min_length=1)
    architecture_proposal_markdown: str = Field(min_length=1)
    external_review_request_markdown: str = Field(min_length=1)
    recommended_next_actions: list[str] = Field(default_factory=list, max_length=30)


class PlannedTaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ESCALATED = "escalated"
    BLOCKED = "blocked"


class PlannedTask(StrictModel):
    id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]*-[0-9]{3,}$")
    title: str = Field(min_length=1, max_length=200)
    kind: Literal["bug", "feature", "tweak", "audit"]
    description: str = Field(min_length=1, max_length=5000)
    worker_tier: Literal["local", "external"] = "local"
    dependencies: list[str] = Field(default_factory=list, max_length=30)
    acceptance_summary: list[str] = Field(default_factory=list, max_length=30)
    external_only: bool = False
    status: PlannedTaskStatus = PlannedTaskStatus.PENDING
    localdev_task_id: str | None = None


class MachineTaskQueue(StrictModel):
    version: int = 1
    tasks: list[PlannedTask] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_queue(self) -> MachineTaskQueue:
        ids = [item.id for item in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("Task IDs must be unique")
        known: set[str] = set()
        for item in self.tasks:
            unknown = [dependency for dependency in item.dependencies if dependency not in known]
            if unknown:
                raise ValueError(
                    f"Task {item.id} depends on missing or later tasks: {', '.join(unknown)}"
                )
            known.add(item.id)
        return self


class PlanDocuments(StrictModel):
    master_plan_markdown: str = Field(min_length=1)
    architecture_markdown: str = Field(min_length=1)
    contracts_markdown: str = Field(min_length=1)
    task_queue_markdown: str = Field(min_length=1)
    task_queue: list[PlannedTask] = Field(default_factory=list, max_length=500)


class TaskRecord(StrictModel):
    id: str
    kind: TaskKind
    description: str
    repository: str
    status: TaskStatus = TaskStatus.CREATED
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    worker: str = "local"
    triage: TriageResult | None = None
    attempts: int = 0
    base_commit: str | None = None
    task_branch: str | None = None
    task_worktree: str | None = None
    integration_branch: str | None = None
    final_commit: str | None = None
    escalation_path: str | None = None
    visible_review_path: str | None = None
    external_review_state: ExternalReviewState = ExternalReviewState.NONE
    external_review_category: ExternalReviewCategory | None = None
    external_review_reason: str | None = None
    frontier_batch_ids: list[str] = Field(default_factory=list)
    resolved_at: datetime | None = None
    resolved_commit: str | None = None
    resolution_note: str | None = None
    events: list[str] = Field(default_factory=list)
    timings_seconds: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_legacy_external_state(self) -> TaskRecord:
        # Older task records only stored status=escalated and escalation_path.
        if (
            self.status in {TaskStatus.ESCALATED, TaskStatus.DEFERRED}
            and self.external_review_state == ExternalReviewState.NONE
        ):
            self.external_review_state = ExternalReviewState.PENDING
        return self

    def transition(self, status: TaskStatus, message: str | None = None) -> None:
        self.status = status
        self.updated_at = utc_now()
        if message:
            timestamp = self.updated_at.isoformat(timespec="seconds")
            self.events.append(f"{timestamp} {status.value}: {message}")

    def mark_external_review(
        self,
        *,
        category: ExternalReviewCategory,
        reason: str,
        state: ExternalReviewState = ExternalReviewState.PENDING,
    ) -> None:
        self.external_review_state = state
        self.external_review_category = category
        self.external_review_reason = reason
        self.updated_at = utc_now()

    def mark_resolved(self, *, commit: str, note: str | None = None) -> None:
        self.external_review_state = ExternalReviewState.RESOLVED
        self.resolved_at = utc_now()
        self.resolved_commit = commit
        self.resolution_note = note
        self.updated_at = self.resolved_at

    def mark_superseded(
        self,
        *,
        commit: str | None = None,
        note: str | None = None,
    ) -> None:
        """Close an external item because later work replaced its attempted path."""
        self.external_review_state = ExternalReviewState.SUPERSEDED
        self.resolved_at = utc_now()
        self.resolved_commit = commit
        self.resolution_note = note
        self.updated_at = self.resolved_at


class FrontierBatchRecord(StrictModel):
    id: str
    repository: str
    status: FrontierBatchStatus = FrontierBatchStatus.OPEN
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    base_branch: str
    integration_branch: str
    integration_commit: str
    task_ids: list[str] = Field(default_factory=list)
    include_integrated: bool = True
    path: str
    visible_path: str | None = None
    resolved_at: datetime | None = None
    resolved_commit: str | None = None

    def mark_resolved(self, commit: str) -> None:
        self.status = FrontierBatchStatus.RESOLVED
        self.resolved_at = utc_now()
        self.resolved_commit = commit
        self.updated_at = self.resolved_at


class ReleaseFinding(StrictModel):
    severity: Literal["info", "low", "medium", "high", "critical"]
    category: Literal[
        "correctness",
        "security",
        "data_integrity",
        "architecture",
        "testing",
        "performance",
        "documentation",
        "release",
    ]
    path: str | None = Field(default=None, max_length=500)
    description: str = Field(min_length=1, max_length=3000)
    recommendation: str = Field(min_length=1, max_length=3000)


class ReleaseAuditResult(StrictModel):
    ready_for_external_review: bool
    locally_release_ready: bool
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=5000)
    findings: list[ReleaseFinding] = Field(default_factory=list, max_length=80)
    required_external_work: list[str] = Field(default_factory=list, max_length=40)
    recommended_external_checks: list[str] = Field(default_factory=list, max_length=40)
