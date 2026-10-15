"""CanvasMaster offline preview entry point. Executed only inside the OS sandbox."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path


def main():
    root = Path.cwd().resolve(strict=True)
    data = root / ".flow-data"
    if (
        os.environ.get("PROJECT_FLOW_PERSONAL") != "1"
        or Path(os.environ.get("PROJECT_FLOW_DATA", "/")) != data
        or not (data / "flow-snapshot.json").is_file()
        or data.is_symlink()
    ):
        raise SystemExit("Personal preview requires an isolated prepared snapshot")
    # Never instantiate Store against live data, invoke uv, or reuse the live worker.
    sys.path.insert(0, str(root / "src"))
    with sqlite3.connect(data / "app.sqlite3") as conn:
        conn.execute("UPDATE schedules SET enabled=0")
        conn.execute(
            "UPDATE jobs SET state='cancelled',owner=NULL,lease_until=NULL "
            "WHERE state IN ('queued','running','retry_wait','cancel_requested')"
        )
        settings = json.loads(conn.execute("SELECT payload FROM settings WHERE id=1").fetchone()[0])
        settings.setdefault("canvas", {})["credential_backend"] = "environment"
        conn.execute("UPDATE settings SET payload=? WHERE id=1", (json.dumps(settings),))
    from canvas_optimizer.cli import serve_app
    from canvas_optimizer.store import Store

    serve_app(Store(data), int(sys.argv[1]), False, False)


if __name__ == "__main__":
    main()
