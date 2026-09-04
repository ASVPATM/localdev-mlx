from __future__ import annotations

import hashlib
import time
from dataclasses import replace

from localdev_mlx.execution.budget import deadline
from localdev_mlx.schemas import ImplementationResult, ReviewResult, TaskKind, TriageResult
from localdev_mlx.workflows.planning import triage_plan_problems


def probe_capabilities(profile, provider, *, timeout=120):
    """Tiny real workflow schemas; report measured capability without inferring quality."""
    selected = replace(
        profile,
        enable_thinking=False,
        thinking_budget=0,
        max_tokens=1200,
        request_timeout_seconds=min(timeout, profile.request_timeout_seconds),
    )
    content = "def add(a, b):\n    return a - b\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    cases = [
        (
            "triage",
            TriageResult,
            "Plan a low-risk bug fix in calc.py: add subtracts instead of adding. "
            'One edit unit with allowed_paths=["calc.py"], read_paths=["calc.py"], acceptance_criteria=["add(2,3)==5"]. '
            'Include test_focus=["check add(2,3)"], relevant_paths=["calc.py"], reproduction_plan=["check add(2,3)"]. '
            'should_escalate=false; confidence=1; task_summary="Fix addition".',
        ),
        (
            "edit",
            ImplementationResult,
            f'Return result_type="edits", summary="Fix addition", and one replace_file edit '
            f'for calc.py with base_hash="{digest}". Current calc.py:\n{content}\n'
            "Replace minus with plus. Include content and reason in the edit.",
        ),
        (
            "review",
            ReviewResult,
            "Review actual diff: return a - b changed to return a + b in add(a,b). "
            "Controller test add(2,3)==5 passed. Return approved=true, confidence=1, summary, issues=[], should_escalate=false.",
        ),
    ]
    results = []
    for name, schema, prompt in cases:
        started = time.monotonic()
        record = {
            "capability": name,
            "model": selected.model,
            "profile": selected.name,
            "thinking": False,
            "max_tokens": selected.max_tokens,
            "timeout_seconds": selected.request_timeout_seconds,
            "prompt_chars": len(prompt),
        }
        try:
            with deadline(selected.request_timeout_seconds, label=f"{name} capability probe"):
                response = provider.complete_structured(
                    profile=selected,
                    system_prompt="Return only schema-valid JSON.",
                    user_prompt=prompt,
                    response_model=schema,
                    schema_name=f"Probe{name}",
                )
            if name == "edit" and (
                response.result_type != "edits"
                or "return a + b" not in (response.edits[0].content or "")
            ):
                raise ValueError("Schema-valid response did not implement the tiny requested edit")
            if name == "triage":
                problems = triage_plan_problems(response, task_kind=TaskKind.BUG)
                if response.should_escalate or problems:
                    raise ValueError(f"Probe did not produce a valid executable plan: {problems}")
            if name == "review" and not response.approved:
                raise ValueError("Probe did not approve the specified trivial correct diff")
            record.update(
                passed=True,
                response=response.model_dump(mode="json"),
                usage=getattr(provider, "last_metadata", {}),
            )
        except Exception as exc:
            record.update(passed=False, error=str(exc))
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        results.append(record)
    return results
