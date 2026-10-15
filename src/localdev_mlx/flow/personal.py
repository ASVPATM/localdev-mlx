"""Opt-in personal snapshots. Only reviewed adapters; never attach to live storage."""

from __future__ import annotations

import ctypes
import errno
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .workspace import atomic_write, safe_path

MAX_SNAPSHOT_BYTES = 20 * 1024**3
MAX_SNAPSHOT_FILES = 100_000


def discard_snapshot(config, workspace):
    """Delete only generated personal data/home copies, never evidence or live storage."""
    if config.mode != "personal" or workspace is None:
        return
    relative = workspace.relative_to(config.data).as_posix()
    if not re.fullmatch(r"inspections/[a-f0-9]{32}/workspace", relative):
        raise ValueError("Refusing to discard an unrecognized personal workspace")
    safe_path(config.data, relative)
    if not (workspace.parent / "baseline.json").is_file():
        raise ValueError("Personal workspace has no source snapshot record")
    for name in (".flow-data", ".flow-home"):
        target = safe_path(config.data, relative + "/" + name)
        if target.exists():
            shutil.rmtree(target)  # Exact generated copy, after its process has stopped.


def discard_abandoned(config):
    # Called only while holding this mode's exclusive server lease.
    if config.mode == "personal":
        for run in (config.data / "inspections").glob("*"):
            if re.fullmatch(r"[a-f0-9]{32}", run.name) and (run / "baseline.json").is_file():
                discard_snapshot(config, run / "workspace")


def require_sandbox():
    if sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError(
            "Personal-data Flow requires macOS sandbox-exec; no unsafe fallback is used"
        )


def source_root(config):
    source = Path(config.personal_source).expanduser()
    if not config.personal_source or not source.is_absolute():
        raise ValueError("Set an absolute personal.source data directory in project-flow.toml")
    if any(p.is_symlink() for p in [source, *source.parents]):
        raise ValueError("Personal data must not traverse symlinks")
    source = source.resolve(strict=True)
    if (
        not source.is_dir()
        or source in {Path(source.anchor), Path.home().resolve()}
        or config.data.is_relative_to(source)
        or source.is_relative_to(config.root / ".project-flow")
    ):
        raise ValueError("Choose the specific live app data directory, separate from Flow evidence")
    database = safe_path(source, "app.sqlite3")
    if not database.is_file():
        raise ValueError("CanvasMaster personal data needs an existing app.sqlite3")
    return source


def for_mode(config, mode):
    if mode == "sample":
        return config
    if mode != "personal":
        raise ValueError("Unknown Flow mode")
    require_sandbox()
    if config.personal_adapter != "canvasmaster":
        raise ValueError(
            "Personal-data Flow currently supports the CanvasMaster adapter only. "
            'Set [personal] adapter = "canvasmaster" and source in project-flow.toml.'
        )
    if not (config.root / "src/canvas_optimizer/web.py").is_file():
        raise ValueError("The CanvasMaster adapter requires src/canvas_optimizer/web.py")
    if not (config.root / ".venv/bin/python").is_file():
        raise ValueError("Install this project's Python dependencies before using personal Flow")
    result = replace(
        config,
        mode="personal",
        preview=("{project_python}", "-I", "{personal_preview}", "{port}"),
        preview_kind="web",
        preview_mutations=True,
        preview_csp="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'",
        preview_timeout=120,
        preview_ready_file="",
        adapter_name="CanvasMaster personal-data snapshot",
        autodetected=False,
        supports_seed=False,
        scenarios=("personal snapshot",),
        capture_narrow=False,
    )
    source_root(result)
    return result


def sandbox_command(argv, workspace, *, project, live_source=None):
    """Deny-by-default OS policy inherited by any descendants; no network egress."""
    require_sandbox()
    workspace = workspace.resolve(strict=True)
    python = Path(argv[0])
    read_roots = {
        workspace,
        (project / ".venv").resolve(strict=True),
        python.absolute().parent.parent.resolve(strict=True),
        python.resolve(strict=True).parent.parent,
        Path(__file__).resolve().parent,
        Path("/System/Library"),
        Path("/System/Volumes/Preboot/Cryptexes/OS"),
        Path("/System/Cryptexes"),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/Library/Frameworks"),
        Path("/private/var/db/timezone"),
    }

    def quote(p):
        return json.dumps(str(p), ensure_ascii=False)

    command = list(argv)
    executables = {python, python.resolve()}
    framework = python.resolve().parent.parent / "Resources/Python.app/Contents/MacOS/Python"
    if framework.is_file():
        # Framework launchers spawn Python.app. Execute it directly so fork stays
        # denied; CPython's macOS launcher variable preserves the selected venv.
        executables.update({framework, Path("/usr/bin/env")})
        command = ["/usr/bin/env", f"__PYVENV_LAUNCHER__={python}", str(framework), *argv[1:]]
    execute = " ".join(f"(literal {quote(p)})" for p in sorted(executables))
    reads = " ".join(f"(subpath {quote(p)})" for p in sorted(read_roots))
    port = int(argv[-1])
    if not 1 <= port <= 65535:
        raise ValueError("Personal preview needs a loopback port")
    profile = f"""(version 1)
(deny default)
(allow file-read-metadata)
(allow file-read* {reads} (literal "/") (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random") (literal "/private/etc/localtime") (literal "/private/etc/apache2/mime.types"))
(allow file-map-executable {reads})
(allow file-read* file-map-executable {execute})
(allow file-write* (subpath {quote(workspace)}) (literal {quote(workspace.parent / "preview.log")}) (literal "/dev/null"))
(allow process-exec {execute})
(allow signal (target self))
(allow sysctl-read)
(allow network-bind (local ip "localhost:{port}"))
(allow network-inbound (local ip "localhost:{port}"))
"""
    if live_source is not None:
        profile += (
            f"(deny file-read* file-write* (subpath {quote(live_source.resolve(strict=True))}))\n"
        )
    destination = workspace.parent / "personal.sb"
    atomic_write(destination, profile.encode())
    return ["/usr/bin/sandbox-exec", "-f", str(destination), *command]


