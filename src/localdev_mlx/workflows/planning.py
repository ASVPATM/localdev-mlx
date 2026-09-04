from __future__ import annotations

import re
from pathlib import Path

from localdev_mlx.git.edits import EditError, safe_target
from localdev_mlx.schemas import TaskKind, TriageResult


def requirements(description: str, baseline: str) -> dict[str, str]:
    result = {
        f"R{number}": text for number, text in re.findall(r"(?m)^\s*(\d+)[.)]\s+(.+)$", description)
    }
    for node in re.findall(r"(?m)^FAILED\s+(\S+::\S+)", baseline):
        result[node] = node
    for command, code in re.findall(r"(?m)^Command: (.+)\nExit code: (-?\d+)$", baseline):
        if int(code) != 0:
            result[command] = f"Failing baseline command: {command}"
    return result


def triage_plan_problems(
    triage: TriageResult,
    *,
    task_kind: TaskKind | None = None,
    root: Path | None = None,
    config=None,
    coverage: dict[str, str] | None = None,
) -> list[str]:
    if triage.should_escalate:
        return []
    problems: list[str] = []
    if not triage.task_summary.strip():
        problems.append("task_summary must not be blank")
    if not triage.reproduction_plan or not all(s.strip() for s in triage.reproduction_plan):
        problems.append("reproduction_plan must contain concrete verification steps")
    if not triage.relevant_paths:
        problems.append("relevant_paths must identify the affected files")
    if not triage.work_units:
        problems.append("non-escalating plan contains no work units")
    if task_kind in {TaskKind.BUG, TaskKind.FEATURE, TaskKind.TWEAK} and not any(
        unit.mode == "edit" for unit in triage.work_units
    ):
        problems.append(f"{task_kind.value} plan contains no edit work unit")
    covered = set(triage.deferred_requirements)
    for index, unit in enumerate(triage.work_units, 1):
        if not unit.title.strip() or not unit.goal.strip():
            problems.append(f"work unit {index} has a blank title or goal")
        if not unit.acceptance_criteria or not all(s.strip() for s in unit.acceptance_criteria):
            problems.append(f"work unit {index} needs concrete acceptance_criteria")
        if not unit.test_focus or not all(s.strip() for s in unit.test_focus):
            problems.append(f"work unit {index} needs explicit test_focus")
        if unit.mode == "edit" and not unit.allowed_paths:
            problems.append(f"work unit {index} has an empty allowed_paths list")
        if unit.mode == "analysis" and unit.allowed_paths:
            problems.append(f"work unit {index} is analysis-only but authorizes writes")
        if any(dep < 1 or dep >= index for dep in unit.dependencies):
            problems.append(f"work unit {index} has invalid dependencies")
        covered.update(unit.requirement_ids)
        covered.update(unit.test_focus)
        if root is not None:
            for relative in dict.fromkeys([*unit.allowed_paths, *unit.read_paths]):
                try:
                    path = safe_target(root, relative, config.deny_paths)
                    if path.is_dir():
                        raise EditError("directories/broad roots are not valid file authority")
                    if not path.exists() and relative not in unit.allowed_paths:
                        raise EditError("required read file is missing")
                    parent = path.parent
                    while not parent.exists():
                        parent = parent.parent
                    if not parent.is_dir():
                        raise EditError("parent is not a directory")
                except EditError as exc:
                    problems.append(f"work unit {index}, {relative}: {exc}")
    for key in coverage or {}:
        if key not in covered:
            problems.append(f"requested requirement not covered: {key}")
    if any(not reason.strip() for reason in triage.deferred_requirements.values()):
        problems.append("deferred requirements need a reason")
    return problems
