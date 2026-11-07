"""Contract tests for all seven hook behaviors."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codex_hooks.cli import initialize, native_config
from codex_hooks.hooks import dispatch, parse_handoff
from codex_hooks.uv_rewrite import translate
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
        "PreToolUse", "PostToolUse", "PreCompact", "SessionStart", "SessionEnd",
        "SubagentStop", "Interrupt"
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


def create_python_project(workspace: Path) -> None:
    (workspace / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\n'
    )


def pretool(workspace: Path, command: str) -> dict:
    return event(
        workspace,
        "PreToolUse",
        tool_name="Bash",
        tool_input={"command": command, "timeout": 15000},
    )


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("pip install requests", "uv add requests"),
        ("pip3 install requests", "uv add requests"),
        ("python -m pip install requests", "uv add requests"),
        ("py -m pip install pytest", "uv add pytest"),
        ('pip install "requests>=2"', "uv add 'requests>=2'"),
        ("pip install requests httpx", "uv add requests httpx"),
        ("pip install -r requirements.txt", "uv add -r requirements.txt"),
        ("pip uninstall requests", "uv remove requests"),
        ("python -m pip uninstall -y requests", "uv remove requests"),
        ("pip list", "uv pip list"),
        ("pip list --format=json", "uv pip list --format=json"),
        ("pip freeze", "uv pip freeze"),
        ("pip show requests", "uv pip show requests"),
        ("pip check", "uv pip check"),
    ],
)
def test_project_native_uv_rewrites(
    workspace: Path, original: str, expected: str
) -> None:
    create_python_project(workspace)
    result = dispatch(pretool(workspace, original))["hookSpecificOutput"]
    assert result["permissionDecision"] == "allow"
    assert result["updatedInput"]["command"] == expected
    assert result["updatedInput"]["timeout"] == 15000


@pytest.mark.parametrize(
    "command",
    [
        "pip install -U requests",
        "pip install --user requests",
        "pip install -e .",
        "pip install requests && echo done",
        "pip uninstall --all requests",
        "pip install requests > file.txt",
    ],
)
def test_ambiguous_or_compound_pip_is_not_silently_rewritten(
    workspace: Path, command: str
) -> None:
    create_python_project(workspace)
    result = dispatch(pretool(workspace, command))["hookSpecificOutput"]
    assert result["permissionDecision"] == "deny"
    assert "updatedInput" not in result


@pytest.mark.parametrize("command", ["echo pip install requests", "uv add requests", "git status"])
def test_unrelated_commands_pass_through(workspace: Path, command: str) -> None:
    assert dispatch(pretool(workspace, command)) == {}


def test_environment_only_uses_uv_pip(workspace: Path) -> None:
    (workspace / ".venv").mkdir()
    result = dispatch(pretool(workspace, "pip install httpx"))["hookSpecificOutput"]
    assert result["updatedInput"]["command"] == "uv pip install httpx"


def test_no_project_or_venv_requires_explicit_uv_setup(workspace: Path) -> None:
    result = dispatch(pretool(workspace, "pip install httpx"))["hookSpecificOutput"]
    assert result["permissionDecision"] == "deny"
    assert "uv venv" in result["permissionDecisionReason"]


def test_python_policy_can_be_disabled(workspace: Path) -> None:
    create_python_project(workspace)
    (workspace / ".codex-hooks.toml").write_text("[python]\nuv_only = false\n")
    assert dispatch(pretool(workspace, "pip install httpx")) == {}


def test_environment_detection_without_executing_project(workspace: Path) -> None:
    create_python_project(workspace)
    (workspace / "uv.lock").write_text("")
    (workspace / "tests").mkdir()
    response = dispatch(event(workspace, "SessionStart", source="startup"))
    assert "Python dependency operations" in response["hookSpecificOutput"]["additionalContext"]
    with Store(repository_root(str(workspace)), "session-A") as store:
        env = store.get("environment")
        assert "python" in env["languages"]
        assert env["package_managers"]["python"] == "uv"
        assert "pytest" in env["test_runners"]


def test_rolling_context_checkpoint_even_without_compaction(workspace: Path) -> None:
    dispatch(event(workspace, "SessionStart", source="startup"))
    (workspace / "changed.py").write_text("x=2")
    dispatch(tool(workspace, "echo ok"))
    with Store(repository_root(str(workspace)), "session-A") as store:
        saved = store.get("checkpoint")
        assert "changed.py" in saved["changed_files"]
        assert len(store.recent("context_checkpoint")) == 1
    dispatch(tool(workspace, "echo ok"))
    with Store(repository_root(str(workspace)), "session-A") as store:
        assert len(store.recent("context_checkpoint")) == 1
    restored = dispatch(event(workspace, "SessionStart", source="resume"))
    assert "changed.py" in restored["hookSpecificOutput"]["additionalContext"]


def test_explicit_project_translation_requirements() -> None:
    assert translate(["install", "-r", "requirements.txt"], project=True, venv=False) == [
        "uv", "add", "-r", "requirements.txt"
    ]
    assert translate(["uninstall", "requests"], project=True, venv=False) == [
        "uv", "remove", "requests"
    ]
