"""Integration tests for the three selected Git hooks and durable test jobs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_hooks import git_bridge, git_jobs
from codex_hooks.state import Store, repository_root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    monkeypatch.setenv("CODEX_HOOKS_STATE_DIR", str(tmp_path / "state"))
    return root


def stage(root: Path, name: str, contents: str) -> None:
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(contents)
    subprocess.run(["git", "add", "--", name], cwd=root, check=True)


def test_commit_msg_requires_conventional_subject(repo: Path) -> None:
    path = repo / "message.txt"
    path.write_text("feat(git): add hooks\n\nTicket: T-001\n")
    git_bridge.commit_msg(repo, path)
    path.write_text("did stuff\n")
    with pytest.raises(ValueError, match="Conventional Commits"):
        git_bridge.commit_msg(repo, path)


@pytest.mark.parametrize(
    "message",
    ["Merge branch 'main'", "fixup! feat: update", "Revert \"feat: old\""],
)
def test_commit_msg_accepts_common_git_messages(repo: Path, message: str) -> None:
    path = repo / "message.txt"
    path.write_text(message + "\n")
    git_bridge.commit_msg(repo, path)


def test_commit_msg_optional_ticket_requirement(repo: Path) -> None:
    (repo / ".codex-hooks.toml").write_text("[git.commit_msg]\nrequire_ticket = true\n")
    path = repo / "message.txt"
    path.write_text("fix: test hooks\n")
    with pytest.raises(ValueError, match="Ticket"):
        git_bridge.commit_msg(repo, path)
    path.write_text("fix: test hooks\n\nTicket: T-002\n")
    git_bridge.commit_msg(repo, path)


def test_revision_is_staged_tree_before_commit(repo: Path) -> None:
    stage(repo, "feature.txt", "staged\n")
    value = git_bridge.git_revision(repo)
    assert value["head"] is None
    assert len(value["staged_tree"]) in (40, 64)
    assert value["worktree"] == str(repo.resolve())
    (repo / "feature.txt").write_text("unstaged\n")
    assert git_bridge.git_revision(repo)["staged_tree"] == value["staged_tree"]


def test_precommit_format_then_record_and_queue(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "app.py", "x=1\n")
    captured = {}
    actual_run = subprocess.run

    def simulate_uv(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command[:5] == ["uv", "run", "--no-sync", "ruff", "format"]:
            (repo / "app.py").write_text("x = 1\n")
            return subprocess.CompletedProcess(command, 0, "", "")
        return actual_run(command, **kwargs)

    monkeypatch.setattr(git_bridge.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(git_bridge.subprocess, "run", simulate_uv)

    def collect(_root: Path, revision: dict, _config: dict) -> int:
        captured.update(revision)
        return 42

    monkeypatch.setattr(git_jobs, "enqueue_test", collect)
    result = git_bridge.pre_commit(repo)
    assert result["test_job"] == 42
    assert result["formatted"] == ["app.py"]
    assert captured["staged_tree"] == git_bridge.git_revision(repo)["staged_tree"]
    indexed = git_bridge.git(repo, "show", ":app.py").stdout
    assert indexed == "x = 1\n"
    with Store(repository_root(str(repo)), "git") as store:
        records = store.recent("git_pre_commit")
        assert records[0]["staged_tree"] == captured["staged_tree"]


def test_precommit_preserves_partial_staging(repo: Path) -> None:
    stage(repo, "app.py", "x=1\n")
    (repo / "app.py").write_text("x=1\ny=2\n")
    with pytest.raises(ValueError, match="unstaged edits"):
        git_bridge.format_staged(repo, {})
    assert git_bridge.git(repo, "show", ":app.py").stdout == "x=1\n"
    assert (repo / "app.py").read_text() == "x=1\ny=2\n"


def test_precommit_without_python_records_revision(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "readme.md", "hello\n")
    monkeypatch.setattr(git_jobs, "enqueue_test", lambda *_: None)
    result = git_bridge.pre_commit(repo)
    assert result["formatted"] == []
    assert result["test_job"] is None
    assert result["staged_tree"]


def test_installer_is_idempotent_and_does_not_replace_hooks(repo: Path) -> None:
    directory = git_bridge.install_git_hooks(repo)
    assert set(path.name for path in directory.iterdir()) == {
        "commit-msg", "pre-commit", "post-checkout"
    }
    assert git_bridge.git(repo, "config", "--local", "--get", "core.hooksPath").stdout.strip() == str(directory.resolve())
    assert git_bridge.install_git_hooks(repo) == directory
    # A different linked worktree can still resolve the shared wrappers.
    sibling = repo.parent / "linked"
    original_hooks_path = str(directory.resolve())
    git_bridge.git(repo, "config", "--local", "--unset", "core.hooksPath")
    git_bridge.git(repo, "commit", "--allow-empty", "-m", "chore: initialize repo")
    git_bridge.git(repo, "config", "--local", "core.hooksPath", original_hooks_path)
    git_bridge.git(repo, "worktree", "add", "--no-checkout", "--detach", str(sibling))
    assert (Path(git_bridge.git(sibling, "config", "--get", "core.hooksPath").stdout.strip()) / "post-checkout").exists()
    if os.name != "nt":
        assert os.access(directory / "pre-commit", os.X_OK)
    (directory / "pre-commit").write_text("echo different")
    with pytest.raises(FileExistsError):
        git_bridge.install_git_hooks(repo)


def test_installer_respects_custom_hooks_path(repo: Path) -> None:
    git_bridge.git(repo, "config", "--local", "core.hooksPath", "custom-hooks")
    with pytest.raises(FileExistsError, match="core.hooksPath"):
        git_bridge.install_git_hooks(repo)


def test_post_checkout_ignores_regular_worktree_and_file_checkout(repo: Path) -> None:
    assert not git_bridge.post_checkout(repo, "old", "new", "1")["initialized"]
    assert not git_bridge.post_checkout(repo, "old", "new", "0")["initialized"]


def test_new_linked_worktree_runs_uv_sync_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "pyproject.toml").write_text('[project]\nname = "demo"\nversion = "0.1"\n')
    (repo / "uv.lock").write_text("")
    monkeypatch.setattr(git_bridge, "linked_worktree", lambda root: True)
    monkeypatch.setattr(git_bridge.shutil, "which", lambda name: "/bin/uv")
    calls = []
    original = subprocess.run

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        if command and command[0] == "uv":
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")
        return original(command, **kwargs)

    monkeypatch.setattr(git_bridge.subprocess, "run", fake_run)
    assert git_bridge.post_checkout(repo, "old", "new", "1") == {
        "initialized": True, "uv_sync": True
    }
    assert calls == [["uv", "sync", "--locked"]]
    assert not git_bridge.post_checkout(repo, "old", "new", "1")["initialized"]
    assert calls == [["uv", "sync", "--locked"]]
    with Store(repo, "git") as store:
        assert store.get("worktree_initialized") is True


def test_test_worker_uses_staged_tree_not_later_worktree(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "sample.txt", "STAGED")
    revision = git_bridge.git_revision(repo)
    config = {
        "git": {
            "tests": {
                "command": [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('sample.txt').read_text() == 'STAGED'",
                ],
            }
        }
    }
    spawned = []
    monkeypatch.setattr(git_jobs, "start_worker", lambda root, job: spawned.append(job))
    job = git_jobs.enqueue_test(repo, revision, config)
    assert job is not None
    assert spawned == [job]
    (repo / "sample.txt").write_text("CHANGED AFTER HOOK")
    assert git_jobs.execute_job(repo, job) == "passed"
    rows = git_jobs.jobs(repo)
    assert rows[0]["status"] == "passed"
    assert rows[0]["staged_tree"] == revision["staged_tree"]
    assert git_jobs.execute_job(repo, job) == "not-queued"


def test_queue_deduplicates_equivalent_staged_trees(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "foo.txt", "content")
    revision = git_bridge.git_revision(repo)
    config = {"git": {"tests": {"command": [sys.executable, "-c", "print('done')"]}}}
    spawned = []
    monkeypatch.setattr(git_jobs, "start_worker", lambda root, job: spawned.append(job))
    first = git_jobs.enqueue_test(repo, revision, config)
    second = git_jobs.enqueue_test(repo, revision, config)
    assert first == second
    assert spawned == [first]
    assert len(git_jobs.jobs(repo)) == 1


def test_worker_failure_does_not_block_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage(repo, "test.txt", "contents")
    config = {"git": {"tests": {"command": [sys.executable, "-c", "raise SystemExit(7)"]}}}
    monkeypatch.setattr(git_jobs, "start_worker", lambda *_: None)
    job = git_jobs.enqueue_test(repo, git_bridge.git_revision(repo), config)
    assert git_jobs.execute_job(repo, job) == "failed"
    assert git_jobs.jobs(repo)[0]["exit_code"] == 7



@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("does-not-exist", []),
        ("commit-msg", []),
        ("pre-commit", ["extra"]),
        ("post-checkout", ["old", "new"]),
    ],
)
def test_git_hook_dispatch_rejects_unknown_events_or_wrong_arity(
    repo: Path, monkeypatch: pytest.MonkeyPatch, name: str, arguments: list[str]
) -> None:
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError):
        git_bridge.run_git_hook(name, arguments)


def test_git_commit_msg_dispatch_table(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(repo)
    message = repo / "commit-message.txt"
    message.write_text("feat(git): dispatch hooks\n")
    assert git_bridge.run_git_hook("commit-msg", [str(message)]) == {"validated": True}
