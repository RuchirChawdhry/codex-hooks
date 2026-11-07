"""Rolling evidence checkpoints, lightweight environment discovery, and uv enforcement."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from .state import Store


def detect_environment(root: Path) -> dict:
    """Detect local language markers without executing project code."""
    markers = {
        "python": ("pyproject.toml", "uv.lock", "requirements.txt", "setup.py", "setup.cfg", ".python-version"),
        "javascript": ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"),
        "rust": ("Cargo.toml",),
        "go": ("go.mod",),
        "powershell": ("*.psd1", "*.psm1", "*.ps1"),
        "dotnet": ("*.sln", "*.csproj", "*.fsproj"),
        "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    }
    languages = {
        name for name, patterns in markers.items()
        if any(root.glob(pattern) for pattern in patterns)
    }
    if any(root.glob("*.py")) or any((root / "src").glob("*.py")):
        languages.add("python")
    js_managers = {
        "bun": ("bun.lock", "bun.lockb"),
        "pnpm": ("pnpm-lock.yaml",),
        "npm": ("package-lock.json",),
        "yarn": ("yarn.lock",),
    }
    javascript = next(
        (name for name, patterns in js_managers.items()
         if any((root / pattern).exists() for pattern in patterns)), None
    )
    managers = {"python": "uv" if "python" in languages else None, "javascript": javascript}
    pytest = (root / "pytest.ini").exists() or (
        (root / "tests").is_dir() and "python" in languages
    )
    return {
        "languages": sorted(languages),
        "package_managers": {name: manager for name, manager in managers.items() if manager},
        "uv_available": shutil.which("uv") is not None,
        "test_runners": ["pytest"] if pytest else [],
    }


def environment_context(environment: dict, uv_guard_enabled: bool) -> str:
    languages = environment["languages"]
    if not languages:
        return ""
    python = "python" in languages
    javascript = environment["package_managers"].get("javascript")
    optional = [
        (
            python,
            "Use uv for all Python dependency operations: uv sync, uv add, "
            "uv remove, uv run, and uv pip when an imperative pip-compatible operation is needed.",
        ),
        (
            python and uv_guard_enabled,
            "Direct pip invocations are rewritten into uv operations when safe; "
            "unsupported forms are denied.",
        ),
        (
            python and not environment["uv_available"],
            "uv was not found on PATH; install uv before managing Python dependencies.",
        ),
        (bool(javascript), f"JavaScript package manager: {javascript}."),
    ]
    return " ".join([
        f"Detected repository languages: {', '.join(languages)}.",
        *(text for condition, text in optional if condition),
    ])

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


