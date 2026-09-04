from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from localdev_mlx.config import ProjectConfig, project_state_dir


class GitError(RuntimeError):
    """Raised when a Git operation fails."""


@dataclass(frozen=True)
class TaskWorkspace:
    task_id: str
    branch: str
    path: Path
    integration_path: Path
    integration_branch: str
    base_commit: str | None = None


class GitRepository:
    def __init__(self, repository: Path) -> None:
        self.root = self.find_root(repository)

    @staticmethod
    def find_root(path: Path) -> Path:
        completed = subprocess.run(
            ["git", "-C", str(path.resolve()), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise GitError(f"Not a Git repository: {path}\n{completed.stderr.strip()}")
        return Path(completed.stdout.strip()).resolve()

    @staticmethod
    def _run(
        cwd: Path,
        args: list[str],
        *,
        check: bool = True,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        completed = subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            input=input_text,
            capture_output=True,
            check=False,
        )
        if check and completed.returncode != 0:
            raise GitError(f"git {' '.join(args)} failed in {cwd}:\n{completed.stderr.strip()}")
        return completed

    def has_commits(self) -> bool:
        return self._run(self.root, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0

    def is_clean(self, path: Path | None = None) -> bool:
        cwd = path or self.root
        output = self._run(cwd, ["status", "--porcelain"]).stdout
        return not output.strip()

    def current_branch(self, path: Path | None = None) -> str:
        cwd = path or self.root
        return self._run(cwd, ["branch", "--show-current"]).stdout.strip()

    def branch_exists(self, branch: str) -> bool:
        return (
            self._run(
                self.root,
                ["show-ref", "--verify", f"refs/heads/{branch}"],
                check=False,
            ).returncode
            == 0
        )

    def worktree_map(self) -> dict[str, Path]:
        raw = self._run(self.root, ["worktree", "list", "--porcelain"]).stdout
        result: dict[str, Path] = {}
        current_path: Path | None = None
        for line in raw.splitlines():
            if line.startswith("worktree "):
                current_path = Path(line.removeprefix("worktree ")).resolve()
            elif line.startswith("branch refs/heads/") and current_path:
                branch = line.removeprefix("branch refs/heads/")
                result[branch] = current_path
        return result

    def ensure_integration_worktree(self, config: ProjectConfig) -> Path:
        if not self.has_commits():
            raise GitError(
                "The repository has no commits. Create an initial commit before LocalDev creates worktrees."
            )
        mapping = self.worktree_map()
        existing = mapping.get(config.integration_branch)
        if existing:
            if not self.is_clean(existing):
                raise GitError(f"Integration worktree is not clean: {existing}")
            return existing
        state = project_state_dir(self.root)
        integration_path = state / "worktrees" / "integration"
        if integration_path.exists():
            raise GitError(
                f"Unregistered integration worktree path exists; preserve and inspect: {integration_path}"
            )
        integration_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.branch_exists(config.integration_branch):
            self._run(self.root, ["branch", config.integration_branch, config.base_branch])
        self._run(
            self.root,
            ["worktree", "add", str(integration_path), config.integration_branch],
        )
        return integration_path

    def create_task_workspace(self, config: ProjectConfig, task_id: str) -> TaskWorkspace:
        integration_path = self.ensure_integration_worktree(config)
        if not self.is_clean(integration_path):
            raise GitError(
                f"Integration worktree is not clean: {integration_path}. Resolve it before starting a task."
            )
        slug = re.sub(r"[^a-z0-9-]+", "-", task_id.lower()).strip("-")
        branch = f"localdev/{slug}"
        state = project_state_dir(self.root)
        path = state / "worktrees" / "tasks" / slug
        if path.exists():
            raise GitError(f"Task worktree already exists; refusing to reuse: {path}")
        if self.branch_exists(branch):
            raise GitError(f"Task branch already exists; refusing to reuse: {branch}")
        path.parent.mkdir(parents=True, exist_ok=True)
        base_commit = self.resolve_ref(integration_path, "HEAD")
        self._run(
            self.root,
            [
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                base_commit,
            ],
        )
        return TaskWorkspace(
            task_id=task_id,
            branch=branch,
            path=path,
            integration_path=integration_path,
            integration_branch=config.integration_branch,
            base_commit=base_commit,
        )

    def diff(self, path: Path, base: str | None = None) -> str:
        # Intent-to-add makes untracked text files visible in the review diff without committing them.
        self._run(path, ["add", "-N", "."], check=False)
        args = ["diff", "--no-ext-diff", "--binary"]
        if base:
            args.append(base)
        else:
            args.append("HEAD")
        return self._run(path, args).stdout

    def status_porcelain(self, path: Path) -> str:
        return self._run(path, ["status", "--porcelain=v1"]).stdout

    def commit_all(self, path: Path, message: str) -> str:
        self._run(path, ["add", "-A"])
        if not self.status_porcelain(path).strip():
            raise GitError("There are no changes to commit")
        self._run(path, ["commit", "-m", message])
        return self._run(path, ["rev-parse", "HEAD"]).stdout.strip()

    def integrate(self, workspace: TaskWorkspace) -> str:
        if not self.is_clean(workspace.integration_path):
            raise GitError("Integration worktree became dirty; refusing to merge")
        if (
            workspace.base_commit
            and self.resolve_ref(workspace.integration_path, "HEAD") != workspace.base_commit
        ):
            raise GitError(
                "Integration advanced during the task; rebase and review the preserved task before integration"
            )
        self._run(
            workspace.integration_path,
            ["merge", "--ff-only", workspace.branch],
        )
        return self._run(workspace.integration_path, ["rev-parse", "HEAD"]).stdout.strip()

    def cleanup_task_workspace(
        self, workspace: TaskWorkspace, *, delete_branch: bool = True, force: bool = False
    ) -> None:
        expected = project_state_dir(self.root) / "worktrees" / "tasks"
        if workspace.path.resolve().parent != expected.resolve():
            raise GitError("Refusing cleanup outside registered task worktree directory")
        if self.worktree_map().get(workspace.branch) != workspace.path.resolve():
            raise GitError("Task worktree registration does not match the recorded branch/path")
        if not force and not self.is_clean(workspace.path):
            raise GitError("Refusing to remove a dirty task worktree without explicit --force")
        self._run(
            self.root,
            ["worktree", "remove", *(["--force"] if force else []), str(workspace.path)],
        )
        if delete_branch and self.branch_exists(workspace.branch):
            if self.is_ancestor(self.root, workspace.branch, workspace.integration_branch):
                self._run(self.root, ["branch", "-d", workspace.branch], check=False)

    def list_files(self, path: Path) -> list[str]:
        completed = self._run(path, ["ls-files", "-co", "--exclude-standard"])
        return sorted(set(line for line in completed.stdout.splitlines() if line.strip()))

    def resolve_ref(self, path: Path, ref: str) -> str:
        return self._run(
            path, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"]
        ).stdout.strip()

    def diff_between(self, path: Path, left: str, right: str = "HEAD") -> str:
        return self._run(path, ["diff", "--no-ext-diff", "--binary", f"{left}...{right}"]).stdout

    def is_ancestor(self, path: Path, ancestor: str, descendant: str) -> bool:
        return (
            self._run(
                path,
                ["merge-base", "--is-ancestor", ancestor, descendant],
                check=False,
            ).returncode
            == 0
        )

    def fast_forward(self, path: Path, ref: str) -> str:
        """Fast-forward the checked-out branch to ref without creating a merge commit."""
        self._run(path, ["merge", "--ff-only", ref])
        return self.resolve_ref(path, "HEAD")

    def configured_identity(self) -> tuple[str | None, str | None]:
        name = self._run(self.root, ["config", "user.name"], check=False).stdout.strip() or None
        email = self._run(self.root, ["config", "user.email"], check=False).stdout.strip() or None
        return name, email
