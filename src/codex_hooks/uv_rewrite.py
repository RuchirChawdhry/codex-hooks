"""Translate simple pip commands to native uv operations safely."""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from pathlib import Path

TOOLS = frozenset({"Bash", "exec_command"})
PIP = re.compile(r"pip(?:3(?:\.\d+)?)?(?:\.exe)?", re.I)
PYTHON = re.compile(r"(?:python(?:3(?:\.\d+)?)?|py|pypy3?)(?:\.exe)?", re.I)
AT_START = re.compile(
    r"(?i)(?:^|&&|\|\||[;&|\n])\s*(?:(?:sudo|env)\s+)?"
    r"(?:pip(?:3(?:\.\d+)?)?(?:\.exe)?|(?:python(?:3(?:\.\d+)?)?|py)"
    r"(?:\.exe)?\s+-m\s+pip)\s+\w+"
)


def project_at(cwd: Path, root: Path) -> Path | None:
    """Find a nearest PEP 621 project within this repository, without executing code."""
    root, current = root.resolve(), cwd.resolve()
    if not current.is_relative_to(root):
        return None
    while True:
        path = current / "pyproject.toml"
        if path.is_file():
            try:
                with path.open("rb") as stream:
                    data = tomllib.load(stream)
                if isinstance(data.get("project"), dict):
                    return current
            except (OSError, ValueError):
                return None
        if current == root:
            return None
        current = current.parent


def tokens_of(command: str) -> list[str] | None:
    """Only a single literal shell invocation is eligible for automatic rewriting."""
    if any(c in command for c in ("$", chr(96), "\\", "\r", "\n", "\0", "%")):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    if not tokens or any(
        token and all(char in ";&|<>" for char in token) for token in tokens
    ):
        return None
    if any("#" in token for token in tokens):
        return None
    return tokens


def pip_args(tokens: list[str]) -> list[str] | None:
    if PIP.fullmatch(tokens[0]):
        return tokens[1:]
    if (
        PYTHON.fullmatch(tokens[0])
        and len(tokens) >= 3
        and tokens[1] == "-m"
        and tokens[2].lower() == "pip"
    ):
        return tokens[3:]
    return None


def packages(names: list[str]) -> bool:
    return bool(names) and all(
        name and not name.startswith("-") and name not in (".", "..")
        and not name.startswith(("./", "../"))
        for name in names
    )


def translate(args: list[str], *, project: bool, venv: bool) -> list[str] | None:
    if not args:
        return None
    action, rest = args[0].lower(), args[1:]
    if action in ("list", "freeze", "show", "check"):
        if action in ("freeze", "check") and rest:
            return None
        if action == "show" and not packages(rest):
            return None
        if action == "list" and rest not in ([], ["--format", "json"], ["--format=json"]):
            return None
        return ["uv", "pip", action, *rest]
    if action == "install":
        if len(rest) == 2 and rest[0] in ("-r", "--requirement"):
            if project:
                return ["uv", "add", "-r", rest[1]]
            return ["uv", "pip", "install", "-r", rest[1]] if venv else None
        if not packages(rest):
            return None
        if project:
            return ["uv", "add", *rest]
        return ["uv", "pip", "install", *rest] if venv else None
    if action == "uninstall":
        names = [name for name in rest if name not in ("-y", "--yes")]
        if not packages(names):
            return None
        if project:
            return ["uv", "remove", *names]
        return ["uv", "pip", "uninstall", *names] if venv else None
    return None


def deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def uv_policy(event: dict, root: Path) -> dict:
    """Replace tool_input.command for safe mappings, without executing any command."""
    tool_input = event.get("tool_input", {})
    if event.get("tool_name") not in TOOLS or not isinstance(tool_input, dict):
        return {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return {}
    tokens = tokens_of(command)
    args = pip_args(tokens) if tokens else None
    if args is None:
        if not AT_START.search(command):
            return {}
        return deny("Compound, nested, or dynamic pip command. Use an explicit uv command.")
    cwd = Path(str(event.get("cwd") or root)).resolve()
    is_project = project_at(cwd, root) is not None
    in_venv = (cwd / ".venv").is_dir() or (
        bool(os.environ.get("VIRTUAL_ENV")) and Path(os.environ["VIRTUAL_ENV"]).is_dir()
    )
    rewritten = translate(args, project=is_project, venv=in_venv)
    if rewritten is None:
        return deny(
            "No safe uv mapping. For a Python project use uv add, uv remove, or uv sync; "
            "otherwise use uv venv followed by uv pip for direct environment operations."
        )
    updated = dict(tool_input)
    updated["command"] = shlex.join(rewritten)
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": updated,
        }
    }
