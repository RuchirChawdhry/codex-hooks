"""Durable, non-blocking validation jobs for the exact staged Git tree."""

from __future__ import annotations

import hashlib
import json
import os
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
    runners = [
        (
            (root / "pyproject.toml").is_file()
            and ((root / "tests").is_dir() or (root / "pytest.ini").is_file()),
            ["uv", "run", "--group", "dev", "pytest", "-q"],
        ),
        ((root / "Cargo.toml").is_file(), ["cargo", "test"]),
        ((root / "go.mod").is_file(), ["go", "test", "./..."]),
    ]
    return next((command for matched, command in runners if matched), None)

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
    """Spawn a detached worker; no test execution in the Git hook process."""
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
        if os.name == "nt" else {"start_new_session": True}
    )
    subprocess.Popen(
        [sys.executable, "-m", "codex_hooks.git_bridge", "worker", str(root), str(job_id)],
        cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, close_fds=True, **options,
    )

def _run_snapshot(
    root: Path, job_id: int, tree: str, command: list[str], directory: Path, timeout: int
) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix=f"test-{job_id}-", dir=directory) as temp:
        location = Path(temp)
        archive, snapshot = location / "staged.zip", location / "snapshot"
        snapshot.mkdir()
        subprocess.run(
            ["git", "archive", "--format=zip", "-o", str(archive), tree],
            cwd=root, capture_output=True, check=True, timeout=30,
        )
        with zipfile.ZipFile(archive) as source:
            if any(
                not (snapshot / name).resolve().is_relative_to(snapshot.resolve())
                for name in source.namelist()
            ):
                raise RuntimeError("Invalid archive path")
            source.extractall(snapshot)
        result = subprocess.run(
            command, cwd=snapshot, capture_output=True,
            text=True, errors="replace", timeout=timeout,
        )
        return result.returncode, (result.stdout + "\n" + result.stderr)[:100_000]


def execute_job(root: Path, job_id: int, timeout: int = 600) -> str:
    """Validate an immutable staged-tree snapshot and persist the outcome."""
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
        directory = store.directory

    code, output = 1, ""
    try:
        code, output = _run_snapshot(
            root, job_id, tree, json.loads(command_json), directory, timeout
        )
    except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        output = type(exc).__name__

    status = "passed" if code == 0 else "failed"
    digest = hashlib.sha256(output.encode("utf-8", "replace")).hexdigest()
    with Store(root, "git") as store:
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

