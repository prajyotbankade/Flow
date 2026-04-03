# Plan v4 — CLI Package & Prod-Ready Architecture

> **Status**: Planning
> **Predecessor**: planv3.md (context slicer, tribunal tiebreaker, eval scalability)
> **Trigger**: The skill is feature-complete but has no standalone interface for agents. Currently agents must run the HTTP server to enforce gate rules and versioning — a fragile dependency. A proper CLI package removes this, gives agents a deterministic interface, and cleanly separates the two consumer surfaces: agents (CLI) and humans (web board via server).

---

## Problem

Agents today have two choices:
1. **Read/write `backlog.json` directly** — bypasses gate rules, versioning, and validation
2. **Hit the HTTP server** — correct behavior, but requires the server to be running

Neither is good. Option 1 is unsafe. Option 2 is fragile. There is no agent-native interface that enforces the rules without a running server.

Additionally, all business logic (gate enforcement, `gate_from` watermark, optimistic locking, atomic writes) lives inside `backlog_server.py`. The server is both the HTTP adapter *and* the rule engine — tightly coupled, hard to reuse.

---

## Solution

Restructure the skill as a proper Python package with three layers:

```
┌─────────────────────┐    ┌─────────────────────┐
│   Agents / Claude   │    │     Web Board UI     │
│   (via CLI / skill) │    │   (human visual use) │
└────────┬────────────┘    └──────────┬───────────┘
         │ direct                     │ HTTP
         │                   ┌────────▼───────┐
         │                   │  backlog server │  (thin HTTP adapter)
         │                   └────────┬───────┘
         │                            │
         └──────────┬─────────────────┘
                    │
            ┌───────▼────────┐
            │  backlog.core  │  ← all business logic lives here
            │                │    gate rules, versioning, atomic writes
            └───────┬────────┘
                    │
            ┌───────▼────────┐
            │  backlog.json  │
            └────────────────┘
```

**Agents use the CLI. Humans use the web board. Logic lives once, in `core`.**

---

## Principles

1. **Logic lives once.** Gate rules, `gate_from` watermark, optimistic locking, and atomic writes belong in `core.py`. The CLI and server are both thin adapters over it.
2. **Agents don't need the server.** Any operation an agent needs to perform is available via `backlog` CLI commands — no server required.
3. **Explicit over implicit.** No walk-up file discovery. File path is set via `BACKLOG_FILE` env var or `--file` flag. Ambiguity is an error.
4. **Deterministic interface.** Same inputs → same outputs. No ambient state. Exit codes are meaningful.

---

## File Configuration

File path resolution follows this precedence (highest to lowest):

```
--file <path>  >  BACKLOG_FILE env var  >  error
```

If neither is set, the CLI exits with a clear message:
```
Error: No backlog file specified. Use --file or set BACKLOG_FILE.
```

Agents set `BACKLOG_FILE` once at session start and run bare commands. Scripts always use `--file` for safety. The server inherits the same convention — no special-casing.

---

## Package Structure

```
skills/backlog-manager/
├── backlog/
│   ├── __init__.py
│   ├── core.py          # BacklogStore — all business logic
│   ├── exceptions.py    # GateViolationError, ConflictError, ItemNotFoundError
│   ├── cli.py           # Typer CLI — thin adapter over core
│   └── server.py        # Flask server — thin adapter over core (replaces backlog_server.py)
├── assets/
│   └── backlog-board.html
├── evals/               # unchanged
├── references/          # unchanged
├── pyproject.toml
└── SKILL.md
```

Entry points defined in `pyproject.toml`:
```toml
[project.scripts]
backlog = "backlog.cli:app"
backlog-server = "backlog.server:main"
```

- **`backlog`** — the agent/user CLI for all backlog operations
- **`backlog-server`** — launches the HTTP server for the web board; accepts `--port` and `--file` flags directly. `backlog board` (CLI subcommand) is a convenience wrapper that calls `backlog-server` with sensible defaults.

After `pip install -e .` inside the skill directory, both commands are available globally.

---

## CLI Command Surface

