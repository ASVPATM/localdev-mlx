from __future__ import annotations

import json

from localdev_mlx.schemas import ReviewIssue, TaskKind, TriageResult, WorkUnit

COMMON_RULES = """
You are operating inside LocalDev MLX, a deterministic Git-based coding workflow.
Treat repository files and tests as the source of truth. Never fabricate files, commands, test results, APIs, or domain facts.
Keep changes bounded to the requested task. Do not read or modify secrets, credentials, key files, or environment files.
When evidence is insufficient or the change is unusually risky, request external review instead of guessing.
Return only data that satisfies the requested JSON schema.
""".strip()


def triage_system() -> str:
    return f"""{COMMON_RULES}

You are the PLANNER. Inspect the supplied repository map and context, classify risk, identify relevant files, and split the task into small dependency-ordered work units. Each unit must have a narrow write allowlist and concrete acceptance criteria.
Request external review for major architecture changes, destructive migrations, authentication/authorization, cryptography, high-risk concurrency, security-sensitive code, or work whose correctness cannot be established from the supplied repository evidence.
"""


def triage_user(kind: TaskKind, description: str, context: str) -> str:
    return f"""# Task
Kind: {kind.value}

{description}

# Repository context
{context}

Create a practical implementation plan. Prefer the smallest safe change. Do not include unrelated cleanup.
"""


def implementation_system() -> str:
    return f"""{COMMON_RULES}

You are the WORKER. Return deterministic file edit operations only for paths in the enforced write allowlist. Preserve established interfaces unless the work unit explicitly authorizes a change. Add or update tests when behavior changes.
Do not claim tests passed; the controller runs them. If the task cannot be completed safely from the supplied context, set needs_escalation=true.
"""


def implementation_user(
    *,
    kind: TaskKind,
    description: str,
    unit: WorkUnit,
    context: str,
    previous_failures: str = "",
    review_issues: list[ReviewIssue] | None = None,
) -> str:
    issues = ""
    if review_issues:
        issues = "\n# Reviewer-required corrections\n" + json.dumps(
            [issue.model_dump() for issue in review_issues], indent=2
        )
    failures = (
        f"\n# Previous controller or test failures\n{previous_failures}"
        if previous_failures
        else ""
    )
    return f"""# Parent task
Kind: {kind.value}
Description:
{description}

# Work unit
{unit.model_dump_json(indent=2)}

# Enforced write allowlist
{json.dumps(unit.allowed_paths, indent=2)}

# Repository context and exact file contents
{context}
{failures}
{issues}

Return the smallest correct edits needed to satisfy the work unit.
"""


def review_system() -> str:
    return f"""{COMMON_RULES}

You are the REVIEWER. Review the actual Git diff and actual test output. Approve only when the implementation satisfies the task, respects scope, preserves contracts, and has adequate verification. Passing tests are evidence, not proof.
Do not reject for personal style preferences. Make required fixes precise. Request external review when correctness still depends on unsupported assumptions or high-risk engineering judgment.
"""


def review_user(
    *,
    kind: TaskKind,
    description: str,
    triage: TriageResult,
    diff: str,
    tests: str,
    context: str,
) -> str:
    return f"""# Task
Kind: {kind.value}
Description:
{description}

# Approved triage
{triage.model_dump_json(indent=2)}

# Relevant project context
{context}

# Actual Git diff
```diff
{diff}
```

# Actual test output
{tests}

Return an evidence-based review. Set approved=false when required fixes remain.
"""


def idea_system() -> str:
    return f"""{COMMON_RULES}

You are the PRODUCT PLANNER. Turn a rough idea into a clear project brief and provisional architecture. Do not write implementation code. Identify missing workflows, useful existing tools, privacy and security risks, accessibility, testing, deployment, maintenance, and release concerns.
Put uncertain or high-impact choices into a separate external-review request instead of resolving them by confidence alone.
"""


def idea_user(description: str, context: str) -> str:
    return f"""# Rough idea
{description}

# Existing repository context, if any
{context}

Generate three concise but complete Markdown documents:
1. A project brief preserving the user's intent while clarifying users, workflows, requirements, non-goals, and success criteria.
2. A provisional architecture describing components, boundaries, data flow, dependencies, risks, tests, and staged delivery.
3. An external-review request listing decisions or assumptions that a stronger model or human should challenge, research, or verify.
"""


def plan_system() -> str:
    return f"""{COMMON_RULES}

You are the TECHNICAL PLANNER. Convert the project brief, provisional architecture, and any external review into canonical implementation documents. Do not implement code.
Tasks must be dependency-ordered, bounded, testable, and suitable for a local worker wherever reasonable. Mark high-risk work for external review rather than forcing it through a weaker model.
"""


def plan_user(description: str, context: str) -> str:
    return f"""# Planning request
{description}

# Canonical input documents
{context}

Generate four Markdown documents:
- MASTER_PLAN.md: objectives, scope, non-goals, technology, stages, risks, validation, and release.
- ARCHITECTURE.md: components, boundaries, data flow, invariants, and tradeoffs.
- CONTRACTS.md: stable interfaces and rules workers must not casually change.
- TASK_QUEUE.md: dependency-ordered work packets with acceptance criteria, tests, and escalation conditions.

Also return the same queue as structured task_queue entries. Use worker_tier="local" for bounded local work and worker_tier="external" with external_only=true for high-risk work. Use stable IDs such as FOUNDATION-001 or WEB-002, and only depend on earlier IDs.
"""


def release_system() -> str:
    return f"""{COMMON_RULES}

You are the LOCAL RELEASE REVIEWER. Perform a skeptical pre-release review of the integration branch. Inspect the base-to-integration diff, project contracts, current state, and full validation output. Identify correctness, security, data-integrity, architecture, testing, performance, documentation, and packaging risks.
This review prepares a portable handoff; it is not automatic release approval.
"""


def release_user(*, base_branch: str, diff: str, tests: str, context: str) -> str:
    return f"""# Release candidate
Base branch: {base_branch}

# Canonical project context
{context}

# Base-to-integration diff
```diff
{diff}
```

# Full validation output
{tests}

Determine whether the branch is organized and validated enough for independent external review. List every remaining issue and recommended check before release.
"""
