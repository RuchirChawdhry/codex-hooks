# codex-hooks

Modular, deterministic lifecycle hooks for OpenAI Codex. Version 0.1 focuses
on observability, debugging signals, continuity, and structured handoffs.

## Included hooks

| Behavior | Codex event | Purpose |
| --- | --- | --- |
| Change tracker | SessionStart, PostToolUse | Observe working-tree changes (including untracked files) |
| Failure-loop detector | PostToolUse | Fingerprint repeated failures; advise a different strategy |
| Context snapshot | PreCompact | Save compact structured evidence before compaction |
| Context restore | SessionStart (resume/compact) | Inject the latest checkpoint |
| Session summary | SessionEnd | Write a JSON session summary |
| Agent handoff | SubagentStop | Validate and record a subagent outcome |
| Test failure | PostToolUse | Record failing test IDs and exit codes |
| Context checkpoint | PostToolUse, PreCompact, SubagentStop, Interrupt, SessionEnd | Save rolling evidence checkpoints |
| Environment detection | SessionStart | Detect languages, package managers, test runners |
| uv command rewrite | PreToolUse | Rewrite pip operations to native uv project commands |

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

All Codex lifecycle modules are enabled by default. Copy and edit settings from
.codex-hooks.example.toml. Individual modules can be disabled in [hooks].

The failure-loop detector offers feedback on the second consecutive
identical failure of a command, without blocking the tool.

In PEP 621 Python projects, PreToolUse automatically rewrites pip install requests
to uv add requests, pip uninstall requests to uv remove requests, and
pip install -r requirements.txt to uv add -r requirements.txt.
The inspection commands pip list/freeze/show/check map to their uv pip forms.
Outside a uv project, the low-level uv pip install/uninstall forms are used
only when a virtual environment is available. Unsupported or compound
pip invocations are denied rather than silently altered.

Rewriting requires Codex permissionDecision=allow with updatedInput, which may
affect normal tool approvals. Disable with [python] uv_only=false or
[hooks] uv_rewrite=false. Check generated hook commands before trusting them.

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


## Git hooks (opt-in)

Install the three local Git hooks inside a target Git repository:

    codex-hooks git install

The installer creates executable shell wrappers in .githooks/ and sets
core.hooksPath=.githooks locally. It refuses to replace custom existing hook
paths or conflicting scripts. The Python package install alone does not
activate local Git hooks.

- **commit-msg** validates Conventional Commits subjects. Merge, revert,
  fixup and squash subjects are accepted. A ticket ID is optional by default;
  enable require_ticket to require a Ticket: T-001 message trailer.
- **pre-commit** runs uv run --no-sync ruff format on staged Python files,
  then stages those exact paths. If a staged Python file has additional
  unstaged edits, formatting aborts rather than overwriting partial staging.
  Ruff must already be installed in the uv-managed environment.
  The hook then records HEAD and the candidate staged tree SHA in SQLite
  and queues isolated tests without waiting for completion.
- **post-checkout** initializes each newly detected linked worktree once.
  If a pyproject.toml is present, it runs uv sync --locked when uv.lock
  exists or uv sync otherwise. Ordinary branch/file checkouts do not
  reinstall the environment after successful initialization.
  Checkout itself cannot be rolled back if uv sync fails.

### Revision identity and non-blocking test runs

At pre-commit time the new commit does not exist yet. We record the Git
index tree SHA returned by git write-tree, along with the previous HEAD
revision. The test worker exports this exact staged tree with git archive
and validates it in a separate temporary directory. Later worktree edits
cannot change which code that job tests.

The hook persists a SQLite job and starts a detached validation worker.
Test failures never retroactively block the commit: a queued job is not a
passing result. You can inspect and manually run queued jobs:

    codex-hooks git jobs
    codex-hooks git worker JOB_ID

Automatic runner discovery supports Python with pytest via uv, Rust, and Go.
For custom tests, configure an argument array (no shell interpolation):

    [git.tests]
    command = ["uv", "run", "--group", "dev", "pytest", "-q"]

The worker stores status, exit code, and output digest, not raw test logs,
and removes its temporary snapshot after completing.

Configure the hooks in .codex-hooks.toml:

    [git.commit_msg]
    require_ticket = false

    [git.pre_commit]
    format = true
    tests = true

    [git.post_checkout]
    uv_sync = true

Hooks can be bypassed locally. Use CI for authoritative merge enforcement.

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
