"""Durable, non-blocking validation jobs for the exact staged Git tree."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from .state import Store

JOBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS git_test_jobs (
    id INTEGER PRIMARY KEY,
    staged_tree TEXT NOT NULL,
    head TEXT,
    worktree TEXT NOT NULL,
    command TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    exit_code INTEGER,
    output_sha256 TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    UNIQUE(staged_tree, worktree, command)
);
"""


def test_command(root: Path, config: dict) -> list[str] | None:
    configured = config.get("git", {}).get("tests", {}).get("command")
    if isinstance(configured, list) and configured and all(
        isinstance(arg, str) and arg for arg in configured
    ):
        return configured
    if (root / "pyproject.toml").is_file() and (
        (root / "tests").is_dir() or (root / "pytest.ini").is_file()
    ):
        return ["uv", "run", "--group", "dev", "pytest", "-q"]
    if (root / "Cargo.toml").is_file():
        return ["cargo", "test"]
    if (root / "go.mod").is_file():
        return ["go", "test", "./..."]
    return None


def enqueue_test(root: Path, revision: dict, config: dict) -> int | None:
    if config.get("git", {}).get("pre_commit", {}).get("tests", True) is False:
        return None
    command = test_command(root, config)
    if command is None:
        with Store(root, "git") as store:
            store.record("git_test_skipped", {"reason": "no configured or detected test runner"})
        return None
    encoded = json.dumps(command, separators=(",", ":"))
    with Store(root, "git") as store:
        store.db.executescript(JOBS_SCHEMA)
        result = store.db.execute(
            """INSERT OR IGNORE INTO git_test_jobs
               (staged_tree, head, worktree, command) VALUES (?, ?, ?, ?)""",
            (revision["staged_tree"], revision["head"], str(root), encoded),
        )
        row = store.db.execute(
            """SELECT id, status FROM git_test_jobs
               WHERE staged_tree=? AND worktree=? AND command=?""",
            (revision["staged_tree"], str(root), encoded),
        ).fetchone()
        job_id, status = row
        if result.rowcount == 0 or status != "queued":
            return job_id
        store.record(
            "git_test_queued", {"job_id": job_id, "staged_tree": revision["staged_tree"]}
        )
    start_worker(root, job_id)
    return job_id


def start_worker(root: Path, job_id: int) -> None:
    """Spawn a detached process; no test execution in the Git hook process."""
    options = {}
    if os.name == "nt":
        options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        options["start_new_session"] = True
    subprocess.Popen(
        [sys.executable, "-m", "codex_hooks.git_bridge", "worker", str(root), str(job_id)],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )


def execute_job(root: Path, job_id: int, timeout: int = 600) -> str:
    """Run isolated tests against an exported staged-tree snapshot."""
    with Store(root, "git") as store:
        store.db.executescript(JOBS_SCHEMA)
        claimed = store.db.execute(
            "UPDATE git_test_jobs SET status='running' WHERE id=? AND status='queued'", (job_id,)
        )
        row = store.db.execute(
            "SELECT staged_tree, command FROM git_test_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if claimed.rowcount != 1 or row is None:
            return "not-queued"
        tree, command_json = row
    code = 1
    output = ""
    try:
        with Store(root, "git") as store, tempfile.TemporaryDirectory(
            prefix=f"test-{job_id}-", dir=store.directory
        ) as temp:
            directory = Path(temp)
            archive = directory / "staged.zip"
            snapshot = directory / "snapshot"
            snapshot.mkdir()
            subprocess.run(
                ["git", "archive", "--format=zip", "-o", str(archive), tree],
                cwd=root, capture_output=True, check=True, timeout=30,
            )
            with zipfile.ZipFile(archive) as source:
                for item in source.namelist():
                    target = (snapshot / item).resolve()
                    if not target.is_relative_to(snapshot.resolve()):
                        raise RuntimeError("Invalid archive path")
                source.extractall(snapshot)
            result = subprocess.run(
                json.loads(command_json), cwd=snapshot, capture_output=True,
                text=True, errors="replace", timeout=timeout,
            )
            code = result.returncode
            output = (result.stdout + "\n" + result.stderr)[:100_000]
    except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        output = f"{type(exc).__name__}"
    digest = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()
    with Store(root, "git") as store:
        status = "passed" if code == 0 else "failed"
        store.db.execute(
            """UPDATE git_test_jobs SET status=?, exit_code=?, output_sha256=?,
               finished_at=CURRENT_TIMESTAMP WHERE id=?""",
            (status, code, digest, job_id),
        )
        store.record(
            "git_test_completed",
            {"job_id": job_id, "status": status, "staged_tree": tree, "exit_code": code},
        )
    return status


def jobs(root: Path) -> list[dict]:
    with Store(root, "git") as store:
        store.db.executescript(JOBS_SCHEMA)
        rows = store.db.execute(
            """SELECT id, staged_tree, status, exit_code, created_at, finished_at
               FROM git_test_jobs ORDER BY id DESC LIMIT 30"""
        ).fetchall()
    return [
        dict(zip(("id", "staged_tree", "status", "exit_code", "created_at", "finished_at"), row))
        for row in rows
    ]


def worker_cli(args: list[str]) -> None:
    if len(args) != 3 or args[0] != "worker":
        raise SystemExit("Usage: python -m codex_hooks.git_bridge worker ROOT JOB_ID")
    status = execute_job(Path(args[1]), int(args[2]))
    print(status)


def _unused_imports_anchor() -> None:
    """Unused optional names intentionally absent; keep module import-light."""
