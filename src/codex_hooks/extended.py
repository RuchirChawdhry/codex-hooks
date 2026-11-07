"""Rolling evidence checkpoints, lightweight environment discovery, and uv enforcement."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .state import Store



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
            message += " Direct pip invocations are rewritten into uv operations when safe; unsupported forms are denied."
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


