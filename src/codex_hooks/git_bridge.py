"""Small Git-hook adapter: commit messages, staged formatting, worktree setup."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .hooks import config_for
from .state import Store, repository_root

GIT_HOOKS = ("commit-msg", "pre-commit", "post-checkout")
SUBJECT = re.compile(
    r"^(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
    r"(?:\([a-zA-Z0-9_./-]+\))?!?: .+"
)
SPECIAL_SUBJECT = ("Merge ", "Revert ", "fixup! ", "squash! ", "amend! ")
TICKET = re.compile(r"(?mi)^Ticket:\s*[A-Za-z0-9][A-Za-z0-9._-]*\s*$")


def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, errors="replace"
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result


def git_revision(root: Path) -> dict:
    """HEAD is only the parent at pre-commit time; staged_tree is the candidate."""
    head = git(root, "rev-parse", "--verify", "HEAD", check=False)
    branch = git(root, "symbolic-ref", "-q", "--short", "HEAD", check=False)
    return {
        "head": head.stdout.strip() if head.returncode == 0 else None,
        "staged_tree": git(root, "write-tree").stdout.strip(),
        "branch": branch.stdout.strip() if branch.returncode == 0 else None,
        "worktree": str(root.resolve()),
    }


def commit_msg(root: Path, message_path: Path) -> None:
    options = config_for(root).get("git", {}).get("commit_msg", {})
    message = message_path.read_text(encoding="utf-8-sig")
    active = [line.strip() for line in message.splitlines() if line.strip() and not line.startswith("#")]
    if not active:
        raise ValueError("Commit message is empty.")
    subject = active[0]
    if not (SUBJECT.fullmatch(subject) or subject.startswith(SPECIAL_SUBJECT)):
        raise ValueError(
            "Commit subject must be Conventional Commits format, e.g. "
            "'feat(hooks): add Git integration'."
        )
    if options.get("require_ticket", False) and not TICKET.search(message):
        raise ValueError("Commit message requires a 'Ticket: T-001' trailer.")


def staged_python_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"],
        cwd=root, capture_output=True, check=True,
    )
    return sorted(
        path for path in result.stdout.decode("utf-8", "surrogateescape").split("\0")
        if path.endswith((".py", ".pyi"))
    )


def format_staged(root: Path, config: dict) -> list[str]:
    """Format safely only when the staged file has no extra unstaged edits."""
    if config.get("git", {}).get("pre_commit", {}).get("format", True) is False:
        return []
    paths = staged_python_files(root)
    if not paths:
        return []
    for name in paths:
        candidate = root / name
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or not candidate.resolve().is_relative_to(root.resolve())
        ):
            raise ValueError(f"Cannot safely auto-format this staged file: {name}")
        if git(root, "diff", "--quiet", "--", name, check=False).returncode:
            raise ValueError(
                f"Staged Python file has additional unstaged edits: {name}. "
                "Format and stage it manually before committing."
            )
    if shutil.which("uv") is None:
        raise ValueError("uv is not on PATH; install uv to run Ruff formatting.")
    command = ["uv", "run", "--no-sync", "ruff", "format", "--", *paths]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            "Ruff formatting failed: " + (result.stderr.strip() or result.stdout.strip())
        )
    git(root, "add", "--", *paths)
    return paths


def pre_commit(root: Path) -> dict:
    config = config_for(root)
    files = format_staged(root, config)
    revision = git_revision(root)
    with Store(root, "git") as store:
        store.record("git_pre_commit", {**revision, "formatted": files})
    # Do not make tests part of the commit's critical path.
    from .git_jobs import enqueue_test

    job = enqueue_test(root, revision, config)
    return {**revision, "formatted": files, "test_job": job}


def linked_worktree(root: Path) -> bool:
    gitdir = git(root, "rev-parse", "--absolute-git-dir").stdout.strip()
    common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(gitdir).resolve() != Path(common).resolve()


def _sync_worktree(root: Path, config: dict) -> bool:
    if not (root / "pyproject.toml").is_file():
        return False
    if config.get("git", {}).get("post_checkout", {}).get("uv_sync", True) is False:
        return False
    if shutil.which("uv") is None:
        raise RuntimeError("New worktree needs uv sync, but uv is not on PATH")
    command = ["uv", "sync", *(["--locked"] if (root / "uv.lock").is_file() else [])]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(
            "Worktree uv sync failed: " + (result.stderr.strip() or result.stdout.strip())
        )
    return True


def post_checkout(root: Path, previous: str, current: str, branch_flag: str) -> dict:
    """Initialize each linked worktree once, not each file/branch checkout."""
    if branch_flag != "1" or not linked_worktree(root):
        return {"initialized": False, "reason": "not a linked-worktree checkout"}
    with Store(root, "git") as store:
        initialized = store.get("worktree_initialized")
    if initialized:
        return {"initialized": False, "reason": "already initialized"}
    synced = _sync_worktree(root, config_for(root))
    with Store(root, "git") as store:
        store.put("worktree_initialized", True)
        store.record("git_post_checkout", {
            "previous": previous, "current": current,
            "worktree": str(root), "uv_sync": synced,
        })
    return {"initialized": True, "uv_sync": synced}

def hook_script(name: str) -> str:
    return f'#!/bin/sh\nexec codex-hooks git {name} "$@"\n'


def _active_default_hook(root: Path) -> Path | None:
    path = Path(git(root, "rev-parse", "--git-path", "hooks").stdout.strip())
    path = path if path.is_absolute() else root / path
    return next(
        (candidate for name in GIT_HOOKS if (candidate := path / name).is_file()
         and os.access(candidate, os.X_OK)), None
    )


def install_git_hooks(root: Path) -> Path:
    """Opt-in installation; never silently replace existing hooks."""
    if not (root / ".git").exists() and git(
        root, "rev-parse", "--is-inside-work-tree"
    ).stdout.strip() != "true":
        raise ValueError("Not inside a Git working tree")

    directory = root / ".githooks"
    configured = git(root, "config", "--local", "--get", "core.hooksPath", check=False).stdout.strip()
    target_path = str(directory.resolve())
    if configured and configured != ".githooks" and Path(configured).resolve() != directory.resolve():
        raise FileExistsError(f"core.hooksPath already points to {configured}; not replacing.")

    existing = _active_default_hook(root) if not configured else None
    if existing:
        raise FileExistsError(
            f"Existing Git hook {existing} would be disabled; "
            "migrate or chain it explicitly before installing."
        )

    conflicting = next(
        (path for name in GIT_HOOKS if (path := directory / name).exists()
         and path.read_text(encoding="utf-8") != hook_script(name)), None
    )
    if conflicting:
        raise FileExistsError(f"{conflicting} exists and differs; not overwriting.")

    directory.mkdir(exist_ok=True)
    for name in GIT_HOOKS:
        target = directory / name
        target.write_text(hook_script(name), encoding="utf-8", newline="\n")
        if os.name != "nt":
            target.chmod(0o755)
    git(root, "config", "--local", "core.hooksPath", target_path)
    return directory

def run_git_hook(name: str, arguments: list[str]) -> dict | None:
    root = repository_root(str(Path.cwd()))
    arity = {"commit-msg": 1, "pre-commit": 0, "post-checkout": 3}
    if name not in arity:
        raise ValueError(f"Unsupported Git hook: {name}")
    if len(arguments) != arity[name]:
        raise ValueError(f"{name} requires {arity[name]} argument(s)")

    def validated_message() -> dict:
        commit_msg(root, Path(arguments[0]))
        return {"validated": True}

    handlers = {
        "commit-msg": validated_message,
        "pre-commit": lambda: pre_commit(root),
        "post-checkout": lambda: post_checkout(root, *arguments),
    }
    return handlers[name]()

def main() -> None:
    """Independent worker entrypoint uses the installed package interpreter."""
    from .git_jobs import worker_cli

    worker_cli(sys.argv[1:])


if __name__ == "__main__":
    main()
