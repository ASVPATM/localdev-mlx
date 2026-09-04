from __future__ import annotations

from localdev_mlx.agents.prompts import (
    idea_system,
    idea_user,
    implementation_system,
    implementation_user,
    plan_system,
    plan_user,
    release_system,
    release_user,
    review_system,
    review_user,
    triage_system,
    triage_user,
)
from localdev_mlx.config import ModelProfile
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.schemas import (
    ImplementationResult,
    PlanDocuments,
    ProjectDesign,
    ReleaseAuditResult,
    ReviewIssue,
    ReviewResult,
    TaskKind,
    TriageResult,
    WorkUnit,
)


class LocalAgents:
    def __init__(self, provider: StructuredProvider) -> None:
        self.provider = provider

    def triage(
        self,
        *,
        profile: ModelProfile,
        kind: TaskKind,
        description: str,
        context: str,
    ) -> TriageResult:
        return self.provider.complete_structured(
            profile=profile,
            system_prompt=triage_system(),
            user_prompt=triage_user(kind, description, context),
            response_model=TriageResult,
            schema_name="LocalDevTriage",
        )

    def implement(
        self,
        *,
        profile: ModelProfile,
        kind: TaskKind,
        description: str,
        unit: WorkUnit,
        context: str,
        previous_failures: str = "",
        review_issues: list[ReviewIssue] | None = None,
    ) -> ImplementationResult:
        return self.provider.complete_structured(
            profile=profile,
            system_prompt=implementation_system(),
            user_prompt=implementation_user(
                kind=kind,
                description=description,
                unit=unit,
                context=context,
                previous_failures=previous_failures,
                review_issues=review_issues,
            ),
            response_model=ImplementationResult,
            schema_name="LocalDevImplementation",
        )

    def review(
        self,
        *,
        profile: ModelProfile,
        kind: TaskKind,
        description: str,
        triage: TriageResult,
        diff: str,
        tests: str,
        context: str,
    ) -> ReviewResult:
        return self.provider.complete_structured(
            profile=profile,
            system_prompt=review_system(),
            user_prompt=review_user(
                kind=kind,
                description=description,
                triage=triage,
                diff=diff,
                tests=tests,
                context=context,
            ),
            response_model=ReviewResult,
            schema_name="LocalDevReview",
        )

    def ideate(
        self,
        *,
        profile: ModelProfile,
        description: str,
        context: str,
    ) -> ProjectDesign:
        return self.provider.complete_structured(
            profile=profile,
            system_prompt=idea_system(),
            user_prompt=idea_user(description, context),
            response_model=ProjectDesign,
            schema_name="LocalDevProjectDesign",
        )

    def plan(
        self,
        *,
        profile: ModelProfile,
        description: str,
        context: str,
    ) -> PlanDocuments:
        return self.provider.complete_structured(
            profile=profile,
            system_prompt=plan_system(),
            user_prompt=plan_user(description, context),
            response_model=PlanDocuments,
            schema_name="LocalDevPlanDocuments",
        )

    def release_audit(
        self,
        *,
        profile: ModelProfile,
        base_branch: str,
        diff: str,
        tests: str,
        context: str,
    ) -> ReleaseAuditResult:
        return self.provider.complete_structured(
            profile=profile,
            system_prompt=release_system(),
            user_prompt=release_user(
                base_branch=base_branch,
                diff=diff,
                tests=tests,
                context=context,
            ),
            response_model=ReleaseAuditResult,
            schema_name="LocalDevReleaseAudit",
        )
