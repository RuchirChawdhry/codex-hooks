"""Install, inspect, and execute native Codex lifecycle hooks."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .hooks import EVENTS, HOOKS, dispatch

EXAMPLE_CONFIG = """[hooks]
change_tracker = true
failure_loop = true
context_snapshot = true
context_restore = true
session_summary = true
agent_handoff = true
test_failure = true
context_checkpoint = true
environment_detection = true
uv_rewrite = true

[failure_loop]
threshold = 2

[agent_handoff]
strict = false

[python]
uv_only = true
"""


def native_config() -> dict:
    return {
        "description": "Codex Hooks: change tracking, failure recovery, context, and handoffs",
        "hooks": {
            event: [{
                "hooks": [{
                    "type": "command",
                    "command": "codex-hooks hook",
                    "commandWindows": "codex-hooks hook",
                    "timeout": 2 if event in ("SessionEnd", "Interrupt") else 10,
                    "statusMessage": f"Codex Hooks: {event}",
                }],
            }] for event in EVENTS
        },
    }


def initialize(root: Path, force: bool = False) -> tuple[Path, Path]:
    hook_file = root / ".codex" / "hooks.json"
    config_file = root / ".codex-hooks.toml"
    if hook_file.exists() and not force:
        raise FileExistsError(f"{hook_file} exists; use --force to replace it")
    hook_file.parent.mkdir(parents=True, exist_ok=True)
    hook_file.write_text(json.dumps(native_config(), indent=2) + "\n", encoding="utf-8")
    if not config_file.exists():
        config_file.write_text(EXAMPLE_CONFIG, encoding="utf-8")
    return hook_file, config_file


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="codex-hooks")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="Read a native Codex JSON event from stdin")
    init = sub.add_parser("init", help="Install .codex/hooks.json into a repository")
    init.add_argument("--force", action="store_true", help="Replace existing hooks.json")
    sub.add_parser("doctor", help="Check executable and project wiring")
    sub.add_parser("list", help="List included modules")
    replay = sub.add_parser("replay", help="Replay an event JSON")
    replay.add_argument("path", type=Path)
    args = parser.parse_args(argv)

    if args.command == "hook":
        try:
            data = json.load(sys.stdin)
            if not isinstance(data, dict):
                raise ValueError("Codex hook input must be a JSON object")
            print(json.dumps(dispatch(data), separators=(",", ":")))
        except (OSError, ValueError, TypeError) as exc:
            print(f"codex-hooks: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    elif args.command == "init":
        try:
            hook, config = initialize(Path.cwd(), args.force)
        except FileExistsError as exc:
            parser.error(str(exc))
        print(f"Installed: {hook}\nConfiguration: {config}\nReview with /hooks in Codex.")
    elif args.command == "doctor":
        path = Path.cwd() / ".codex" / "hooks.json"
        print(json.dumps({
            "executable": shutil.which("codex-hooks"),
            "project_hooks": str(path) if path.is_file() else None,
            "events": EVENTS,
        }, indent=2))
    elif args.command == "list":
        print("\n".join(HOOKS))
    elif args.command == "replay":
        with args.path.open(encoding="utf-8") as source:
            event = json.load(source)
        if not isinstance(event, dict):
            parser.error("Event JSON must be an object")
        print(json.dumps(dispatch(event), indent=2))
