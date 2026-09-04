from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from localdev_mlx.agents import LocalAgents
from localdev_mlx.config import GlobalConfig, load_project_config, project_state_dir
from localdev_mlx.context import build_context
from localdev_mlx.execution import run_tests
from localdev_mlx.git import GitRepository
from localdev_mlx.models import ModelManager
from localdev_mlx.progress import ProgressReporter
from localdev_mlx.providers.base import StructuredProvider
from localdev_mlx.tasks import TaskStore
from localdev_mlx.workflows.task_runner import render_tests


class ReleaseWorkflow:
    """Create a portable pre-release review bundle."""

    def __init__(
        self,
        *,
        global_config: GlobalConfig,
        provider: StructuredProvider,
        model_manager: ModelManager | None = None,
        manage_models: bool = True,
        progress: ProgressReporter | None = None,
    ) -> None:
        self.global_config = global_config
        self.agents = LocalAgents(provider)
        self.model_manager = model_manager or ModelManager()
        self.manage_models = manage_models
        self.progress = progress or ProgressReporter()

    def run(self, repository: Path) -> Path:
        git = GitRepository(repository)
        config = load_project_config(git.root)
        integration = git.ensure_integration_worktree(config)
        if not git.is_clean(integration):
            raise RuntimeError("Integration branch must be clean before release review")

        task_store = TaskStore(git.root)
        open_external = task_store.open_external()
        self.progress.emit("[RELEASE] START — preparing local release review")
        with self.progress.operation("RELEASE", "Full configured validation"):
            tests = run_tests(integration, config.tests, "full")
        test_text = render_tests(tests)
        diff = git.diff_between(integration, config.base_branch, "HEAD")
        context = build_context(
            integration,
            config,
            char_budget=config.reviewer_context_chars,
            include_map=True,
        ).render()

        if self.manage_models:
            with self.progress.operation("RELEASE", "Preparing reviewer model"):
                self.model_manager.ensure(self.global_config.reviewer)
        with self.progress.operation("RELEASE", "Local release-review inference"):
            audit = self.agents.release_audit(
                profile=self.global_config.reviewer,
                base_branch=config.base_branch,
                diff=diff,
                tests=test_text,
                context=context,
            )
        if not tests.passed or open_external:
            audit.locally_release_ready = False

        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        bundle = project_state_dir(git.root) / "release-reviews" / stamp
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "FULL_TEST_RESULTS.txt").write_text(test_text, encoding="utf-8")
        (bundle / "BASE_TO_INTEGRATION.patch").write_text(diff, encoding="utf-8")
        (bundle / "LOCAL_RELEASE_REVIEW.json").write_text(
            audit.model_dump_json(indent=2),
            encoding="utf-8",
        )
        issue_lines = ["# Open external-review tasks", ""]
        if open_external:
            for task in open_external:
                issue_lines.extend(
                    [
                        f"## {task.id}",
                        "",
                        f"- Kind: `{task.kind.value}`",
                        f"- Outcome: `{task.status.value}`",
                        f"- Category: `{task.external_review_category.value if task.external_review_category else 'unknown'}`",
                        "",
                        task.description,
                        "",
                        task.external_review_reason or "No reason was recorded.",
                        "",
                    ]
                )
                if task.escalation_path and Path(task.escalation_path).exists():
                    shutil.copytree(
                        Path(task.escalation_path),
                        bundle / "open-issues" / task.id,
                        dirs_exist_ok=True,
                    )
        else:
            issue_lines.append("No unresolved external-review tasks were recorded.")
        (bundle / "OPEN_EXTERNAL_TASKS.md").write_text(
            "\n".join(issue_lines) + "\n",
            encoding="utf-8",
        )

        prompt = f"""# Independent Release Review

Repository: `{git.root}`
Base branch: `{config.base_branch}`
Candidate branch: `{config.integration_branch}`
Candidate commit: `{git.resolve_ref(integration, 'HEAD')}`

Local full tests passed: `{tests.passed}`
Local reviewer marked locally release-ready: `{audit.locally_release_ready}`

Read:

- `AGENTS.md`
- canonical files under `docs/ai/`
- `{bundle / 'LOCAL_RELEASE_REVIEW.json'}`
- `{bundle / 'FULL_TEST_RESULTS.txt'}`
- `{bundle / 'BASE_TO_INTEGRATION.patch'}`
- `{bundle / 'OPEN_EXTERNAL_TASKS.md'}`
- issue-specific artifacts under `{bundle / 'open-issues'}` when present

Open external-review task count: `{len(open_external)}`

Required external work reported locally:

{json.dumps(audit.required_external_work, indent=2)}

Independently audit and repair the candidate. Resolve or explicitly disposition every open external-review task, while preserving later integrated work. Verify architecture, correctness, security, data integrity, migrations, concurrency, dependency choices, error handling, performance, tests, documentation, packaging, and release behavior. Do not trust local-model claims without checking code and commands. Merge into the base branch only after the candidate is genuinely ready.
"""
        (bundle / "EXTERNAL_RELEASE_REVIEW.md").write_text(prompt, encoding="utf-8")
        (bundle / "MANIFEST.json").write_text(
            json.dumps(
                {
                    "repository": str(git.root),
                    "base_branch": config.base_branch,
                    "candidate_branch": config.integration_branch,
                    "candidate_commit": git.resolve_ref(integration, "HEAD"),
                    "generated_at": datetime.now(UTC).isoformat(),
                    "tests_passed": tests.passed,
                    "open_external_task_ids": [task.id for task in open_external],
                    "local_review": audit.model_dump(),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        self.progress.emit(f"[RELEASE] COMPLETE — review bundle created at {bundle}")
        return bundle
