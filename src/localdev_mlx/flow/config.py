from __future__ import annotations

import fnmatch
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

DENIED_PARTS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".project-flow",
    ".project-flow-update-backups",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "cache",
    "output",
    "dist",
    "build",
    ".localdev",
    ".pmt-flow",
    ".flow-home",
    ".flow-data",
}
DENIED_GLOBS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.sqlite*",
    "*.db",
    "*.dump",
    "*.log",
    "*credentials*",
    "*secrets*",
)


@dataclass(frozen=True)
class Config:
    root: Path
    mode: str = "sample"
    personal_adapter: str = ""
    personal_source: str = ""
    personal_database: str = "app.sqlite3"
    personal_assets: tuple[str, ...] = ()
    personal_entrypoint: str = ""
    personal_python: str = ".venv/bin/python"
    include: tuple[str, ...] = (
        "src",
        "tests",
        "docs",
        "scripts",
        "pyproject.toml",
        "README.md",
    )
    exclude: tuple[str, ...] = ()
    allowed_source_files: tuple[str, ...] = ()
    checks: tuple[tuple[str, ...], ...] = ()
    preview: tuple[str, ...] = ()
    preview_kind: str = "web"
    preview_csp: str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
    preview_mutations: bool = False
    preview_timeout: int = 30
    preview_ready_file: str = ""
    adapter_name: str = "project-flow.toml"
    autodetected: bool = False
    supports_seed: bool = False
    capture_narrow: bool = False
    organizer_timeout: int = 20
    scenarios: tuple[str, ...] = ("default",)
    safe_query_keys: tuple[str, ...] = (
        "view",
        "page",
        "sort",
        "direction",
        "page_size",
    )
    model: str = ""
    model_url: str = "http://127.0.0.1:11434"
    max_file_bytes: int = 524288
    max_total_bytes: int = 32 * 1024 * 1024
    check_timeout: int = 180
    retrieval_tokens: int = 6000
    high_risk_paths: tuple[str, ...] = (
        "**/migrations/**",
        "**/security*",
        "**/auth*",
        "alembic/**",
    )
    data: Path = field(init=False)

    def __post_init__(self):
        object.__setattr__(self, "root", self.root.resolve(strict=True))
        if self.mode not in {"sample", "personal"}:
            raise ValueError("Flow mode must be sample or personal")
        data = self.root / ".project-flow"
        object.__setattr__(self, "data", data / "personal" if self.mode == "personal" else data)
        if self.preview_kind not in {"web", "command"}:
            raise ValueError("preview_kind must be web or command (native/CLI/backend)")
        if not 1 <= self.preview_timeout <= 300:
            raise ValueError("preview_timeout must be between 1 and 300 seconds")
        if self.preview_ready_file:
            ready = PurePosixPath(self.preview_ready_file)
            if (
                ready.is_absolute()
                or ".." in ready.parts
                or "\\" in self.preview_ready_file
                or len(ready.parts) < 2
                or ready.parts[0] != ".flow-data"
            ):
                raise ValueError("preview_ready_file must be a relative file inside .flow-data/")
        if not 1 <= self.organizer_timeout <= 120:
            raise ValueError("organizer_timeout must be between 1 and 120 seconds")
        if (
            not self.preview_csp
            or any(char in self.preview_csp for char in "\r\n")
            or len(self.preview_csp) > 4000
        ):
            raise ValueError("preview_csp must be a nonempty single-line synthetic-only policy")
        if (
            not 1024 <= self.max_file_bytes <= 16 * 1024 * 1024
            or not self.max_file_bytes <= self.max_total_bytes <= 256 * 1024 * 1024
        ):
            raise ValueError(
                "Snapshot budgets must be bounded: file 1 KB–16 MB, total up to 256 MB"
            )
        if not self.scenarios or any(not isinstance(s, str) or len(s) > 80 for s in self.scenarios):
            raise ValueError("Provide at least one bounded scenario name")
        for value in self.include:
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts or str(path) in {"", "."}:
                raise ValueError(
                    "Include entries must be explicit project-relative files or directories"
                )
        for value in self.allowed_source_files:
            path = PurePosixPath(value)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in value
                or path.suffix not in {".py", ".js", ".ts", ".tsx", ".jsx", ".swift", ".rs", ".go"}
                or any(char in value for char in "*?[]")
            ):
                raise ValueError(
                    "allowed_source_files must name exact project-relative source files"
                )
        parsed = urlsplit(self.model_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("The optional model must use a credential-free loopback HTTP endpoint")

    def allows(self, relative: str) -> bool:
        path = PurePosixPath(relative)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in relative
            or any(p in DENIED_PARTS for p in path.parts)
        ):
            return False
        if (
            any(fnmatch.fnmatch(path.name.casefold(), p) for p in DENIED_GLOBS)
            and relative not in self.allowed_source_files
        ):
            return False
        if any(fnmatch.fnmatch(relative, p) for p in self.exclude):
            return False
        return any(relative == p or relative.startswith(p.rstrip("/") + "/") for p in self.include)

    def command(
        self,
        argv: tuple[str, ...],
        *,
        port: int = 0,
        scenario: str = "default",
        seed: int = 42,
    ) -> list[str]:
        # No shell, shell interpolation, source-project CWD, or inherited application secrets.
        project_python = (
            self.root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        if self.mode == "personal":
            project_python = self.root / self.personal_python
        return [
            s.replace("{python}", sys.executable)
            .replace(
                "{project_python}",
                str(project_python) if project_python.exists() else sys.executable,
            )
            .replace("{port}", str(port))
            .replace("{scenario}", scenario)
            .replace("{seed}", str(seed))
            .replace("{pmt_preview}", str(Path(__file__).with_name("pmt_preview.py")))
            .replace("{personal_preview}", self.personal_entrypoint)
            for s in argv
        ]

    def environment(self, workspace: Path) -> dict[str, str]:
        allowed = ("PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL")
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        env.update(
            PROJECT_FLOW_HOME=str(workspace / ".flow-home"),
            PROJECT_FLOW_DATA=str(workspace / ".flow-data"),
            PYTHONPATH=str(workspace / "src"),
            PYTHONDONTWRITEBYTECODE="1",
            PROJECT_FLOW_PREVIEW="1",
        )
        if self.mode == "personal":
            env.update(
                HOME=str(workspace / ".flow-home"),
                TMPDIR=str(workspace / ".flow-home/tmp"),
                PROJECT_FLOW_PERSONAL="1",
                PYTHONNOUSERSITE="1",
            )
        return env


def load_config(root: Path) -> Config:
    file = root / "project-flow.toml"
    if not file.is_file():
        raise ValueError(
            "Run localdev-mlx flow init --project PATH and review project-flow.toml first"
        )
    if file.is_symlink() or file.stat().st_size > 65536:
        raise ValueError("project-flow.toml must be a regular, bounded local configuration file")
    raw = tomllib.loads(file.read_text())
    project, model = raw.get("project", {}), raw.get("model", {})
    assets = raw.get("personal", {}).get("assets", [])
    if (
        not isinstance(assets, list)
        or len(assets) > 32
        or any(not isinstance(x, str) for x in assets)
    ):
        raise ValueError("personal.assets must be a list of at most 32 directory names")

    def commands(name):
        values = project.get(name, [])
        if not isinstance(values, list) or any(
            not isinstance(v, list) or not v or any(not isinstance(s, str) or not s for s in v)
            for v in values
        ):
            raise ValueError(f"{name} must be a list of nonempty argument arrays")
        return tuple(tuple(v) for v in values)

    previews = commands("preview")
    if len(previews) > 1:
        raise ValueError("At most one preview command is supported")
    return Config(
        root=root,
        personal_adapter=str(raw.get("personal", {}).get("adapter", "")),
        personal_source=str(raw.get("personal", {}).get("source", "")),
        personal_database=str(raw.get("personal", {}).get("database", "app.sqlite3")),
        personal_assets=tuple(assets),
        personal_entrypoint=str(raw.get("personal", {}).get("entrypoint", "")),
        personal_python=str(raw.get("personal", {}).get("python", ".venv/bin/python")),
        include=tuple(project.get("include", Config.__dataclass_fields__["include"].default)),
        exclude=tuple(project.get("exclude", [])),
        allowed_source_files=tuple(project.get("allowed_source_files", [])),
        checks=commands("checks"),
        preview=previews[0] if previews else (),
        preview_kind=str(project.get("preview_kind", "web")),
        preview_csp=str(
            project.get("preview_csp", Config.__dataclass_fields__["preview_csp"].default)
        ),
        preview_mutations=project.get("preview_mutations") is True,
        preview_timeout=int(project.get("preview_timeout", 30)),
        preview_ready_file=str(project.get("preview_ready_file", "")),
        supports_seed=project.get("supports_seed") is True,
        capture_narrow=project.get("capture_narrow") is True,
        organizer_timeout=int(model.get("organizer_timeout", 20)),
        scenarios=tuple(project.get("scenarios", ["default"])),
        safe_query_keys=tuple(
            project.get("safe_query_keys", ["view", "page", "sort", "direction", "page_size"])
        ),
        max_file_bytes=int(project.get("max_file_bytes", 524288)),
        max_total_bytes=int(project.get("max_total_bytes", 32 * 1024 * 1024)),
        model=str(model.get("name", "")),
        model_url=str(model.get("url", "http://127.0.0.1:11434")),
        high_risk_paths=tuple(
            project.get(
                "high_risk_paths",
                Config.__dataclass_fields__["high_risk_paths"].default,
            )
        ),
    )