```bash
# Board view
backlog list                          # grouped by lane
backlog list --status ready           # filter by lane
backlog list --assigned-to alice      # filter by assignee

# Item detail
backlog show 3                        # full detail for item #3

# Adding
backlog add "Title of task"
backlog add "Title" --priority high --complexity low

# Lane transitions (gate rules enforced)
backlog move 3 in-progress
backlog move 3 code-review
backlog done 3

# Assignment
backlog assign 3 --to alice
backlog unassign 3

# Lifecycle
backlog discard 3
backlog restore 3                     # always restores to backlog lane

# Workflow helpers
backlog pick                          # pick highest-priority ready item (sets in-progress + assigned-to)

# Plumbing
backlog init                          # write starter backlog.json to current directory
backlog board                         # convenience: launch web board (wraps backlog-server)

# Output
backlog list --json                   # machine-readable output for scripting
```

**Exit codes:**
- `0` — success
- `1` — item not found, gate violation, or validation error (with clear message)
- `2` — conflict (version mismatch — re-read and retry)

---

## Tasks

> **Execution order**: Task 0 → Task 2 → Task 3 → Task 5 → Task 1 → Task 4
>
> CLI tests (Task 5) run before the server rewrite (Task 1). If a gate rule breaks during the server rewrite, the tests catch it immediately. `pyproject.toml` (Task 3) comes after `cli.py` (Task 2) — entry points can't be defined before the module exists.

---

### Task 0: Extract `backlog/core.py` and `backlog/exceptions.py`

**What**: Pull all business logic out of `backlog_server.py` into a new `BacklogStore` class, and define shared exceptions that both CLI and server catch.

**`backlog/exceptions.py`:**
```python
class GateViolationError(Exception):
    """Raised when a forward move is blocked by unsatisfied gate requirements."""

class ConflictError(Exception):
    """Raised when a write is rejected due to a version mismatch (optimistic locking)."""

class ItemNotFoundError(Exception):
    """Raised when a position number does not resolve to a backlog item."""
```

CLI maps these to exit codes (1 for `GateViolationError`/`ItemNotFoundError`, 2 for `ConflictError`). Server maps them to HTTP status codes (422, 409, 404 respectively).

**`BacklogStore` interface:**
```python
class BacklogStore:
    def __init__(self, file_path: str): ...

    def read(self) -> dict                                              # read + parse backlog.json
    def write(self, data: dict) -> None                                 # atomic write with version increment
    def get_item(self, position: int) -> dict                           # 1-based; raises ItemNotFoundError
    def add_item(self, title: str, **fields) -> dict
    def move_item(self, position: int, target_status: str) -> dict      # raises GateViolationError if gates unsatisfied
    def assign_item(self, position: int, agent: str) -> dict
    def unassign_item(self, position: int) -> dict
    def discard_item(self, position: int) -> dict
    def restore_item(self, position: int) -> dict                       # always restores to backlog lane
    def pick_item(self, agent: str) -> dict                             # highest-priority ready item → in-progress
    def reorder(self, position: int, new_position: int) -> None
```

**Gate enforcement**: `move_item` checks `lane_history[gate_from:]` against `config.statuses[target].requires` and raises `GateViolationError` if not satisfied.

**`gate_from` watermark**: when `move_item` moves an item **backward** (to an earlier lane), it appends the current lane to `lane_history` AND sets `gate_from` to the new length of `lane_history`. This resets the watermark — the item must re-earn forward gates from its new position. Forward moves only append to `lane_history`; `gate_from` is unchanged.

**Atomic writes**: temp file + rename, same as current server implementation. Raises `ConflictError` on version mismatch.

**Acceptance**: All existing server behavior is preserved. No business logic remains in `server.py` that belongs in `core.py`.

---

### Task 2: Build `backlog/cli.py`

**What**: Typer-based CLI. Every command is a thin wrapper over `BacklogStore`. Catches exceptions from `exceptions.py` and maps to exit codes.

**File resolution** (shared helper used by all commands):
```python
def resolve_file(file_flag: str | None) -> str:
    path = file_flag or os.environ.get("BACKLOG_FILE")
    if not path:
        typer.echo("Error: No backlog file specified. Use --file or set BACKLOG_FILE.")
        raise typer.Exit(1)
    return path
```

