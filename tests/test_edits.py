from __future__ import annotations

from pathlib import Path

import pytest

from localdev_mlx.git.edits import EditError, apply_edits
from localdev_mlx.schemas import FileEdit


def test_apply_exact_replace(tmp_path: Path) -> None:
    target = tmp_path / "a.py"
    target.write_text("value = 1\n", encoding="utf-8")
    changed = apply_edits(
        tmp_path,
        [
            FileEdit(
                operation="replace_text",
                path="a.py",
                old_text="value = 1",
                new_text="value = 2",
                reason="test",
            )
        ],
        allowed_paths={"a.py"},
        deny_patterns=(),
    )
    assert changed == ["a.py"]
    assert target.read_text(encoding="utf-8") == "value = 2\n"


def test_apply_rejects_path_outside_allowlist(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    with pytest.raises(EditError):
        apply_edits(
            tmp_path,
            [FileEdit(operation="delete", path="a.py", reason="test")],
            allowed_paths={"b.py"},
            deny_patterns=(),
        )


def test_apply_rejects_secret_like_path(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("TOKEN=x", encoding="utf-8")
    with pytest.raises(EditError):
        apply_edits(
            tmp_path,
            [FileEdit(operation="delete", path=".env", reason="test")],
            allowed_paths={".env"},
            deny_patterns=(),
        )


def test_edit_response_rolls_back_when_a_later_edit_fails(tmp_path: Path) -> None:
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")

    with pytest.raises(EditError):
        apply_edits(
            tmp_path,
            [
                FileEdit(
                    operation="replace_text",
                    path="first.py",
                    old_text="value = 1",
                    new_text="value = 10",
                    reason="first edit",
                ),
                FileEdit(
                    operation="replace_text",
                    path="second.py",
                    old_text="missing text",
                    new_text="value = 20",
                    reason="force failure",
                ),
            ],
            allowed_paths={"first.py", "second.py"},
            deny_patterns=(),
        )

    assert first.read_text(encoding="utf-8") == "value = 1\n"
    assert second.read_text(encoding="utf-8") == "value = 2\n"


def test_replacing_file_preserves_executable_mode(tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\necho old\n", encoding="utf-8")
    script.chmod(0o755)

    apply_edits(
        tmp_path,
        [
            FileEdit(
                operation="replace_file",
                path="run.sh",
                content="#!/bin/sh\necho new\n",
                reason="update script",
            )
        ],
        allowed_paths={"run.sh"},
        deny_patterns=(),
    )

    assert script.stat().st_mode & 0o777 == 0o755
