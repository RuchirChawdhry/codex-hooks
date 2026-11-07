# Python conventions

- Prefer small functions, guard clauses and early returns to nested if/else branches.
- Use explicit dispatch tables for CLI subcommands, lifecycle events and discrete policies.
- Prefer comprehensions for data transformations and enumerate() over manual index counters.
- Keep imperative loops for side effects, ordering, state transitions and filesystem safety.
- Do not trade clarity, predictable error handling or security checks for fewer lines.
- Preserve native Codex/Git hook contracts and SQLite state schema during refactors.
- Use uv, not pip: uv sync, uv add, uv remove, uv run and uv pip where appropriate.
- Validate every change with: uv run --group dev ruff check . and uv run --group dev pytest -q.
- Keep tests deterministic; do not require a live Codex instance for contract tests.