**Exception mapping:**
```python
except ItemNotFoundError as e:  → typer.echo(str(e)); raise typer.Exit(1)
except GateViolationError as e: → typer.echo(str(e)); raise typer.Exit(1)
except ConflictError as e:      → typer.echo(str(e)); raise typer.Exit(2)
```

**Display**: human-readable by default (board-style grouped output for `list`, detail view for `show`). `--json` flag on all read commands for scripting. No color escape codes when stdout is not a tty (`rich` handles this automatically).

**`backlog board`**: convenience subcommand that execs `backlog-server` with `--file` set from the resolved path and `--port 8089` as default. User can override port with `--port`.

**Acceptance**: All commands in the command surface above work. `--help` is available on every command. Exit codes are correct.

---

### Task 3: Package Setup (`pyproject.toml`)

**What**: Wire everything up as an installable package.

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "backlog-manager"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "typer>=0.12",
    "flask>=3.0",
    "rich>=13.0",
]

[project.scripts]
backlog = "backlog.cli:app"
backlog-server = "backlog.server:main"
```

**Acceptance**: `pip install -e .` succeeds. `backlog --help` and `backlog-server --help` work globally.

---

### Task 5: CLI Integration Tests

**What**: A test file `evals/test_cli.py` that exercises the CLI end-to-end via `typer.testing.CliRunner` against a temp `backlog.json`.

Scenarios to cover:
- `backlog add` → item appears in `backlog list`
- `backlog move` through a valid lane sequence → succeeds, exit code 0
- `backlog move` that skips a required lane → exit code 1, message contains "gate"
- `backlog move` backward → succeeds, `gate_from` watermark resets (verified by attempting a previously-earned forward move and confirming it is now blocked)
- `backlog pick` → highest-priority ready item moves to `in-progress`, assigned correctly
- `backlog restore` → item returns to `backlog` lane regardless of prior status
- Concurrent write conflict → exit code 2
- `BACKLOG_FILE` not set and no `--file` → exit code 1, clear error message
- `--json` output is valid JSON and contains expected fields

**Acceptance**: All scenarios pass. Each test uses a fresh temp file — no shared state between cases.

---

### Task 1: Rewrite `backlog_server.py` → `backlog/server.py`

**What**: Slim the server down to a pure HTTP adapter. Every route handler instantiates `BacklogStore`, calls the right method, and returns JSON. No business logic in route handlers.

**Exception mapping:**
```python
except ItemNotFoundError:    → 404
except GateViolationError:   → 422
except ConflictError:        → 409
```

The server still serves `backlog-board.html` and all existing API routes. External behavior is unchanged — only the internals are restructured.

**Acceptance**: All existing API routes work identically. The web board operates normally. CLI integration tests still pass after this task (confirms `core.py` didn't regress).

---

### Task 4: Update SKILL.md

**What**: Update the skill's documentation to reflect the new architecture.

- Add CLI command reference (mirrors the command surface above)
- Add setup instructions: `pip install -e .` inside skill directory, then `export BACKLOG_FILE=path/to/backlog.json`
- Document `BACKLOG_FILE` env var and `--file` flag with the precedence rule
- Clarify the two entry points: `backlog` for agents/CLI use, `backlog-server` for the web board; `backlog board` as the convenience wrapper
- Update server launch command: `backlog-server` replaces `python scripts/backlog_server.py`
- Document which interface to use when: CLI for agents, web board for humans, Claude skill for natural language

**Acceptance**: A new user can follow SKILL.md alone to install, configure, and use all three interfaces.

---

## Non-Goals (for this plan)

- **Changing the web board UI** — `backlog-board.html` is unchanged
- **Changing the eval suite** — evals continue to test LLM behavior, not the CLI
- **Shell completion** — Typer supports it, but not in scope for v4
- **Windows support** — atomic writes use POSIX rename; not a current target
- **Remote/multi-user backlogs** — the file is still local; network sync is a future concern

---

## Success Criteria

1. `pip install -e .` inside the skill directory gives working `backlog` and `backlog-server` commands
2. An agent can perform any backlog operation (add, move, assign, pick, done) with zero server dependency
3. Gate rules are enforced identically whether the operation comes from CLI or HTTP server
4. The web board continues to work exactly as before
5. All CLI integration tests pass
6. SKILL.md is updated and a new user can follow it without prior context
