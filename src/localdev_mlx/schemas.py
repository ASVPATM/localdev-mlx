from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


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
    CANCELLED = "cancelled"
    PLANNED = "planned"


class TaskPhase(StrEnum):
    CREATED = "created"
    PREFLIGHT = "preflight"
    BASELINE = "baseline"
    PLANNING = "planning"
    PLAN_VALIDATION = "plan_validation"
    IMPLEMENTING = "implementing"
    VALIDATING = "validating"
    REVIEWING = "reviewing"
    REPAIRING = "repairing"
    COMMITTING = "committing"
    INTEGRATING = "integrating"
    COMPLETED = "completed"
    STOPPED = "stopped"


PHASE_SUCCESSORS = {
    TaskPhase.CREATED: {TaskPhase.PREFLIGHT},
    TaskPhase.PREFLIGHT: {TaskPhase.BASELINE},
    TaskPhase.BASELINE: {TaskPhase.VALIDATING},
    TaskPhase.PLANNING: {TaskPhase.PLAN_VALIDATION},
    TaskPhase.PLAN_VALIDATION: {TaskPhase.IMPLEMENTING, TaskPhase.COMPLETED},
    TaskPhase.IMPLEMENTING: {TaskPhase.IMPLEMENTING, TaskPhase.VALIDATING},
    TaskPhase.VALIDATING: {
        TaskPhase.VALIDATING,
        TaskPhase.PLANNING,
        TaskPhase.PLAN_VALIDATION,
        TaskPhase.IMPLEMENTING,
        TaskPhase.REPAIRING,
        TaskPhase.REVIEWING,
    },
    TaskPhase.REVIEWING: {TaskPhase.REPAIRING, TaskPhase.COMMITTING},
    TaskPhase.REPAIRING: {TaskPhase.REPAIRING, TaskPhase.VALIDATING},
    TaskPhase.COMMITTING: {TaskPhase.INTEGRATING, TaskPhase.COMPLETED},
    TaskPhase.INTEGRATING: {TaskPhase.COMPLETED},
    TaskPhase.COMPLETED: set(),
    TaskPhase.STOPPED: set(),
}


