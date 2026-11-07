"""Small SQLite event store; state stays outside the project and repository."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path


def repository_root(cwd: str) -> Path:
    path = Path(cwd).resolve()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
        return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError):
        return path


def state_directory(root: Path) -> Path:
    configured = os.environ.get("CODEX_HOOKS_STATE_DIR")
    if configured:
        base = Path(configured).expanduser()
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "codex-hooks"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        base /= "codex-hooks"
    digest = hashlib.sha256(os.path.normcase(str(root)).encode()).hexdigest()[:20]
    path = base / digest
    path.mkdir(parents=True, exist_ok=True)
    return path


class Store:
    def __init__(self, root: Path, session: str):
        self.root = root
        self.session = session
        self.directory = state_directory(root)
        self.db = sqlite3.connect(self.directory / "state.sqlite3", timeout=3)
        self.db.execute("PRAGMA busy_timeout=3000")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY,
                session TEXT NOT NULL,
                kind TEXT NOT NULL,
                value TEXT NOT NULL,
                at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS events_lookup
                ON events(session, kind, id);
            CREATE TABLE IF NOT EXISTS kv (
                session TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY(session, key)
            );
            """
        )

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.db.commit()
        self.db.close()

    def put(self, key: str, value: object) -> None:
        self.db.execute(
            """INSERT INTO kv (session, key, value) VALUES (?, ?, ?)
               ON CONFLICT(session, key) DO UPDATE SET value=excluded.value""",
            (self.session, key, json.dumps(value, ensure_ascii=False)),
        )

    def get(self, key: str, default: object = None) -> object:
        row = self.db.execute(
            "SELECT value FROM kv WHERE session=? AND key=?", (self.session, key)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def record(self, kind: str, value: object) -> None:
        self.db.execute(
            "INSERT INTO events (session, kind, value) VALUES (?, ?, ?)",
            (self.session, kind, json.dumps(value, ensure_ascii=False)),
        )

    def recent(self, kind: str, limit: int = 10) -> list[dict]:
        rows = self.db.execute(
            "SELECT value FROM events WHERE session=? AND kind=? ORDER BY id DESC LIMIT ?",
            (self.session, kind, limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT kind, COUNT(*) FROM events WHERE session=? GROUP BY kind", (self.session,)
        ).fetchall()
        return dict(rows)

    def summary_path(self) -> Path:
        digest = hashlib.sha256(self.session.encode()).hexdigest()[:24]
        directory = self.directory / "summaries"
        directory.mkdir(exist_ok=True)
        return directory / f"{digest}.json"

    def save_summary(self, value: dict) -> Path:
        import tempfile

        destination = self.summary_path()
        with tempfile.NamedTemporaryFile(
            "w", dir=destination.parent, suffix=".tmp", encoding="utf-8", delete=False
        ) as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            temporary = Path(handle.name)
        os.replace(temporary, destination)
        return destination
