# codex-hooks

Modular, deterministic lifecycle hooks for OpenAI Codex. Version 0.1 focuses
on observability, debugging signals, continuity, and structured handoffs.

## The first seven hooks

| Behavior | Codex event | Purpose |
| --- | --- | --- |
| Change tracker | SessionStart, PostToolUse | Observe working-tree changes (including untracked files) |
| Failure-loop detector | PostToolUse | Fingerprint repeated failures; advise a different strategy |
| Context snapshot | PreCompact | Save compact structured evidence before compaction |
| Context restore | SessionStart (resume/compact) | Inject the latest checkpoint |
| Session summary | SessionEnd | Write a JSON session summary |
| Agent handoff | SubagentStop | Validate and record a subagent outcome |
| Test failure | PostToolUse | Record failing test IDs and exit codes |

These are deterministic handlers. They do not spawn another agent or model.

## Installation

Requirements: Python 3.11+, Git, and uv (https://docs.astral.sh/uv/).

From a checkout:

    uv tool install .

Once the initial version is merged, installation from GitHub will be:

    uv tool install git+https://github.com/RuchirChawdhry/codex-hooks

Inside a target Git repository:

    codex-hooks init
    codex-hooks doctor
    codex-hooks list

This creates a native .codex/hooks.json file and a .codex-hooks.toml file.
Existing hooks.json files are not overwritten without --force. An existing TOML
configuration is never replaced. Make sure the codex-hooks executable is on
the PATH used by Codex, and reload Codex after installation if necessary.

Codex requires you to review and trust non-managed hooks. Use /hooks in Codex
to review the installed hook definitions.

## Configuration

All seven modules are enabled by default. Copy and edit settings from
.codex-hooks.example.toml. Individual modules can be disabled in [hooks].

The failure-loop detector offers feedback on the second consecutive
identical failure of a command, without blocking the tool.

Subagent handoffs are advisory by default. To enforce them, set:

    [agent_handoff]
    strict = true

The handoff JSON should look like:

    {
      "ticket_id": "T-001",
      "status": "done",
      "changed": ["src/module.py"],
      "tests": ["pytest -q: passed"],
      "blockers": []
    }

Only ticket_id and status are required. Status values: done, partial,
blocked, failed. Optional changed/tests/blockers must be arrays.
Strict mode can request a single continuation and honors stop_hook_active.

## State, privacy, and caveats

State is kept OUTSIDE the working repository:

- Linux: XDG_STATE_HOME/codex-hooks/<repo-hash>/ (or ~/.local/state/)
- Windows: LOCALAPPDATA/codex-hooks/<repo-hash>/
- Override base directory: CODEX_HOOKS_STATE_DIR

SQLite stores structured event evidence and session-local checkpoints.
Summaries live under summaries/<session-hash>.json.
Raw transcripts and raw tool output are not copied into the database.
Failure text is fingerprinted; failing test IDs and file paths remain visible.

Change tracking compares worktree observations, not exact tool edit
provenance. Existing dirty paths are baselined when SessionStart fires.
Failure detection requires recognizable exit codes or an error flag.
SessionEnd is best-effort, especially after abrupt termination.
Checkpoints cannot reconstruct unrecorded design decisions or the conversation.
These hooks are not a security boundary, test suite, or autonomous orchestrator.

Some hosted Codex environments do not run local command hooks.
See the current Codex Hooks reference:
https://learn.chatgpt.com/docs/hooks

## Development

    uv sync --group dev
    task check

Or directly:

    uv run --group dev ruff check .
    uv run --group dev pytest -q

Replay one native Codex JSON event from a local file:

    codex-hooks replay event.json

Or stream one native payload:

    cat event.json | codex-hooks hook