class LocalOutcome(StrEnum):
    INTEGRATED = "integrated"
    APPROVED_NOT_INTEGRATED = "approved_not_integrated"
    DEFERRED = "deferred"
    ESCALATED = "escalated"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PLANNED = "planned"


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
    CONFIGURATION_ERROR = "configuration_error"
    PREFLIGHT_FAILURE = "preflight_failure"


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
    requirement_ids: list[str] = Field(default_factory=list, max_length=60)

    @field_validator("allowed_paths", "read_paths")
    @classmethod
    def validate_relative_paths(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            value = raw.strip().replace("\\", "/")
            path = Path(value)
            if (
                not value
                or value == "."
                or path.is_absolute()
                or ".." in path.parts
                or ":" in value
                or "\x00" in value
            ):
                raise ValueError(f"Path must be repository-relative and safe: {raw!r}")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned

    @model_validator(mode="after")
    def validate_authority(self) -> WorkUnit:
        if self.mode == "edit" and not self.allowed_paths:
            raise ValueError("Edit work unit requires non-empty allowed_paths")
        if self.mode == "analysis" and self.allowed_paths:
            raise ValueError("Analysis work unit cannot authorize writes")
        return self


class TriageResult(StrictModel):
    task_summary: str = Field(min_length=1, max_length=3000)
    risk: RiskLevel
    confidence: float = Field(ge=0, le=1)
    should_escalate: bool
    escalation_reasons: list[str] = Field(default_factory=list, max_length=20)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    reproduction_plan: list[str] = Field(default_factory=list, max_length=30)
    relevant_paths: list[str] = Field(default_factory=list, max_length=60)
    work_units: list[WorkUnit] = Field(max_length=12)
    recommended_test_profile: Literal["quick", "full"] = "quick"
    external_questions: list[str] = Field(default_factory=list, max_length=30)
    deferred_requirements: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def escalation_consistency(self) -> TriageResult:
        if self.should_escalate and not self.escalation_reasons:
            raise ValueError("Escalated triage must include at least one escalation reason")
        if not self.should_escalate and not self.work_units:
            raise ValueError("Non-escalating triage must include at least one work unit")
        return self


class FileEdit(StrictModel):
    operation: Literal["create", "replace_file", "replace_text", "delete"]
    path: str = Field(min_length=1, max_length=500)
    content: str | None = None
    old_text: str | None = None
    new_text: str | None = None
    expected_replacements: int = Field(default=1, ge=1, le=100)
    reason: str = Field(min_length=1, max_length=1000)
    base_hash: str | None = Field(default=None, pattern=r"^(?:[0-9a-f]{64}|missing)$")

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        # The model server sees operation-specific required fields, not a bag of
        # optional content/old_text/new_text fields. Python keeps the stable API.
        schema = handler(core_schema)
        properties = schema["properties"]
        variants = []
        for operation, required in (
            ("create", ["content"]),
            ("replace_file", ["content"]),
            ("replace_text", ["old_text", "new_text"]),
            ("delete", []),
        ):
            fields = {key: properties[key] for key in ("path", "reason", "base_hash")}
            fields["operation"] = {"type": "string", "const": operation}
            fields.update({key: {"type": "string"} for key in required})
            if operation == "replace_text":
                fields["expected_replacements"] = properties["expected_replacements"]
            variants.append(
                {
                    "type": "object",
                    "properties": fields,
                    "required": ["operation", "path", "reason", *required],
                    "additionalProperties": False,
                }
            )
        return {"title": "FileEdit", "anyOf": variants}

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/")
        path = Path(normalized)
        if (
            not normalized
            or normalized == "."
            or path.is_absolute()
            or ".." in path.parts
            or ":" in normalized
            or "\x00" in normalized
        ):
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
    result_type: Literal["edits", "no_change", "analysis", "escalate"]
    summary: str = Field(min_length=1, max_length=3000)
    edits: list[FileEdit] = Field(default_factory=list, max_length=30)
    tests_added_or_changed: list[str] = Field(default_factory=list, max_length=30)
    notes: list[str] = Field(default_factory=list, max_length=30)
    escalation_reason: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_result(cls, value: object) -> object:
        # Accept concrete v0.2 responses, never infer success from a bare summary.
        if isinstance(value, dict):
            value = dict(value)
            no_change = value.pop("no_changes_needed", False)
            escalate = value.pop("needs_escalation", False)
            if no_change and escalate:
                raise ValueError("Contradictory no-change and escalation response")
            if "result_type" not in value:
                if escalate:
                    value["result_type"] = "escalate"
                elif no_change:
                    value["result_type"] = "no_change"
                elif value.get("edits"):
                    value["result_type"] = "edits"
            elif (escalate and value["result_type"] != "escalate") or (
                no_change and value["result_type"] != "no_change"
            ):
                raise ValueError("Contradictory legacy result flags")
        return value

    @property
    def needs_escalation(self) -> bool:
        return self.result_type == "escalate"

    @property
    def no_changes_needed(self) -> bool:
        return self.result_type == "no_change"

    @model_validator(mode="after")
    def escalation_consistency(self) -> ImplementationResult:
        if self.needs_escalation and not self.escalation_reason:
            raise ValueError("needs_escalation requires escalation_reason")
        if self.result_type == "edits" and not self.edits:
            raise ValueError("edits result requires at least one edit")
        if self.result_type != "edits" and self.edits:
            raise ValueError("Only edits results may contain file edits")
        if self.result_type in {"no_change", "analysis"} and not self.notes:
            raise ValueError("no_change/analysis requires evidence in notes")
        if not self.needs_escalation and self.escalation_reason:
            raise ValueError("Only escalate results may contain an escalation reason")
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
    failure_kind: Literal["spawn", "timeout", "collection", "assertion", "exit"] | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class TestRunResult(StrictModel):
    profile: str
    commands: list[TestCommandResult]

    @property
    def passed(self) -> bool:
        return bool(self.commands) and all(command.passed for command in self.commands)

    @property
    def environment_failed(self) -> bool:
        return any(command.failure_kind in {"spawn", "collection"} for command in self.commands)


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


class PhaseTransition(StrictModel):
    timestamp: datetime = Field(default_factory=utc_now)
    previous: TaskPhase
    phase: TaskPhase
    reason: str
    model: str | None = None
    attempt: int = 0
    input_hash: str | None = None
    output_hash: str | None = None
    base_commit: str | None = None
    worktree_commit: str | None = None
    diff_hash: str | None = None


class AttemptRecord(StrictModel):
    phase: TaskPhase
    work_unit: str | None = None
    model: str
    profile: str
    started_at: datetime = Field(default_factory=utc_now)
    ended_at: datetime | None = None
    prompt_chars: int
    prompt_hash: str
    context_manifest_path: str | None = None
    response_path: str | None = None
    response_schema_valid: bool = False
    response_digest: str | None = None
    edit_digest: str | None = None
    files_changed: list[str] = Field(default_factory=list)
    test_result_path: str | None = None
    outcome: str = "running"
    failure_category: str | None = None
    thinking_budget: int = 0
    max_tokens: int = 0
    timeout_seconds: float = 0
    elapsed_seconds: float = 0
    usage: dict = Field(default_factory=dict)


class TaskRecord(StrictModel):
    model_config = ConfigDict(extra="allow", validate_default=True)
    schema_version: int = 2
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
    phase: TaskPhase = TaskPhase.CREATED
    local_outcome: LocalOutcome | None = None
    failure_category: ExternalReviewCategory | None = None
    phase_history: list[PhaseTransition] = Field(default_factory=list)
    attempt_records: list[AttemptRecord] = Field(default_factory=list)
    legacy_triage: dict | None = None
    controller_pid: int | None = None

    @model_validator(mode="before")
    @classmethod
    def read_legacy_task(cls, value: object) -> object:
        if isinstance(value, dict) and value.get("schema_version", 1) < 2:
            value = dict(value)
            if value.get("triage"):
                try:
                    TriageResult.model_validate(value["triage"])
                except ValueError:
                    value["legacy_triage"] = value.pop("triage")
            if value.get("status") in {"escalated", "deferred"}:
                value.setdefault("external_review_state", "pending")
            if "phase" not in value:
                old_status = value.get("status", "created")
                value["phase"] = {
                    "integrated": "completed",
                    "approved": "completed",
                    "escalated": "stopped",
                    "deferred": "stopped",
                    "failed": "stopped",
                    "triaging": "planning",
                    "testing": "validating",
                }.get(old_status, old_status)
        return value

    @model_validator(mode="after")
    def derive_legacy_external_state(self) -> TaskRecord:
        # Older task records only stored status=escalated and escalation_path.
        outcomes = {
            "approved": "approved_not_integrated",
            "integrated": "integrated",
            "failed": "failed",
            "cancelled": "cancelled",
            "deferred": "deferred",
            "escalated": "escalated",
            "planned": "planned",
        }
        if self.local_outcome is None and self.status.value in outcomes:
            self.local_outcome = LocalOutcome(outcomes[self.status.value])
        return self

    def advance(self, phase: TaskPhase, reason: str, **evidence: object) -> None:
        if self.phase in {TaskPhase.COMPLETED, TaskPhase.STOPPED} and phase != self.phase:
            raise ValueError("Cannot restart a terminal task; create a new task")
        if phase != TaskPhase.STOPPED and phase not in PHASE_SUCCESSORS[self.phase]:
            raise ValueError(f"Invalid task phase transition: {self.phase} -> {phase}")
        self.phase_history.append(
            PhaseTransition(
                previous=self.phase,
                phase=phase,
                reason=reason,
                base_commit=self.base_commit,
                **evidence,
            )
        )
        self.phase = phase
        self.updated_at = utc_now()
        self.events.append(
            f"{self.updated_at.isoformat(timespec='seconds')} {phase.value}: {reason}"
        )

    def transition(self, status: TaskStatus, message: str | None = None) -> None:
        self.status = status
        self.updated_at = utc_now()
        self.local_outcome = None
        self.derive_legacy_external_state()
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
    model_config = ConfigDict(extra="allow", validate_default=True)
    schema_version: int = 2
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
