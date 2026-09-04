from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from localdev_mlx.config import GlobalConfig, ModelProfile, RoleConfig


@pytest.fixture
def global_config() -> GlobalConfig:
    planner = ModelProfile(
        name="planner",
        model="mock/planner",
        executable=Path("/bin/true"),
        enable_thinking=True,
        thinking_budget=4096,
        max_tokens=8192,
    )
    worker = ModelProfile(
        name="worker",
        model="mock/worker",
        executable=Path("/bin/true"),
        enable_thinking=True,
        thinking_budget=2048,
        max_tokens=6144,
    )
    reviewer = ModelProfile(
        name="reviewer",
        model="mock/reviewer",
        executable=Path("/bin/true"),
        enable_thinking=True,
        thinking_budget=4096,
        max_tokens=8192,
    )
    return GlobalConfig(
        models={
            "planner": planner,
            "worker": worker,
            "reviewer": reviewer,
        },
        roles=RoleConfig(planner="planner", worker="worker", reviewer="reviewer"),
    )


@pytest.fixture
def sample_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import localdev_mlx.config as config_module

    monkeypatch.setattr(config_module, "DATA_ROOT", tmp_path / "localdev-data")
    repository = tmp_path / "repo"
    (repository / "src/samplecalc").mkdir(parents=True)
    (repository / "tests").mkdir()
    (repository / "src/samplecalc/__init__.py").write_text(
        "from samplecalc.core import add, subtract\n",
        encoding="utf-8",
    )
    (repository / "src/samplecalc/core.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n\n\ndef subtract(a: int, b: int) -> int:\n    return a + b  # intentional demo bug\n",
        encoding="utf-8",
    )
    (repository / "tests/test_core.py").write_text(
        "import unittest\n\nfrom samplecalc import add, subtract\n\nclass Tests(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n    def test_subtract(self): self.assertEqual(subtract(7, 2), 5)\n",
        encoding="utf-8",
    )
    (repository / "README.md").write_text("# Sample\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Test User"], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )

    from localdev_mlx.project import initialize_project

    initialize_project(
        repository,
        quick_tests=["python3 -m unittest discover -s tests -v"],
        full_tests=["python3 -m unittest discover -s tests -v"],
        test_env={"PYTHONPATH": "src"},
    )
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "initialize localdev"],
        check=True,
        capture_output=True,
    )
    return repository
