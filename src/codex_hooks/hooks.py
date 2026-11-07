"""Seven composable hook behaviors driven by native Codex lifecycle payloads."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tomllib
from pathlib import Path

from .state import Store, repository_root
from .extended import context_checkpoint, detect_environment, environment_context, uv_policy

EVENTS = ("PreToolUse", "PostToolUse", "PreCompact", "SessionStart", "SessionEnd", "SubagentStop", "Interrupt")
HOOKS = (
    "change_tracker",
    "failure_loop",
    "context_snapshot",
    "context_restore",
    "session_summary",
    "agent_handoff",
    "test_failure",
    "context_checkpoint",
    "environment_detection",
    "uv_guard",
)
TEST_COMMAND = re.compile(
    r"\b(?:pytest|unittest|vitest|jest|mocha|cargo\s+test|go\s+test|"
    r"npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|bun\s+test)\b",
    re.I,
)
EXIT_CODE = re.compile(r"(?:exit(?:ed)?\s*(?:with)?\s*code|exit_code)\s*[:=]?\s*(-?\d+)", re.I)
TEST_ID = re.compile(r"(?m)^(?:FAILED|ERROR)\s+([\w./:\\-]+)")
SECRETISH = re.compile(r"(?i)(?:password|token|secret|api[_-]?key)\s*[:=]\s*\S+")


def config_for(root: Path) -> dict:
    path = root / ".codex-hooks.toml"
    if not path.is_file():
        return {}
    with path.open("rb") as stream:
        return tomllib.load(stream)


def enabled(config: dict, name: str) -> bool:
    return config.get("hooks", {}).get(name, True) is not False


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:20]


def git_worktree(root: Path) -> dict[str, dict]:
    """Best-effort worktree observation, including pre-existing dirty files."""
    try:
        output = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=3,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    entries = output.split("\0")
    files: dict[str, dict] = {}
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            index += 1  # -z format supplies the original name as a second entry
        if len(files) >= 200:
            break
        item: dict = {"status": status}
        candidate = root / name
        try:
            # Never follow symlinks or read files outside the repository.
            if candidate.is_file() and not candidate.is_symlink() and candidate.stat().st_size <= 2_000_000:
                with candidate.open("rb") as stream:
                    item["digest"] = hashlib.sha256(stream.read()).hexdigest()[:16]
        except OSError:
            pass
        files[name] = item
    return files


def change_tracker(store: Store) -> None:
    previous = store.get("worktree")
    current = git_worktree(store.root)
    if previous is not None:
        changed = sorted(
            name for name in set(previous) | set(current) if previous.get(name) != current.get(name)
        )
        if changed:
            store.record("change_tracker", {"files": changed[:200]})
    store.put("worktree", current)


def command_of(event: dict) -> str:
    value = event.get("tool_input", {})
    if not isinstance(value, dict):
        return ""
    command = value.get("command", value.get("cmd", ""))
    return " ".join(map(str, command)) if isinstance(command, list) else str(command)


def response_text(response: object) -> str:
    if isinstance(response, str):
        return response[:100_000]
    if isinstance(response, dict):
        # Prefer well-known output fields; avoid serializing unrelated metadata.
        values = [
            response[key]
            for key in ("output", "stdout", "stderr", "content", "error")
            if key in response
        ]
        return " ".join(map(str, values))[:100_000]
    return str(response)[:100_000]


def result_code(response: object, text: str) -> int | None:
    if isinstance(response, dict):
        for key in ("exit_code", "exitCode", "returncode"):
            value = response.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
        if response.get("isError") is True:
            return 1
    match = EXIT_CODE.search(text)
    return int(match.group(1)) if match else None


def test_failure(store: Store, event: dict, command: str, output: str, code: int | None) -> None:
    if not TEST_COMMAND.search(command) or code is None or code == 0:
        return
    identifiers = list(dict.fromkeys(TEST_ID.findall(output)))[:8]
    record = {
        "runner": TEST_COMMAND.search(command).group(0).lower(),
        "exit_code": code,
        "tests": identifiers,
        "command_hash": fingerprint(command),
    }
    store.record("test_failure", record)


def failure_loop(store: Store, event: dict, command: str, output: str, code: int | None, config: dict) -> str | None:
    if not command:
        return None
    key = fingerprint(str(event.get("tool_name", "")) + ":" + command.strip())
    all_loops = store.get("failure_loops", {})
    previous = all_loops.get(key, {})
    if code is None or code == 0:
        if key in all_loops:
            del all_loops[key]
            store.put("failure_loops", all_loops)
        return None
    # Store only fingerprints of potentially sensitive command/output.
    signature = fingerprint(f"{code}:{SECRETISH.sub('[redacted]', output)[:1500]}")
    streak = previous.get("streak", 0) + 1 if previous.get("signature") == signature else 1
    all_loops[key] = {"signature": signature, "streak": streak}
    store.put("failure_loops", all_loops)
    threshold = max(2, int(config.get("failure_loop", {}).get("threshold", 2)))
    if streak == threshold:
        store.record("failure_loop", {"command_hash": key, "streak": streak})
        return f"The same tool command has failed {streak} times with the same error fingerprint. Change strategy: inspect the underlying cause instead of repeating the command."
    return None


def checkpoint(store: Store) -> dict:
    current = store.get("worktree", {})
    return {
        "changed_files": sorted(current)[:30],
        "test_failures": store.recent("test_failure", 5),
        "failure_loops": store.recent("failure_loop", 3),
        "handoffs": store.recent("agent_handoff", 5),
        "environment": store.get("environment", {}),
    }


def context_snapshot(store: Store, event: dict) -> None:
    change_tracker(store)
    saved = checkpoint(store)
    store.put("checkpoint", saved)
    store.record("context_snapshot", {"trigger": event.get("trigger", "unknown")})


def context_restore(store: Store, event: dict) -> dict:
    if event.get("source") not in ("resume", "compact"):
        return {}
    saved = store.get("checkpoint")
    if not saved:
        return {}
    concise = json.dumps(saved, separators=(",", ":"), ensure_ascii=False)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "Codex Hooks restored the following observable session checkpoint. "
                "It is evidence from the local worktree and hook records, not a transcript "
                "or a substitute for the user's instructions: " + concise[:3500]
            ),
        }
    }


def parse_handoff(message: object) -> dict | None:
    if not isinstance(message, str):
        return None
    trimmed = message.strip()
    match = re.fullmatch(chr(96) * 3 + r"(?:json)?\s*(.*?)\s*" + chr(96) * 3, trimmed, flags=re.S)
    if match:
        trimmed = match.group(1)
    try:
        data = json.loads(trimmed)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("ticket_id"), str) or not data["ticket_id"]:
        return None
    if data.get("status") not in ("done", "partial", "blocked", "failed"):
        return None
    if any(not isinstance(data.get(key, []), list) for key in ("changed", "tests", "blockers")):
        return None
    return data


def agent_handoff(store: Store, event: dict, config: dict) -> dict:
    handoff = parse_handoff(event.get("last_assistant_message"))
    data = {
        "agent_id": str(event.get("agent_id", "")),
        "agent_type": str(event.get("agent_type", "")),
        "ticket_id": handoff["ticket_id"] if handoff else None,
        "status": handoff["status"] if handoff else "unstructured",
        "changed": handoff.get("changed", [])[:30] if handoff else [],
        "tests": handoff.get("tests", [])[:20] if handoff else [],
        "blockers": handoff.get("blockers", [])[:10] if handoff else [],
    }
    store.record("agent_handoff", data)
    if (
        not handoff
        and config.get("agent_handoff", {}).get("strict", False)
        and not event.get("stop_hook_active", False)
    ):
        return {
            "decision": "block",
            "reason": (
                "Return a single JSON handoff with ticket_id (string), "
                "status (done|partial|blocked|failed), and optional changed/tests/blockers arrays."
            ),
        }
    return {}


def session_summary(store: Store, event: dict) -> None:
    summary = {
        "session_id": store.session,
        "project": str(store.root),
        "reason": event.get("reason"),
        "counts": store.counts(),
        "checkpoint": checkpoint(store),
    }
    path = store.save_summary(summary)
    store.record("session_summary", {"file": str(path)})


def dispatch(event: dict) -> dict:
    """Evaluate a single Codex event; return native response JSON."""
    name = event.get("hook_event_name")
    if name not in EVENTS:
        return {}
    root = repository_root(str(event.get("cwd") or Path.cwd()))
    config = config_for(root)
    session = str(event.get("session_id") or "unknown")
    with Store(root, session) as store:
        if name == "PreToolUse":
            if enabled(config, "uv_guard") and config.get("python", {}).get("uv_only", True):
                return uv_policy(event.get("tool_name"), command_of(event))
            return {}

        if name == "SessionStart":
            if enabled(config, "change_tracker") and store.get("worktree") is None:
                store.put("worktree", git_worktree(root))
            contexts: list[str] = []
            if enabled(config, "environment_detection"):
                environment = detect_environment(root)
                store.put("environment", environment)
                contexts.append(environment_context(environment, enabled(config, "uv_guard")))
            if enabled(config, "context_restore"):
                restored = context_restore(store, event)
                if restored:
                    contexts.append(restored["hookSpecificOutput"]["additionalContext"])
            if contexts:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": "\n".join(contexts)[:4200],
                    }
                }
            return {}

        if name == "PostToolUse":
            if enabled(config, "change_tracker"):
                change_tracker(store)
            command = command_of(event)
            output = response_text(event.get("tool_response", {}))
            code = result_code(event.get("tool_response"), output)
            if enabled(config, "test_failure"):
                test_failure(store, event, command, output, code)
            feedback = None
            if enabled(config, "failure_loop"):
                feedback = failure_loop(store, event, command, output, code, config)
            if enabled(config, "context_checkpoint"):
                if not enabled(config, "change_tracker"):
                    store.put("worktree", git_worktree(root))
                context_checkpoint(store, checkpoint(store), "tool")
            if feedback:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": feedback,
                    }
                }
            return {}

        if name == "PreCompact":
            if enabled(config, "context_snapshot"):
                context_snapshot(store, event)
            if enabled(config, "context_checkpoint"):
                context_checkpoint(store, checkpoint(store), "precompact")
        elif name == "SubagentStop":
            result = agent_handoff(store, event, config) if enabled(config, "agent_handoff") else {}
            if enabled(config, "context_checkpoint"):
                context_checkpoint(store, checkpoint(store), "subagent")
            return result
        elif name == "Interrupt":
            if enabled(config, "context_checkpoint"):
                # Persist the latest observed evidence, even if compaction never occurs.
                context_checkpoint(store, checkpoint(store), "interrupt")
        elif name == "SessionEnd":
            if enabled(config, "context_checkpoint"):
                context_checkpoint(store, checkpoint(store), "session_end")
            if enabled(config, "session_summary"):
                session_summary(store, event)
    return {}