def copy_asset(source, destination):
    """APFS copy-on-write when available, never a hard link; portable copy fallback."""
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        clonefile = library.clonefile
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clonefile.restype = ctypes.c_int
        if clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0:
            return
        if ctypes.get_errno() not in {errno.EXDEV, errno.ENOTSUP, errno.EINVAL, errno.ENOSYS}:
            raise OSError(ctypes.get_errno(), "Could not clone snapshot asset")
    shutil.copyfile(source, destination)


def prepare_snapshot(config, workspace, cancelled, progress=lambda _: None):
    """Only stdlib reads touch live data. Project code runs later, inside the sandbox."""
    require_sandbox()
    source = source_root(config)
    target = workspace / ".flow-data"
    target.mkdir(mode=0o700, exist_ok=False)
    (workspace / ".flow-home/tmp").mkdir(parents=True, mode=0o700)
    started = time.monotonic()

    def check():
        if cancelled.is_set():
            raise ValueError("Personal snapshot cancelled; live data was not changed")
        if time.monotonic() - started > 600:
            raise ValueError("Personal snapshot exceeded ten minutes; no preview was started")

    check()
    database = safe_path(source, "app.sqlite3")
    total = database.stat().st_size
    if total > MAX_SNAPSHOT_BYTES or shutil.disk_usage(workspace).free < total + 256 * 1024**2:
        raise ValueError("Insufficient snapshot space (or data exceeds the 20 GB limit)")
    progress("Copying a consistent personal database; live data stays read-only")
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5) as live:
        live.execute("PRAGMA query_only=ON")
        page_bytes = live.execute("PRAGMA page_size").fetchone()[0]
        with sqlite3.connect(target / "app.sqlite3") as snapshot:

            def backup_progress(status, remaining, pages):
                check()
                if pages * page_bytes > MAX_SNAPSHOT_BYTES:
                    raise ValueError("Personal database exceeds the snapshot size limit")

            live.backup(snapshot, pages=256, progress=backup_progress, sleep=0.05)
            if snapshot.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("Personal database snapshot failed integrity verification")
    (target / "app.sqlite3").chmod(0o600)
    total = (target / "app.sqlite3").stat().st_size
    planned = []
    # Enumerate after the database snapshot, so newly referenced assets are not missed.
    # No credentials, browser profiles, configuration, runtime, logs or old backups.
    for folder in ("objects", "artifacts", "meeting-artifacts"):
        top = safe_path(source, folder)
        if not top.exists():
            continue
        for path in top.rglob("*"):
            check()
            relative = path.relative_to(source).as_posix()
            safe_path(source, relative)
            if not path.is_file():
                if not path.is_dir():
                    raise ValueError("Personal snapshot contains a non-regular asset")
                continue
            size = path.stat().st_size
            total += size
            planned.append((relative, size))
            if len(planned) > MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
                raise ValueError("Personal snapshot exceeds 20 GB / 100,000 files")
    if total > MAX_SNAPSHOT_BYTES or shutil.disk_usage(workspace).free < total + 256 * 1024**2:
        raise ValueError("Insufficient snapshot space (or data exceeds the 20 GB limit)")
    progress(f"Copying {len(planned)} personal assets into the isolated workspace")
    for relative, size in planned:
        check()
        path = safe_path(source, relative)
        before = path.stat()
        copy_asset(path, target / relative)
        after = path.stat()
        if before.st_size != size or (before.st_size, before.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError("A personal asset changed during copying; refresh when it is stable")
        (target / relative).chmod(0o600)
    record = {
        "mode": "personal",
        "adapter": "canvasmaster",
        "captured_at": datetime.now(UTC).isoformat(),
        "logical_bytes": total,
        "asset_files": len(planned),
        "offline": True,
        "writeback": False,
    }
    atomic_write(target / "flow-snapshot.json", json.dumps(record).encode())
    return record
