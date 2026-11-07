"""Contract tests for all seven hook behaviors."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codex_hooks.cli import initialize, native_config
from codex_hooks.hooks import dispatch, parse_handoff
from codex_hooks.state import Store, repository_root


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True, capture_output=True)
    monkeypatch.setenv("CODEX_HOOKS_STATE_DIR", str(tmp_path / "state"))
    return project


def event(root: Path, name: str, session: str = "session-A", **fields: object) -> dict:
    return {"session_id": session, "cwd": str(root), "hook_event_name": name, **fields}


def tool(root: Path, command: str = "pytest -q", exit_code: int = 0, output: str = "") -> dict:
    return event(
        root,
        "PostToolUse",
        tool_name="Bash",
        tool_input={"command": command},
        tool_response={"exit_code": exit_code, "stdout": output},
    )


def test_config_covers_native_events() -> None:
    config = native_config()
    assert set(config["hooks"]) == {
        "PostToolUse", "PreCompact", "SessionStart", "SessionEnd", "SubagentStop"
    }
    assert all(group[0]["hooks"][0]["type"] == "command" for group in config["hooks"].values())


def test_install_preserves_existing_configuration(workspace: Path) -> None:
    installed, settings = initialize(workspace)
    assert json.loads(installed.read_text()) == native_config()
    with pytest.raises(FileExistsError):
        initialize(workspace)
    settings.write_text("[agent_handoff]\nstrict = true\n")
    initialize(workspace, force=True)
    assert settings.read_text() == "[agent_handoff]\nstrict = true\n"


def test_change_tracker_and_context_restore(workspace: Path) -> None:
    dispatch(event(workspace, "SessionStart", source="startup"))
    (workspace / "feature.py").write_text("hello = True\n")
    dispatch(tool(workspace, "echo edited"))
    with Store(repository_root(str(workspace)), "session-A") as store:
        assert store.recent("change_tracker")[0]["files"] == ["feature.py"]
    dispatch(event(workspace, "PreCompact", trigger="auto"))
    restored = dispatch(event(workspace, "SessionStart", source="compact"))
    assert "feature.py" in restored["hookSpecificOutput"]["additionalContext"]


def test_change_tracker_does_not_misattribute_initial_dirty_files(workspace: Path) -> None:
    (workspace / "initial.py").write_text("1")
    dispatch(event(workspace, "SessionStart", source="startup"))
    dispatch(tool(workspace, "echo no changes"))
    with Store(repository_root(str(workspace)), "session-A") as store:
        assert store.recent("change_tracker") == []
    (workspace / "initial.py").write_text("2")
    dispatch(tool(workspace, "echo changed"))
    with Store(repository_root(str(workspace)), "session-A") as store:
        assert store.recent("change_tracker")[0]["files"] == ["initial.py"]


def test_failed_tests_are_recorded_without_raw_output(workspace: Path) -> None:
    dispatch(tool(
        workspace, "uv run pytest -q", 1,
        "FAILED tests/test_math.py::test_sum - AssertionError: password=secret",
    ))
    with Store(repository_root(str(workspace)), "session-A") as store:
        failures = store.recent("test_failure")
        assert failures[0]["tests"] == ["tests/test_math.py::test_sum"]
        assert failures[0]["exit_code"] == 1
        assert "password=secret" not in (store.directory / "state.sqlite3").read_bytes().decode(
            errors="ignore"
        )


def test_failure_loop_warns_once_and_resets(workspace: Path) -> None:
    assert dispatch(tool(workspace, "pytest", 1, "fatal: failed")) == {}
    notice = dispatch(tool(workspace, "pytest", 1, "fatal: failed"))
    assert "Change strategy" in notice["hookSpecificOutput"]["additionalContext"]
    assert dispatch(tool(workspace, "pytest", 1, "fatal: failed")) == {}
    assert dispatch(tool(workspace, "pytest", 0, "1 passed")) == {}
    assert dispatch(tool(workspace, "pytest", 1, "fatal: failed")) == {}
    with Store(repository_root(str(workspace)), "session-A") as store:
        assert len(store.recent("failure_loop")) == 1


def test_session_summary_saved_outside_repository(workspace: Path) -> None:
    dispatch(event(workspace, "SessionStart", source="startup"))
    (workspace / "new.txt").write_text("new")
    dispatch(tool(workspace, "echo wrote file"))
    dispatch(event(workspace, "SessionEnd", reason="other"))
    with Store(repository_root(str(workspace)), "session-A") as store:
        summary = json.loads(store.summary_path().read_text())
        assert "new.txt" in summary["checkpoint"]["changed_files"]
        assert summary["counts"]["change_tracker"] == 1
        assert summary["reason"] == "other"
        assert not str(store.summary_path()).startswith(str(workspace))


def test_agent_handoff_accepts_plain_and_fenced_json(workspace: Path) -> None:
    handoff = {"ticket_id": "T-007", "status": "done", "changed": ["src/a.py"], "tests": []}
    assert parse_handoff(json.dumps(handoff)) == handoff
    fenced = chr(96) * 3 + "json\n" + json.dumps(handoff) + "\n" + chr(96) * 3
    assert parse_handoff(fenced) == handoff
    result = dispatch(event(
        workspace, "SubagentStop", agent_id="agent-1", agent_type="worker",
        last_assistant_message=json.dumps(handoff),
    ))
    assert result == {}
    with Store(repository_root(str(workspace)), "session-A") as store:
        assert store.recent("agent_handoff")[0]["ticket_id"] == "T-007"


def test_invalid_handoff_is_advisory_until_strict_opt_in(workspace: Path) -> None:
    value = event(workspace, "SubagentStop", last_assistant_message="Completed.", agent_id="one")
    assert dispatch(value) == {}
    (workspace / ".codex-hooks.toml").write_text("[agent_handoff]\nstrict = true\n")
    assert dispatch(value)["decision"] == "block"
    assert dispatch({**value, "stop_hook_active": True}) == {}


def test_hooks_can_be_disabled_independently(workspace: Path) -> None:
    (workspace / ".codex-hooks.toml").write_text(
        "[hooks]\ntest_failure = false\nfailure_loop = false\n"
    )
    dispatch(tool(workspace, "pytest -q", 1, "FAILED tests/a.py::test_x"))
    with Store(repository_root(str(workspace)), "session-A") as store:
        assert store.recent("test_failure") == []
        assert store.recent("failure_loop") == []


def test_session_state_is_isolated(workspace: Path) -> None:
    dispatch(tool(workspace, "pytest", 1, "ERROR"))
    other = tool(workspace, "pytest", 1, "ERROR")
    other["session_id"] = "session-B"
    assert dispatch(other) == {}
    with Store(repository_root(str(workspace)), "session-B") as store:
        assert store.recent("failure_loop") == []
