"""Rolling evidence checkpoints, lightweight environment discovery, and uv enforcement."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
from pathlib import Path

from .state import Store

SHELL_TOOLS = frozenset({"Bash", "exec_command"})
PIP_COMMAND = re.compile(r"pip(?:3(?:\.\d+)?)?(?:\.exe)?$", re.I)
PYTHON_COMMAND = re.compile(r"(?:python(?:3(?:\.\d+)?)?|py|pypy3?)(?:\.exe)?$", re.I)
SHELL_COMMAND = frozenset({"bash", "sh", "zsh", "fish", "pwsh", "powershell", "cmd", "cmd.exe"})
PREFIX_COMMAND = frozenset({"sudo", "command", "env", "time", "nohup"})
SEPARATORS = frozenset({";", "&&", "||", "|", "&", "\n"})


def detect_environment(root: Path) -> dict:
    """Inspect a bounded set of project markers; never execute project code."""
    markers = {
        "python": ("pyproject.toml", "uv.lock", "requirements.txt", "setup.py", "setup.cfg", ".python-version"),
        "javascript": ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"),
        "rust": ("Cargo.toml",),
        "go": ("go.mod",),
        "powershell": ("*.psd1", "*.psm1", "*.ps1"),
        "dotnet": ("*.sln", "*.csproj", "*.fsproj"),
        "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    }
    languages = [
        name for name, patterns in markers.items()
        if any(bool(list(root.glob(pattern))) for pattern in patterns)
    ]
    if "python" not in languages and (
        any(root.glob("*.py")) or any((root / "src").glob("*.py"))
    ):
        languages.append("python")
    managers = {
        "python": "uv" if "python" in languages else None,
        "javascript": (
            "bun" if (root / "bun.lock").exists() or (root / "bun.lockb").exists()
            else "pnpm" if (root / "pnpm-lock.yaml").exists()
            else "npm" if (root / "package-lock.json").exists()
            else "yarn" if (root / "yarn.lock").exists() else None
        ),
    }
    return {
        "languages": sorted(languages),
        "package_managers": {key: val for key, val in managers.items() if val},
        "uv_available": shutil.which("uv") is not None,
        "test_runners": (
            ["pytest"] if (root / "pytest.ini").exists()
            or (root / "tests").is_dir() and "python" in languages
            else []
        ),
    }


def environment_context(environment: dict, uv_guard_enabled: bool) -> str:
    if not environment["languages"]:
        return ""
    languages = ", ".join(environment["languages"])
    message = f"Detected repository languages: {languages}."
    if "python" in environment["languages"]:
        message += (
            " Use uv for all Python dependency operations: uv sync, uv add, "
            "uv remove, uv run, and uv pip when an imperative pip-compatible operation is needed."
        )
        if uv_guard_enabled:
            message += " Direct pip invocations are denied by the uv_guard policy."
        if not environment["uv_available"]:
            message += " uv was not found on PATH; install uv before managing Python dependencies."
    if environment["package_managers"].get("javascript"):
        message += (
            " JavaScript package manager: "
            + environment["package_managers"]["javascript"] + "."
        )
    return message


def context_checkpoint(store: Store, value: dict, reason: str) -> bool:
    """Persist a checkpoint only when its material contents have changed."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = hashlib.sha256(payload.encode()).hexdigest()
    if store.get("checkpoint_signature") == signature:
        return False
    store.put("checkpoint", value)
    store.put("checkpoint_signature", signature)
    store.record(
        "context_checkpoint",
        {"reason": reason, "signature": signature[:16], "paths": len(value.get("changed_files", []))},
    )
    return True


def _basename(token: str) -> str:
    return token.strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()


def _unsafe_pip_command(command: str, depth: int = 0) -> bool:
    """Recognize direct pip invocations at shell-command positions, not in echo text."""
    if depth > 2:
        return False
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        # Can't reliably parse a malformed shell expression: do not claim coverage.
        return False
    index = 0
    while index < len(tokens):
        if tokens[index] in SEPARATORS:
            index += 1
            continue
        segment = []
        while index < len(tokens) and tokens[index] not in SEPARATORS:
            segment.append(tokens[index])
            index += 1
        if _unsafe_segment(segment, depth):
            return True
    return False


def _unsafe_segment(tokens: list[str], depth: int) -> bool:
    # Strip harmless command prefixes but don't mistake e.g. echo "pip install"
    # for an actual pip invocation.
    while tokens:
        name = _basename(tokens[0])
        if re.fullmatch(r"[A-Za-z_]\w*=.+", tokens[0]):
            tokens = tokens[1:]
            continue
        if name in PREFIX_COMMAND:
            tokens = tokens[1:]
            while tokens and (tokens[0].startswith("-") or "=" in tokens[0]):
                tokens = tokens[1:]
            continue
        break
    if not tokens:
        return False
    name = _basename(tokens[0])
    if PIP_COMMAND.fullmatch(name):
        return True
    if PYTHON_COMMAND.fullmatch(name):
        return any(
            tokens[i] == "-m" and _basename(tokens[i + 1]) == "pip"
            for i in range(1, min(len(tokens) - 1, 7))
        )
    if name == "uv" and len(tokens) > 2 and tokens[1] == "run":
        return _unsafe_segment(tokens[2:], depth + 1)
    if name in SHELL_COMMAND and depth < 2:
        for i, token in enumerate(tokens[1:], 1):
            if token.lower() in ("-c", "-lc", "-command", "/c") and i + 1 < len(tokens):
                return _unsafe_pip_command(tokens[i + 1], depth + 1)
    return False


def uv_policy(tool_name: object, command: str) -> dict:
    """Return native PreToolUse denial for direct pip; never silently rewrite commands."""
    if tool_name not in SHELL_TOOLS or not _unsafe_pip_command(command):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "Direct pip invocation is disabled. Use uv instead: "
                "uv add <package> (project dependencies), uv sync (project setup), "
                "uv remove <package>, uv run <command>, or uv pip <subcommand> "
                "(pip-compatible environment operations). Do not use pip or python -m pip."
            ),
        }
    }
