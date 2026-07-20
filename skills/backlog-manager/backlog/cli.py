"""Backlog CLI — thin Typer adapter over BacklogStore.

File resolution precedence:
  --file flag  >  BACKLOG_FILE env var  >  ./backlog.json (default)

Exit codes:
  0 — success
  1 — item not found, gate violation, or validation error
  2 — version conflict (re-read and retry)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from .core import BacklogStore, DEFAULT_STATUSES, _detect_foreign_schema, migrate_to_flow_schema, _now_iso, reconcile_journal
from .exceptions import ConflictError, GateViolationError, ItemNotFoundError
from .server import compute_scores, compute_item_readiness, evaluate_tribunal

app = typer.Typer(
    name="backlog",
    help="Manage your project backlog from the terminal.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

# ── File resolution ────────────────────────────────────────────────────────────

def _resolve_file(file_flag: Optional[str]) -> str:
    return file_flag or os.environ.get("BACKLOG_FILE", "backlog.json")


def _store(file_flag: Optional[str]) -> BacklogStore:
    return BacklogStore(_resolve_file(file_flag))


# #95 recovery journal: path-keyed guard so reconcile runs at most once per
# resolved path per process. A path-keyed set (not a process-global boolean) is
# required because test_cli_core.py runs ~63 in-process CliRunner invocations
# across DISTINCT tmp_path files in one pytest process.
_RECONCILED_PATHS: set = set()


def _reset_reconcile_guard() -> None:
    """Test hook: clear the per-process reconcile guard so a path re-triggers."""
    _RECONCILED_PATHS.clear()


def _require_store(file_flag: Optional[str]) -> BacklogStore:
    path = _resolve_file(file_flag)
    abs_path = os.path.abspath(path)
    if abs_path not in _RECONCILED_PATHS:
        _RECONCILED_PATHS.add(abs_path)
        # Conflict mode comes from BACKLOG_ON_CONFLICT env (no per-command flag).
        reconcile_journal(abs_path)
    if not os.path.exists(path):
        err_console.print(f"[red]No backlog.json found at {path}.[/red]")
        err_console.print("Run [bold]backlog init[/bold] to create one and get started.")
        raise typer.Exit(1)
    return BacklogStore(path)


# ── Exception handling ────────────────────────────────────────────────────────

def _handle(fn):
    """Decorator: map domain exceptions to exit codes."""
    import functools
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ItemNotFoundError as e:
            err_console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
        except GateViolationError as e:
            err_console.print(f"[red]Gate violation:[/red] {e}")
            raise typer.Exit(1)
        except ConflictError as e:
            err_console.print(f"[red]Conflict:[/red] {e}")
            raise typer.Exit(2)
        except FileExistsError as e:
            err_console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
        except ValueError as e:
            err_console.print(f"[red]Error:[/red] {e}")
            raise typer.Exit(1)
    return wrapper


# ── Display helpers ───────────────────────────────────────────────────────────

def _status_label(status_id: str, statuses: list) -> str:
    for s in statuses:
        if s.get("id") == status_id:
            return s.get("label", status_id)
    return status_id


def _print_board(data: dict, filter_status: Optional[str] = None,
                 filter_assigned: Optional[str] = None, as_json: bool = False) -> None:
    items = data.get("items", [])
    statuses = data.get("config", {}).get("statuses", DEFAULT_STATUSES)

    # Apply filters
    if filter_status:
        items = [i for i in items if i.get("status") == filter_status]
    if filter_assigned:
        items = [i for i in items if i.get("assigned_to") == filter_assigned]

    if as_json:
        console.print_json(json.dumps(items))
        return

    if not items:
        console.print("[dim]No items found.[/dim]")
        return

    # Group by status (in configured order)
    status_order = [s.get("id") for s in statuses]
    groups: dict[str, list] = {}
    for item in items:
        s = item.get("status", "backlog")
        groups.setdefault(s, []).append(item)

    # Build position map (global 1-based index in original items array)
    all_items = data.get("items", [])
    pos_map = {item.get("id"): idx + 1 for idx, item in enumerate(all_items)}

    project = data.get("config", {}).get("project_name", "")
    title = f"Backlog — {project}" if project else "Backlog"
    console.print(f"\n[bold]{title}[/bold]\n")

    for sid in status_order:
        if sid not in groups:
            continue
        label = _status_label(sid, statuses)
        console.print(f"[bold]{label}[/bold]")
        for item in groups[sid]:
            pos = pos_map.get(item.get("id"), "?")
            assigned = f" [dim](→ {item['assigned_to']})[/dim]" if item.get("assigned_to") else ""
            reviewer = item.get("reviewer")
            reviewer_flag = f" [magenta](reviewer: {reviewer})[/magenta]" if reviewer else (
                " [yellow](no reviewer)[/yellow]" if sid == "code-review" else ""
            )
            unresolved = sum(
                1 for t in item.get("threads", []) if not t.get("resolved")
            )
            thread_flag = f" [yellow]⚠ {unresolved} unresolved[/yellow]" if unresolved else ""
            arrow = "→ " if sid == "in-progress" else "  "
            console.print(
                f"  {arrow}[cyan]#{pos}[/cyan] {item.get('title', '')}"
                f"{assigned}{reviewer_flag}{thread_flag}"
            )
        console.print()


def _print_item(item: dict, position: int, as_json: bool = False) -> None:
    if as_json:
        console.print_json(json.dumps(item))
        return

    console.print(f"\n[bold cyan]#{position} {item.get('title', '')}[/bold cyan]")
    console.print(f"  Status:     {item.get('status', '')}")
    console.print(f"  Priority:   {item.get('priority', '')}  weight={item.get('priority_weight', '')}")
    console.print(f"  Complexity: {item.get('complexity', '')}")
    console.print(f"  Category:   {item.get('category', '')}")
    console.print(f"  Tags:       {', '.join(item.get('tags', []))}")
    console.print(f"  Assigned:   {item.get('assigned_to') or '(unassigned)'}")
    reviewer = item.get("reviewer")
    reviewer_history = item.get("reviewer_history", [])
    if reviewer or item.get("status") == "code-review":
        console.print(f"  Reviewer:   {reviewer or '(unassigned)'}")
    if reviewer_history:
        console.print(f"  Rev. history: {', '.join(reviewer_history)}")
    if item.get("description"):
        console.print(f"  Description:\n    {item['description']}")
    threads = item.get("threads", [])
    if threads:
        console.print(f"  Threads:    {len(threads)} ({sum(1 for t in threads if not t.get('resolved'))} unresolved)")
    staged_actions = item.get("staged_actions", [])
    pending = [a for a in staged_actions if a.get("status") == "pending"]
    if staged_actions:
        console.print(f"  Staged:     {len(staged_actions)} action(s) ({len(pending)} pending)")
        for a in pending:
            console.print(
                f"    [yellow]PENDING[/yellow] [{a.get('type','')}] {a.get('description','')} "
                f"(id={a.get('id','')}, by={a.get('staged_by','')})"
            )
    history = item.get("lane_history", [])
    if history:
        console.print(f"  History:    {len(history)} lane transition(s)")
    exec_history = item.get("execution_history", [])
    if exec_history:
        console.print(f"  Audit trail ({len(exec_history)} event(s)):")
        for entry in exec_history:
            actor = entry.get("actor", "?")
            action = entry.get("action", "?")
            at = entry.get("at", "")[:19].replace("T", " ")
            detail = entry.get("detail", "")
            console.print(f"    [dim]{at}[/dim]  [{actor}] {action}" + (f" — {detail}" if detail else ""))
    console.print(f"  ID:         {item.get('id', '')}")
    console.print(f"  Created:    {item.get('created_at', '')}")
    console.print(f"  Updated:    {item.get('updated_at', '')}")
    console.print()


# ── Commands ──────────────────────────────────────────────────────────────────

FILE_OPT = typer.Option(None, "--file", "-f", help="Path to backlog.json (overrides BACKLOG_FILE)")
JSON_OPT = typer.Option(False, "--json", help="Output as JSON")


@app.command()
@_handle
def list(
    file: Optional[str] = FILE_OPT,
    status: Optional[str] = typer.Option(None, "--status", "-s", help="Filter by lane"),
    assigned_to: Optional[str] = typer.Option(None, "--assigned-to", help="Filter by assignee"),
    json_out: bool = JSON_OPT,
) -> None:
    """Show the backlog grouped by lane."""
    store = _require_store(file)
    data = store.read()
    _print_board(data, filter_status=status, filter_assigned=assigned_to, as_json=json_out)


@app.command()
@_handle
def top(
    n: int = typer.Argument(5, help="Number of items to show (default 5)"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Show the top N prioritized items, ranked by tribunal. No server needed."""
    store = _require_store(file)
    data = store.read()
    all_items = data.get("items", [])

    if not any(i.get("status") not in {"done", "discarded"} for i in all_items):
        console.print("[dim]Backlog is empty.[/dim]")
        return

    pos_map = {item.get("id"): idx + 1 for idx, item in enumerate(all_items)}
    items_by_id = {item.get("id"): item for item in all_items}

    result = evaluate_tribunal(data)
    picked = result.get("picked")
    shadow = result.get("shadow_ranking", [])

    if not picked:
        console.print("[dim]Backlog is empty.[/dim]")
        return

    # Build ranked list: winner first, then shadow runners-up, sliced to n
    ranked_entries = [{"id": picked["item_id"], "tribunal_score": picked["tribunal_score"],
                       "score": picked["score"], "reasoning": picked.get("reasoning"),
                       "readiness": picked.get("readiness", {})}]
    for s in shadow:
        ranked_entries.append({"id": s["item_id"], "tribunal_score": s["tribunal_score"],
                                "score": s["score"], "reasoning": None,
                                "readiness": {}})
    ranked_entries = ranked_entries[:n]

    if json_out:
        out = []
        for r in ranked_entries:
            raw = items_by_id.get(r["id"], {})
            out.append({**r, "priority_weight": raw.get("priority_weight"),
                        "complexity": raw.get("complexity"), "tags": raw.get("tags", []),
                        "assigned_to": raw.get("assigned_to"),
                        "status": raw.get("status"), "title": raw.get("title")})
        console.print_json(json.dumps(out))
        return

    console.print(f"\n[bold]Top {n} by tribunal[/bold]\n")
    for rank, r in enumerate(ranked_entries, 1):
        iid = r["id"]
        raw = items_by_id.get(iid, {})
        pos = pos_map.get(iid, "?")
        tribunal_score = r["tribunal_score"]
        status = raw.get("status", "")
        title = raw.get("title", "")
        pw = raw.get("priority_weight") or "—"
        tags = ", ".join(raw.get("tags") or [])
        assigned = raw.get("assigned_to") or "unassigned"
        complexity = raw.get("complexity") or "—"
        readiness_pct = int(r["readiness"].get("score", 0) * 100)

        status_color = {
            "in-progress": "yellow", "ready": "green", "refined": "blue",
            "backlog": "dim", "code-review": "magenta",
        }.get(status, "dim")

        console.print(
            f"  [bold]{rank}.[/bold] [cyan]#{pos}[/cyan] {title}  "
            f"[bold]score={tribunal_score}[/bold]  pw={pw}  [{status_color}]{status}[/{status_color}]"
        )
        console.print(
            f"       readiness={readiness_pct}%  effort={complexity}  assigned={assigned}"
            + (f"  tags={tags}" if tags else "")
        )
        if rank == 1:
            justification = r.get("reasoning") or "No justification available"
            console.print(f"       [dim]why: {justification}[/dim]")
    console.print()


@app.command()
@_handle
def show(
    position: int = typer.Argument(..., help="Item number (e.g. 3 for #3)"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Show full detail for one item."""
    store = _require_store(file)
    _, item = store.get_item(position)
    _print_item(item, position, as_json=json_out)


@app.command()
@_handle
def add(
    title: str = typer.Argument(..., help="Item title"),
    file: Optional[str] = FILE_OPT,
    description: str = typer.Option("", "--description", "-d", help="Item description"),
    priority: Optional[str] = typer.Option(None, "--priority", "-p", help="high/medium/low"),
    priority_weight: Optional[int] = typer.Option(None, "--priority-weight", help="1–10"),
    complexity: Optional[str] = typer.Option(None, "--complexity", "-c", help="low/medium/high"),
    category: Optional[str] = typer.Option(None, "--category"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags"),
    assigned_to: Optional[str] = typer.Option(None, "--assigned-to"),
) -> None:
    """Add a new item to the bottom of the backlog."""
    store = _require_store(file)
    tag_list = [t.strip() for t in tags.split(",")] if tags else []
    item = store.add_item(
        title,
        description=description,
        priority=priority,
        priority_weight=priority_weight,
        complexity=complexity,
        category=category,
        tags=tag_list,
        assigned_to=assigned_to,
    )
    data = store.read()
    position = len(data.get("items", []))
    console.print(f"[green]Added[/green] #{position} \"{item['title']}\"")


@app.command()
@_handle
def move(
    position: int = typer.Argument(..., help="Item number"),
    target_status: str = typer.Argument(..., help="Target lane (e.g. in-progress)"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Move an item to a different lane (gate rules enforced)."""
    store = _require_store(file)
    item = store.move_item(position, target_status, moved_by="user")
    console.print(f"[green]Moved[/green] #{position} \"{item['title']}\" → {target_status}")


@app.command()
@_handle
def done(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Move an item to done."""
    store = _require_store(file)
    item = store.move_item(position, "done", moved_by="user")
    console.print(f"[green]Done[/green] #{position} \"{item['title']}\"")


@app.command()
@_handle
def assign(
    position: int = typer.Argument(..., help="Item number"),
    to: str = typer.Option(..., "--to", help="Agent or person name"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Assign an item to an agent or person."""
    store = _require_store(file)
    item = store.assign_item(position, to)
    console.print(f"[green]Assigned[/green] #{position} \"{item['title']}\" → {to}")


@app.command()
@_handle
def unassign(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Remove assignment from an item."""
    store = _require_store(file)
    item = store.unassign_item(position)
    console.print(f"[green]Unassigned[/green] #{position} \"{item['title']}\"")


@app.command()
@_handle
def discard(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Discard an item (always allowed from any lane)."""
    store = _require_store(file)
    item = store.discard_item(position, moved_by="user")
    console.print(f"[dim]Discarded[/dim] #{position} \"{item['title']}\"")


@app.command()
@_handle
def restore(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Restore a discarded item back to backlog."""
    store = _require_store(file)
    item = store.restore_item(position, moved_by="user")
    console.print(f"[green]Restored[/green] #{position} \"{item['title']}\" → backlog")


@app.command()
@_handle
def pick(
    agent: str = typer.Argument(..., help="Your agent/user name"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Pick the highest-priority ready item, move to in-progress, and assign it."""
    store = _require_store(file)
    item = store.pick_item(agent)
    if json_out:
        console.print_json(json.dumps(item))
    else:
        console.print(
            f"[green]Picked[/green] \"{item['title']}\" → in-progress, assigned to {agent}"
        )


@app.command()
@_handle
def edit(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
    title: Optional[str] = typer.Option(None, "--title"),
    description: Optional[str] = typer.Option(None, "--description", "-d"),
    priority: Optional[str] = typer.Option(None, "--priority", "-p"),
    priority_weight: Optional[int] = typer.Option(None, "--priority-weight"),
    complexity: Optional[str] = typer.Option(None, "--complexity", "-c"),
    category: Optional[str] = typer.Option(None, "--category"),
    tags: Optional[str] = typer.Option(None, "--tags", help="Comma-separated tags"),
    assigned_to: Optional[str] = typer.Option(None, "--assigned-to"),
    refinement_gate: Optional[str] = typer.Option(None, "--refinement-gate", help="simple/complex"),
) -> None:
    """Edit fields on an item (use 'move' to change status)."""
    store = _require_store(file)
    fields = {}
    if title is not None:          fields["title"] = title
    if description is not None:    fields["description"] = description
    if priority is not None:       fields["priority"] = priority
    if priority_weight is not None: fields["priority_weight"] = priority_weight
    if complexity is not None:     fields["complexity"] = complexity
    if category is not None:       fields["category"] = category
    if tags is not None:           fields["tags"] = [t.strip() for t in tags.split(",")]
    if assigned_to is not None:    fields["assigned_to"] = assigned_to
    if refinement_gate is not None:
        if refinement_gate not in ("simple", "complex"):
            raise typer.BadParameter("Must be 'simple' or 'complex'", param_hint="--refinement-gate")
        fields["refinement_gate"] = refinement_gate
    if not fields:
        err_console.print("[yellow]No fields to update.[/yellow]")
        raise typer.Exit(1)
    item = store.edit_item(position, **fields)
    console.print(f"[green]Updated[/green] #{position} \"{item['title']}\"")


@app.command(name="init")
@_handle
def init_cmd(
    file: Optional[str] = FILE_OPT,
) -> None:
    """Write a starter backlog.json to the current directory (or --file path)."""
    path = file or os.environ.get("BACKLOG_FILE", "backlog.json")
    store = BacklogStore(path)
    try:
        store.init()
    except FileExistsError as e:
        msg = str(e)
        if msg.startswith("foreign_schema:"):
            schema_desc = msg[len("foreign_schema:"):]
            console.print(f"[yellow]backlog.json already exists with an incompatible schema:[/yellow] {schema_desc}")
            console.print("Run [bold]backlog doctor --fix[/bold] to migrate it to Flow format (a backup will be created).")
        else:
            console.print("[yellow]backlog.json already exists.[/yellow] Run [bold]backlog doctor --fix[/bold] to check setup.")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] Created {store.file_path}")

    # Auto-wire CLAUDE.md so agents use the backlog without extra setup steps
    cwd = Path.cwd()
    claude_md = _find_claude_md(cwd)
    target = claude_md or (cwd / "CLAUDE.md")
    try:
        action = _ensure_snippet(target)
    except OSError as exc:
        console.print(f"[yellow]⚠[/yellow] Could not update {target.name}: {exc}")
        action = "error"
    if action == "noop":
        console.print(f"[green]✓[/green] CLAUDE.md already configured")
    elif action == "wrapped":
        console.print(f"[green]✓[/green] {target.name} already has a Flow Backlog section — added markers so it's recognised on future runs")
        console.print(f"[dim]  git add {target.name} && git commit -m 'chore: add flow backlog markers'[/dim]")
    elif action == "appended":
        console.print(f"[green]✓[/green] Updated {target.name} — agents will use the backlog automatically")
        console.print(f"[dim]  git add {target.name} && git commit -m 'chore: add flow backlog setup'[/dim]")

    # Install pre-commit hook (opt-in integrity gate for staged backlog.json).
    hook_status, hook_path = _install_precommit_hook(Path(store.file_path).resolve().parent)
    if hook_status == "installed":
        console.print(f"[green]✓[/green] Installed pre-commit hook → {hook_path}")
    elif hook_status == "refreshed":
        console.print(f"[green]✓[/green] Refreshed pre-commit hook → {hook_path}")
    elif hook_status == "up-to-date":
        console.print("[green]✓[/green] pre-commit hook already up to date")
    elif hook_status == "foreign":
        console.print(
            "[yellow]![/yellow] A pre-commit hook already exists at "
            f"{hook_path}. To add the backlog integrity gate manually, "
            "append the following snippet to that file:"
        )
        console.print(f"[dim]{_HOOK_SCRIPT}[/dim]")

    console.print()
    console.print("[bold green]You're ready.[/bold green]")
    console.print()
    console.print('  backlog add "Your first task"')
    console.print("  backlog top                             [dim]# what to work on next[/dim]")
    console.print("  backlog board                           [dim]# open the visual board[/dim]")


# ── Pre-commit hook ────────────────────────────────────────────────────────────

_HOOK_MARKER = "# flow-backlog-integrity-gate"

_HOOK_SCRIPT = """\
#!/bin/sh
# flow-backlog-integrity-gate
# Validate backlog.json before it is committed.
# Installed by `backlog init`. Remove this file to disable the gate.

BACKLOG="backlog.json"

# Only run if backlog.json is staged.
git diff --cached --name-only | grep -q "^${BACKLOG}$" || exit 0

# Extract the staged content to a temp file.
TMPFILE=$(mktemp)
git show ":${BACKLOG}" > "$TMPFILE" 2>/dev/null || { rm -f "$TMPFILE"; exit 0; }

# Conflict-marker check — anchored to line start, runs FIRST.
# A conflicted file is also invalid JSON; showing the marker diagnostic
# (with line numbers) is more actionable than a generic JSON parse error.
python3 - "$TMPFILE" <<'PYEOF'
import sys, re
text = open(sys.argv[1]).read()
bad = [i+1 for i, l in enumerate(text.splitlines()) if re.match(r'^(<<<<<<< |>>>>>>> |=======\\s*$)', l)]
if bad:
    print(f"error: staged backlog.json contains git conflict markers at line(s): {', '.join(map(str,bad))}", file=sys.stderr)
    sys.exit(1)
PYEOF
STATUS=$?
if [ $STATUS -ne 0 ]; then
  rm -f "$TMPFILE"
  exit $STATUS
fi

# JSON validity check — second gate for marker-free but malformed files.
python3 -c "import json,sys; json.loads(open(sys.argv[1]).read())" "$TMPFILE" 2>/dev/null
if [ $? -ne 0 ]; then
  echo "error: staged backlog.json is not valid JSON. Fix it before committing." >&2
  rm -f "$TMPFILE"
  exit 1
fi

# Branch guard — backlog.json must only be committed on the trunk branch.
# Backlog state follows trunk; committing it on a feature branch contaminates
# code PRs with lane-move noise.
# Fail open: detached HEAD, missing git/python3, or unreadable config → allow.
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ -n "$CURRENT_BRANCH" ] && [ "$CURRENT_BRANCH" != "HEAD" ]; then
  # Determine trunk: main/master are always trunk; also check config.integration_branch.
  TRUNK_BRANCH=$(python3 - "$TMPFILE" <<'PYEOF2'
import sys, json
try:
    data = json.loads(open(sys.argv[1]).read())
    ib = data.get("config", {}).get("integration_branch", "")
    if ib:
        print(ib)
    else:
        print("main")
except Exception:
    print("main")
PYEOF2
)
  if [ -z "$TRUNK_BRANCH" ]; then
    TRUNK_BRANCH="main"
  fi
  # Allow main and master unconditionally; allow configured integration_branch.
  if [ "$CURRENT_BRANCH" != "main" ] && [ "$CURRENT_BRANCH" != "master" ] && [ "$CURRENT_BRANCH" != "$TRUNK_BRANCH" ]; then
    echo "error: backlog.json is staged on branch '${CURRENT_BRANCH}' (not trunk)." >&2
    echo "  Backlog state must follow trunk, not code branches." >&2
    echo "  To fix: git restore --staged backlog.json" >&2
    rm -f "$TMPFILE"
    exit 1
  fi
fi

rm -f "$TMPFILE"
exit 0
"""


def _install_precommit_hook(repo_search_dir: Path) -> tuple[str, Optional[Path]]:
    """Install or refresh the pre-commit hook that validates backlog.json.

    Resolves the hooks directory via ``git rev-parse --git-path hooks`` so
    that both plain repos (.git is a directory) and linked worktrees (.git is
    a gitdir-pointer file) work correctly.

    Returns ``(status, hook_path)`` — callers are responsible for all console
    output.  ``hook_path`` is ``None`` when status is ``'skipped'``.

    Status values:
      'installed'   — hook was newly written
      'refreshed'   — hook was flow-managed but stale; overwritten with current script
      'up-to-date'  — hook was flow-managed and already current; no change
      'foreign'     — a non-flow hook exists; did NOT clobber (caller prints guidance)
      'skipped'     — not a git repo, git not found, or hooks dir unresolvable
    """
    import subprocess as _sp

    # Ask git for the canonical hooks directory.  Works for normal repos AND
    # linked worktrees (where .git is a file, not a directory).
    try:
        result = _sp.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=str(repo_search_dir),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        # git binary not found — silently skip.
        return "skipped", None

    if result.returncode != 0:
        # Not a git repo (or some other git error) — silently skip.
        return "skipped", None

    hooks_raw = result.stdout.strip()
    if not hooks_raw:
        return "skipped", None

    # git may return a relative path; resolve it against repo_search_dir.
    hooks_dir = (repo_search_dir / hooks_raw).resolve()
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_path = hooks_dir / "pre-commit"

    if hook_path.exists():
        try:
            existing = hook_path.read_text(encoding="utf-8")
        except OSError:
            # Unreadable hook — treat as foreign/skip; do not crash.
            return "skipped", None

        if _HOOK_MARKER in existing:
            if existing == _HOOK_SCRIPT:
                # Already installed by us and up to date — nothing to do.
                return "up-to-date", hook_path
            else:
                # Flow-managed but stale — overwrite with current script.
                hook_path.write_text(_HOOK_SCRIPT, encoding="utf-8")
                hook_path.chmod(0o755)
                return "refreshed", hook_path
        else:
            # Foreign hook — never clobber it.  Caller prints guidance.
            return "foreign", hook_path

    hook_path.write_text(_HOOK_SCRIPT, encoding="utf-8")
    hook_path.chmod(0o755)
    return "installed", hook_path


def _doctor_hook_state(repo_search_dir: Path) -> str:
    """Return the hook state for doctor reporting without modifying anything.

    Returns one of: 'ok' | 'outdated' | 'missing' | 'foreign' | 'skipped'.
    """
    import subprocess as _sp

    try:
        result = _sp.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=str(repo_search_dir),
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return "skipped"

    if result.returncode != 0:
        return "skipped"

    hooks_raw = result.stdout.strip()
    if not hooks_raw:
        return "skipped"

    hooks_dir = (repo_search_dir / hooks_raw).resolve()
    hook_path = hooks_dir / "pre-commit"

    if not hook_path.exists():
        return "missing"

    try:
        existing = hook_path.read_text(encoding="utf-8")
    except OSError:
        return "skipped"

    if _HOOK_MARKER not in existing:
        return "foreign"

    if existing == _HOOK_SCRIPT:
        return "ok"

    return "outdated"


@app.command(name="install-hook")
def install_hook_cmd(
    file: Optional[str] = FILE_OPT,
) -> None:
    """Install or refresh the pre-commit integrity-gate hook for this repo.

    Works whether or not backlog.json exists.  Resolves the repo from
    BACKLOG_FILE / --file (same resolution as init).  Never overwrites a
    foreign (non-flow) pre-commit hook.
    """
    path = file or os.environ.get("BACKLOG_FILE", "backlog.json")
    repo_dir = Path(path).resolve().parent
    status, hook_path = _install_precommit_hook(repo_dir)
    if status == "foreign" and hook_path is not None:
        console.print(
            "[yellow]![/yellow] A pre-commit hook already exists at "
            f"{hook_path}. To add the backlog integrity gate manually, "
            "append the following snippet to that file:"
        )
        console.print(f"[dim]{_HOOK_SCRIPT}[/dim]")
    _STATUS_MESSAGES = {
        "installed": "[green]✓[/green] Hook installed.",
        "refreshed": "[green]✓[/green] Hook refreshed to latest version.",
        "up-to-date": "[green]✓[/green] Hook is already up to date — nothing changed.",
        "foreign": "[yellow]![/yellow] Foreign hook detected — not modified.",
        "skipped": "[yellow]![/yellow] Not a git repo or git not available — hook not installed.",
    }
    console.print(_STATUS_MESSAGES.get(status, f"status: {status}"))


# ── CLAUDE.md snippet ─────────────────────────────────────────────────────────

_CLAUDE_MD_MARKER = "<!-- flow-backlog-setup -->"

_CLAUDE_MD_SNIPPET = """\
<!-- flow-backlog-setup -->
## Flow Backlog

This project uses the Flow backlog manager skill.

- **What to work on next:** `backlog top`
- **Never reason about priorities yourself** — always check the backlog first
- **First time on a session:** run `backlog top` to orient, then pick up the top item
<!-- end flow-backlog-setup -->
"""


def _find_backlog_file(cwd: Path) -> Optional[Path]:
    """Walk up from cwd looking for backlog.json."""
    for directory in [cwd, *cwd.parents]:
        candidate = directory / "backlog.json"
        if candidate.exists():
            return candidate
        if (directory / ".git").exists():
            break  # stop at repo root
    return None


def _find_claude_md(cwd: Path) -> Optional[Path]:
    """Return CLAUDE.md in cwd if it exists, else None."""
    candidate = cwd / "CLAUDE.md"
    return candidate if candidate.exists() else None


_CLAUDE_MD_HEADING_RE = re.compile(r"^##\s+flow\s+backlog\s*$", re.IGNORECASE | re.MULTILINE)
_CLAUDE_MD_END_MARKER = "<!-- end flow-backlog-setup -->"


def _snippet_present(claude_md: Path) -> bool:
    try:
        return _CLAUDE_MD_MARKER in claude_md.read_text(encoding="utf-8")
    except OSError:
        return False


def _heading_present(text: str) -> bool:
    """Return True if text contains a '## Flow Backlog' heading (any case/spacing)."""
    return bool(_CLAUDE_MD_HEADING_RE.search(text))


def _wrap_existing_heading(claude_md: Path) -> None:
    """Insert marker comments around an existing '## Flow Backlog' section.

    Finds the first matching heading line, inserts the open-marker on the line
    immediately above it, then inserts the close-marker after the section body
    (i.e. just before the next '##' heading or at EOF).  The user's existing
    content is preserved verbatim.
    """
    text = claude_md.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    # Find the index of the first matching heading line.
    heading_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if _CLAUDE_MD_HEADING_RE.match(line.rstrip("\r\n")):
            heading_idx = i
            break

    if heading_idx is None:
        return  # nothing to do (caller should have checked)

    # Find where the section ends: the next '##' heading or EOF.
    end_idx = len(lines)
    for i in range(heading_idx + 1, len(lines)):
        if lines[i].startswith("##"):
            end_idx = i
            break

    # Build the new file content.
    before = lines[:heading_idx]
    section = lines[heading_idx:end_idx]
    after = lines[end_idx:]

    # Ensure the section body ends with a newline before appending the close-marker.
    if section and not section[-1].endswith("\n"):
        section[-1] += "\n"

    new_lines = (
        before
        + [_CLAUDE_MD_MARKER + "\n"]
        + section
        + [_CLAUDE_MD_END_MARKER + "\n"]
        + after
    )
    claude_md.write_text("".join(new_lines), encoding="utf-8")


def _ensure_snippet(claude_md: Path) -> str:
    """Ensure CLAUDE.md has the flow-backlog-setup marker.

    Returns a human-readable description of what was done:
      - 'noop'    — marker already present
      - 'wrapped' — existing '## Flow Backlog' heading wrapped with markers
      - 'appended' — full snippet appended (no heading existed)

    Raises OSError on read/write failure (caller handles gracefully).
    """
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""

    if _CLAUDE_MD_MARKER in existing:
        return "noop"

    if _heading_present(existing):
        _wrap_existing_heading(claude_md)
        return "wrapped"

    # No heading, no marker — append the full snippet.
    separator = "\n" if existing and not existing.endswith("\n") else ""
    claude_md.write_text(existing + separator + "\n" + _CLAUDE_MD_SNIPPET, encoding="utf-8")
    return "appended"


def _write_snippet(claude_md: Path) -> None:
    """Append the full snippet.  Legacy entry-point; new code should use _ensure_snippet."""
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    separator = "\n" if existing and not existing.endswith("\n") else ""
    claude_md.write_text(existing + separator + "\n" + _CLAUDE_MD_SNIPPET, encoding="utf-8")


def _detect_default_branch(
    git_cwd: Optional[Path] = None,
    integration_branch: Optional[str] = None,
) -> Optional[str]:
    """Return the default/trunk branch name without mutating git state.

    Resolution precedence:
      0. ``integration_branch`` argument, if set and the branch exists locally
      1. git symbolic-ref refs/remotes/origin/HEAD  (set after 'git remote set-head')
      2. git rev-parse --abbrev-ref origin/HEAD      (same but shorter form)
      3. Literal 'main' if a 'main' ref exists
      4. Literal 'master' if a 'master' ref exists

    When ``integration_branch`` is set but does not exist in the repository,
    the caller receives ``None`` from this function with a ``_MISSING_BRANCH``
    sentinel so it can emit a warning and fall back gracefully.
    """
    run_kw: dict = dict(capture_output=True, text=True, timeout=5)
    if git_cwd is not None:
        run_kw["cwd"] = git_cwd

    # Step 0: honour explicit integration_branch if it resolves to a local ref
    if integration_branch:
        try:
            verify = subprocess.run(
                ["git", "rev-parse", "--verify", integration_branch],
                **run_kw,
            )
            if verify.returncode == 0:
                return integration_branch
        except Exception:
            pass
        # Branch was named but does not exist — signal the caller via sentinel
        return _MISSING_BRANCH

    for cmd in [
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        ["git", "rev-parse", "--abbrev-ref", "origin/HEAD"],
    ]:
        try:
            result = subprocess.run(cmd, **run_kw)
            if result.returncode == 0:
                branch = result.stdout.strip()
                # Strip the 'origin/' prefix when present
                if "/" in branch:
                    branch = branch.split("/", 1)[1]
                if branch:
                    return branch
        except Exception:
            pass

    # Fallback: probe local refs
    for candidate in ("main", "master"):
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                **run_kw,
            )
            if result.returncode == 0:
                return candidate
        except Exception:
            pass

    return None


# Sentinel returned by _detect_default_branch when config.integration_branch
# names a branch that does not exist in the local repository.
_MISSING_BRANCH = "__backlog_missing_branch__"


def _check_backlog_divergence(
    backlog_path: Path,
    ok: list,
    issues: list,
) -> None:
    """Warn when the committed backlog.json on the current branch differs from trunk.

    Read-only: never mutates git state.
    Degrades gracefully (appends a note to ok) when:
      - outside a git repo
      - detached HEAD
      - no trunk branch found
      - the file is not tracked on either branch

    All git commands run with cwd set to the directory containing backlog.json
    so that git resolves to the correct repo regardless of the process's cwd.
    """
    # Use the directory containing backlog.json as git cwd so that the check
    # operates on the correct repo even when the process cwd differs (e.g. in tests).
    git_cwd = backlog_path.resolve().parent
    run_kw: dict = dict(capture_output=True, text=True, timeout=5, cwd=git_cwd)

    try:
        # Is this a git repo?
        repo_check = subprocess.run(["git", "rev-parse", "--git-dir"], **run_kw)
        if repo_check.returncode != 0:
            ok.append("not in a git repo — skipping multi-branch divergence check")
            return

        # Find the repo root so we can compute the repo-relative path
        root_result = subprocess.run(["git", "rev-parse", "--show-toplevel"], **run_kw)
        if root_result.returncode != 0:
            ok.append("could not determine git root — skipping divergence check")
            return
        git_root = Path(root_result.stdout.strip())

        try:
            rel_path = backlog_path.resolve().relative_to(git_root)
        except ValueError:
            ok.append("backlog.json is outside the git repo root — skipping divergence check")
            return

        # Are we on a named branch?
        head_result = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], **run_kw)
        if head_result.returncode != 0 or head_result.stdout.strip() == "HEAD":
            ok.append("detached HEAD — skipping divergence check")
            return
        current_branch = head_result.stdout.strip()

        # Read config.integration_branch from backlog.json (optional field).
        configured_branch: Optional[str] = None
        try:
            with open(backlog_path, "r", encoding="utf-8") as _bf:
                _bd = json.load(_bf)
            configured_branch = _bd.get("config", {}).get("integration_branch") or None
        except Exception:
            pass  # malformed JSON or missing file — proceed without it

        # Detect trunk: config.integration_branch → origin/HEAD → main → master
        trunk = _detect_default_branch(git_cwd=git_cwd, integration_branch=configured_branch)
        if trunk == _MISSING_BRANCH:
            ok.append(
                f"config.integration_branch '{configured_branch}' does not exist in this "
                f"repository — falling back to auto-detection"
            )
            trunk = _detect_default_branch(git_cwd=git_cwd)

        if not trunk:
            ok.append("no trunk branch found (tried origin/HEAD, main, master) — skipping divergence check")
            return

        # If we're already on trunk, no divergence is possible
        if current_branch == trunk:
            ok.append(f"backlog.json is on the trunk branch ({trunk}) — no divergence risk")
            return

        # Compare committed versions: git show <trunk>:<relpath> vs git show HEAD:<relpath>
        def _git_show(ref: str) -> Optional[str]:
            r = subprocess.run(
                ["git", "show", f"{ref}:{rel_path}"],
                **run_kw,
            )
            return r.stdout if r.returncode == 0 else None

        trunk_content = _git_show(trunk)
        head_content = _git_show("HEAD")

        if trunk_content is None and head_content is None:
            ok.append("backlog.json not committed on either branch — skipping divergence check")
            return

        if trunk_content is None:
            ok.append(f"backlog.json not committed on {trunk} — skipping divergence check")
            return

        if head_content is None:
            ok.append(f"backlog.json not committed on HEAD ({current_branch}) — skipping divergence check")
            return

        if trunk_content == head_content:
            ok.append(f"backlog.json committed version matches {trunk} — no divergence")
        else:
            issues.append(
                f"backlog.json committed on '{current_branch}' differs from '{trunk}' — "
                f"risk of forked backlog state. Use a canonical-copy worktree: "
                f"`git worktree add ../<repo>-backlog {trunk}` and point all agents at "
                f"that copy with BACKLOG_FILE."
            )

    except Exception as exc:
        ok.append(f"divergence check skipped ({exc})")


def _check_trunk_ahead_of_origin(
    backlog_path: Path,
    ok: list,
    issues: list,
) -> None:
    """Warn when local trunk has backlog-only commits not yet on origin trunk.

    Read-only: never fetches, never mutates git state.
    Degrades gracefully (appends a note to ok) when:
      - outside a git repo
      - trunk detection returns empty
      - no origin remote or origin/<trunk> ref absent
      - backlog.json is outside the repo root
      - any subprocess error

    All git commands run with cwd set to the directory containing backlog.json
    so that git resolves to the correct repo regardless of the process's cwd.
    """
    git_cwd = backlog_path.resolve().parent
    run_kw: dict = dict(capture_output=True, text=True, timeout=5, cwd=git_cwd)

    try:
        # Is this a git repo?
        repo_check = subprocess.run(["git", "rev-parse", "--git-dir"], **run_kw)
        if repo_check.returncode != 0:
            ok.append("not in a git repo — skipping trunk-ahead check")
            return

        # Find the repo root so we can compute the repo-relative backlog.json path
        root_result = subprocess.run(["git", "rev-parse", "--show-toplevel"], **run_kw)
        if root_result.returncode != 0:
            ok.append("could not determine git root — skipping trunk-ahead check")
            return
        git_root = Path(root_result.stdout.strip())

        try:
            rel_path = backlog_path.resolve().relative_to(git_root)
        except ValueError:
            ok.append("backlog.json is outside the git repo root — skipping trunk-ahead check")
            return

        # Read config.integration_branch from backlog.json (optional field).
        configured_branch: Optional[str] = None
        try:
            with open(backlog_path, "r", encoding="utf-8") as _bf:
                _bd = json.load(_bf)
            configured_branch = _bd.get("config", {}).get("integration_branch") or None
        except Exception:
            pass  # malformed JSON or missing file — proceed without it

        # Detect trunk: config.integration_branch → origin/HEAD → main → master
        trunk = _detect_default_branch(git_cwd=git_cwd, integration_branch=configured_branch)
        if trunk == _MISSING_BRANCH:
            ok.append(
                f"config.integration_branch '{configured_branch}' does not exist in this "
                f"repository — skipping trunk-ahead check"
            )
            trunk = _detect_default_branch(git_cwd=git_cwd)

        if not trunk:
            ok.append("no trunk branch found (tried origin/HEAD, main, master) — skipping trunk-ahead check")
            return

        # Verify that origin/<trunk> exists as a ref (do NOT fetch)
        origin_ref = f"origin/{trunk}"
        verify_result = subprocess.run(
            ["git", "rev-parse", "--verify", origin_ref],
            **run_kw,
        )
        if verify_result.returncode != 0:
            ok.append(f"no origin remote or '{origin_ref}' ref absent — skipping trunk-ahead check")
            return

        # Count commits local trunk is ahead of origin trunk
        ahead_result = subprocess.run(
            ["git", "rev-list", f"{origin_ref}..{trunk}"],
            **run_kw,
        )
        if ahead_result.returncode != 0:
            ok.append(f"trunk-ahead check skipped (rev-list failed)")
            return

        ahead_commits = [line for line in ahead_result.stdout.splitlines() if line.strip()]
        if not ahead_commits:
            ok.append(f"local {trunk} is in sync with {origin_ref}")
            return

        n = len(ahead_commits)

        # Determine whether the drift is backlog-only
        diff_result = subprocess.run(
            ["git", "diff", "--name-only", f"{origin_ref}..{trunk}"],
            **run_kw,
        )
        if diff_result.returncode != 0:
            ok.append(f"trunk-ahead check skipped (diff failed)")
            return

        changed_paths = [line for line in diff_result.stdout.splitlines() if line.strip()]
        backlog_rel = str(rel_path)

        if changed_paths == [backlog_rel]:
            # Backlog-only drift — this is the problem case
            issues.append(
                f"local {trunk} is ahead of {origin_ref} by {n} backlog-only commit(s) — "
                f"branches cut from {trunk} will carry backlog.json into their PR diff "
                f"against origin. Diff PRs against {origin_ref}, or land backlog state "
                f"to origin before branching."
            )
        else:
            # Normal unpushed code (may include backlog.json but also other files) — neutral
            ok.append(
                f"local {trunk} is ahead of {origin_ref} by {n} commit(s) incl. non-backlog changes"
            )

    except Exception as exc:
        ok.append(f"trunk-ahead check skipped ({exc})")


@app.command()
def doctor(
    fix: bool = typer.Option(False, "--fix", help="Write missing setup to CLAUDE.md"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Check (and optionally fix) project setup so agents use the backlog automatically."""
    cwd = Path.cwd()
    issues: list[str] = []
    ok: list[str] = []

    # ── 1. backlog.json ───────────────────────────────────────────────────────
    if file:
        backlog_path: Optional[Path] = Path(file)
        if not backlog_path.exists():
            backlog_path = None
    else:
        env_path = os.environ.get("BACKLOG_FILE")
        backlog_path = Path(env_path) if env_path and Path(env_path).exists() else _find_backlog_file(cwd)

    if backlog_path:
        rel = backlog_path.relative_to(cwd) if backlog_path.is_relative_to(cwd) else backlog_path
        ok.append(f"backlog.json found at {rel}")
    else:
        issues.append("backlog.json not found — run `backlog init` to create one")

    # ── 1b. Schema compatibility ──────────────────────────────────────────────
    if backlog_path:
        try:
            with open(backlog_path, "r") as f:
                raw_data = json.load(f)
            schema_type, schema_desc = _detect_foreign_schema(raw_data)
            if schema_type != "flow":
                if fix:
                    backup_path = backlog_path.with_suffix(".json.bak")
                    shutil.copy2(backlog_path, backup_path)
                    migrated = migrate_to_flow_schema(raw_data)
                    BacklogStore(str(backlog_path)).write(migrated)
                    ok.append(
                        f"backlog.json migrated from foreign schema ({schema_desc}) — "
                        f"backup saved to {backup_path.name}"
                    )
                else:
                    issues.append(
                        f"backlog.json has incompatible schema ({schema_desc}) — "
                        f"Flow commands will fail. Run `backlog doctor --fix` to migrate "
                        f"(a .bak backup will be created automatically)."
                    )
        except json.JSONDecodeError:
            issues.append("backlog.json is not valid JSON — cannot read or migrate it")

    # ── 2. CLAUDE.md snippet ─────────────────────────────────────────────────
    claude_md = _find_claude_md(cwd)
    snippet_ok = claude_md is not None and _snippet_present(claude_md)

    if snippet_ok:
        ok.append("CLAUDE.md has Flow setup — agents will use the backlog automatically")
    else:
        if fix:
            target = claude_md or (cwd / "CLAUDE.md")
            try:
                action = _ensure_snippet(target)
            except OSError as exc:
                issues.append(f"Could not update {target.name}: {exc}")
                action = "error"
            if action == "wrapped":
                ok.append(
                    f"CLAUDE.md already has a Flow Backlog section — added markers so it's recognised on future runs"
                )
            elif action == "appended":
                ok.append(f"CLAUDE.md updated — Flow setup written to {target.name}")
            elif action == "noop":
                ok.append("CLAUDE.md has Flow setup — agents will use the backlog automatically")
        else:
            issues.append(
                "CLAUDE.md missing Flow setup — agents won't know to use the backlog. "
                "Run `backlog doctor --fix` to add it."
            )

    # ── 3. Pre-commit hook ────────────────────────────────────────────────────
    hook_repo_dir = (backlog_path.resolve().parent if backlog_path else cwd)
    hook_state = _doctor_hook_state(hook_repo_dir)
    if hook_state == "ok":
        ok.append("pre-commit hook installed and up to date")
    elif hook_state == "outdated":
        if fix:
            status, _ = _install_precommit_hook(hook_repo_dir)
            if status == "refreshed":
                ok.append("pre-commit hook refreshed to latest version")
            else:
                ok.append(f"pre-commit hook refresh attempted (status: {status})")
        else:
            issues.append(
                "pre-commit hook is outdated (flow-managed but stale). "
                "Run `backlog doctor --fix` or `backlog install-hook` to refresh."
            )
    elif hook_state == "missing":
        if fix:
            status, _ = _install_precommit_hook(hook_repo_dir)
            if status == "installed":
                ok.append("pre-commit hook installed")
            else:
                ok.append(f"pre-commit hook install attempted (status: {status})")
        else:
            issues.append(
                "pre-commit hook not installed — backlog.json commits are unguarded. "
                "Run `backlog doctor --fix` or `backlog install-hook` to install."
            )
    elif hook_state == "foreign":
        ok.append(
            "pre-commit hook exists but was not installed by Flow — not modified "
            "(add the backlog gate manually if needed)"
        )
    else:
        # skipped / not a git repo
        ok.append("pre-commit hook check skipped (not a git repo or git not available)")

    # ── 4. BACKLOG_FILE env var ───────────────────────────────────────────────
    if os.environ.get("BACKLOG_FILE"):
        ok.append(f"BACKLOG_FILE env var set → {os.environ['BACKLOG_FILE']}")
    else:
        ok.append("BACKLOG_FILE env var not set — defaulting to ./backlog.json (no config needed)")

    # ── 5. Multi-branch divergence check ─────────────────────────────────────
    if backlog_path:
        _check_backlog_divergence(backlog_path, ok, issues)

    # ── 6. Trunk-ahead-of-origin check ───────────────────────────────────────
    if backlog_path:
        _check_trunk_ahead_of_origin(backlog_path, ok, issues)

    # ── Report ────────────────────────────────────────────────────────────────
    console.print()
    for msg in ok:
        console.print(f"  [green]✓[/green] {msg}")
    for msg in issues:
        console.print(f"  [red]✗[/red] {msg}")

    if not issues:
        console.print("\n[green]All good.[/green] Agents on this project will use the backlog automatically.\n")
    elif fix and not any("backlog.json" in i for i in issues):
        console.print("\n[green]Fixed.[/green] Commit CLAUDE.md so all agents on this project pick it up.\n")
    else:
        console.print()
        raise typer.Exit(1)


@app.command()
def board(
    file: Optional[str] = FILE_OPT,
    port: int = typer.Option(8089, "--port", help="Port for the web board"),
) -> None:
    """Launch the web board (starts backlog-server)."""
    resolved = _resolve_file(file)
    env = os.environ.copy()
    env["BACKLOG_FILE"] = resolved
    server_args = [
        "--file", resolved,
        "--port", str(port),
    ]
    try:
        subprocess.run(["backlog-server", *server_args], env=env)
    except FileNotFoundError:
        # Fallback: try running the server script directly
        script = Path(__file__).parent.parent / "scripts" / "backlog_server.py"
        subprocess.run([sys.executable, str(script), *server_args], env=env)


def _validate_backlog_file(abs_path: str) -> None:
    """Validate backlog.json before staging or committing.

    Raises typer.Exit(1) with a clear error message if:
    - The file contains git conflict markers anchored to the start of a line.
    - The file cannot be parsed as JSON.

    Conflict-marker check runs FIRST so that a real conflicted file (which is
    also invalid JSON) produces the specific conflict-marker diagnostic with
    line numbers rather than a generic JSON parse error.

    A description containing ``=======`` mid-line is NOT a conflict marker and
    must pass validation without error.
    """
    import re

    try:
        raw = Path(abs_path).read_text(encoding="utf-8")
    except OSError as exc:
        err_console.print(f"[red]Error:[/red] cannot read {abs_path}: {exc}")
        raise typer.Exit(1)

    # Conflict-marker check — anchored to line start to avoid false positives.
    # Runs BEFORE JSON parsing so a conflicted file gets the marker diagnostic,
    # not a generic "not valid JSON" error.
    # Match lines that start with "<<<<<<< " or ">>>>>>> ", or are exactly "=======".
    bad_lines = [
        i + 1
        for i, line in enumerate(raw.splitlines())
        if re.match(r"^(<<<<<<< |>>>>>>> |=======\s*$)", line)
    ]
    if bad_lines:
        lines_str = ", ".join(str(n) for n in bad_lines)
        err_console.print(
            f"[red]Error:[/red] {abs_path} contains git conflict markers "
            f"at line(s): {lines_str}. Resolve conflicts before committing."
        )
        raise typer.Exit(1)

    # JSON validity check — second gate for marker-free but malformed files.
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        err_console.print(
            f"[red]Error:[/red] {abs_path} is not valid JSON: {exc}"
        )
        raise typer.Exit(1)


@app.command(name="commit")
@_handle
def commit_cmd(
    file: Optional[str] = FILE_OPT,
    message: str = typer.Option(
        "chore(backlog): checkpoint", "--message", "-m", help="Commit message"
    ),
) -> None:
    """Commit backlog.json into git history (deliberate durability checkpoint).

    Commits ONLY the backlog file — other staged/unstaged changes are left
    untouched. No-op (exit 0) when the file already matches HEAD. Works on any
    branch, including main. Run this before a manual git checkout/stash/reset.
    """
    abs_path = os.path.abspath(_resolve_file(file))
    repo_dir = os.path.dirname(abs_path) or "."

    def _git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
        )

    # Validate BEFORE staging — a corrupt file must never be committed.
    _validate_backlog_file(abs_path)

    try:
        # Stage only the backlog file.
        add = _git("add", "--", abs_path)
        if add.returncode != 0:
            stderr = add.stderr.strip()
            if "not a git repository" in stderr.lower():
                err_console.print(
                    "[red]Error:[/red] not a git repository — run this inside a git repo."
                )
            else:
                err_console.print(f"[red]Error:[/red] git add failed: {stderr or 'unknown error'}")
            raise typer.Exit(1)

        # No-op if nothing staged for this file differs from HEAD.
        diff = _git("diff", "--cached", "--quiet", "--", abs_path)
        if diff.returncode == 0:
            console.print("nothing to commit — backlog.json is already up to date")
            raise typer.Exit(0)

        # Commit ONLY this file (pathspec restricts the commit to it).
        committed = _git("commit", "-m", message, "--", abs_path)
        if committed.returncode != 0:
            stderr = committed.stderr.strip() or committed.stdout.strip()
            err_console.print(f"[red]Error:[/red] git commit failed: {stderr or 'unknown error'}")
            raise typer.Exit(1)

        short = _git("rev-parse", "--short", "HEAD")
        short_hash = short.stdout.strip() if short.returncode == 0 else "?"
        console.print(f"[green]Committed[/green] {short_hash} {message}")
    except FileNotFoundError:
        err_console.print(
            "[red]Error:[/red] git not found — install git to use backlog commit."
        )
        raise typer.Exit(1)


@app.command()
@_handle
def handoff(
    agent: str = typer.Argument(..., help="Agent name (e.g. backend-dev)"),
    file: Optional[str] = FILE_OPT,
    item: Optional[int] = typer.Option(None, "--item", help="Force specific item by position"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print prompt without invoking claude"),
    review: bool = typer.Option(False, "--review", help="Review mode: prompt asks for pass/reject verdict, not work completion"),
) -> None:
    """Assemble a structured work brief and hand it off to claude CLI.

    Use --review when handing off to a reviewer agent — the prompt and output
    contract change to ask for a code review verdict instead of work completion.
    """
    import datetime
    import shutil

    store = _require_store(file)
    data = store.read()
    backlog_path = store.file_path

    # ── Resolve item ──────────────────────────────────────────────────────────
    if item is not None:
        items = data.get("items", [])
        idx = item - 1
        if idx < 0 or idx >= len(items):
            err_console.print(f"[red]Error:[/red] Item #{item} not found.")
            raise typer.Exit(1)
        target = items[idx]
        tribunal_info = None
        pick_reason = f"Forced via --item {item}"
    else:
        # Use compute_pulse to pick top recommendation
        try:
            from .server import compute_pulse
            pulse = compute_pulse(data, agent_name=agent, backlog_path=backlog_path)
            rec = pulse.get("recommendation", {})
            picked = rec.get("picked")
            if not picked:
                err_console.print("[red]Error:[/red] No ready items available for this agent.")
                raise typer.Exit(1)
            target_id = picked.get("item_id")
            items_by_id = {i.get("id"): i for i in data.get("items", [])}
            target = items_by_id.get(target_id)
            if not target:
                err_console.print(f"[red]Error:[/red] Pulse returned unknown item id {target_id!r}.")
                raise typer.Exit(1)
            tribunal_info = rec
            pick_reason = picked.get("reasoning") or "Tribunal recommendation"
        except ImportError:
            # Fallback: pick first ready item
            ready = [i for i in data.get("items", []) if i.get("status") == "ready"]
            if not ready:
                err_console.print("[red]Error:[/red] No ready items in the backlog.")
                raise typer.Exit(1)
            target = ready[0]
            tribunal_info = None
            pick_reason = "First ready item (pulse unavailable)"

    # ── Load agent persona ────────────────────────────────────────────────────
    backlog_dir = Path(backlog_path).parent
    # Walk up to find .claude/agents/
    persona_text = ""
    search_dir = backlog_dir
    for _ in range(5):
        persona_path = search_dir / ".claude" / "agents" / f"{agent}.md"
        if persona_path.exists():
            persona_text = persona_path.read_text(encoding="utf-8")
            break
        parent = search_dir.parent
        if parent == search_dir:
            break
        search_dir = parent

    # ── Resolve linked items ──────────────────────────────────────────────────
    items_by_id = {i.get("id"): i for i in data.get("items", [])}
    links = target.get("links", [])
    linked_summaries = []
    for link in links:
        linked_id = link.get("item_id", "")
        linked_item = items_by_id.get(linked_id)
        if linked_item:
            linked_summaries.append({
                "id": linked_id,
                "title": linked_item.get("title", ""),
                "status": linked_item.get("status", ""),
                "type": link.get("type", ""),
                "reason": link.get("reason", ""),
            })
        else:
            linked_summaries.append({
                "id": linked_id,
                "title": "(unknown)",
                "status": "(unknown)",
                "type": link.get("type", ""),
                "reason": link.get("reason", ""),
            })

    # ── Assemble prompt ───────────────────────────────────────────────────────
    item_id = target.get("id", "")

    # Build branch slug: title lowercased, spaces→hyphens, max 40 chars, alphanumeric+hyphens only
    import re as _re
    _slug_raw = target.get("title", "").lower().replace(" ", "-")
    _slug_clean = _re.sub(r"[^a-z0-9\-]", "", _slug_raw)[:40].strip("-")
    branch_name = f"feat/item-{item_id}-{_slug_clean}"

    if review:
        output_contract = json.dumps({
            "item_id": item_id,
            "verdict": "pass | reject",
            "summary": "...",
            "issues": [{"description": "...", "severity": "blocker | warning"}],
        }, indent=2)
    else:
        output_contract = json.dumps({
            "item_id": item_id,
            "status": "done | blocked | partial",
            "summary": "...",
            "bugs_found": [{"title": "...", "description": "..."}],
            "follow_ups": [{"title": "...", "description": "..."}],
            "blocker": "... (only if status=blocked)",
            "branch_name": branch_name,
        }, indent=2)

    lines = []
    lines.append("# Review Brief" if review else "# Work Brief")
    lines.append("")
    if persona_text:
        lines.append("## Agent Persona")
        lines.append(persona_text.strip())
        lines.append("")

    if review:
        lines.append("## Your Role")
        lines.append(
            "You are the code reviewer for this item. Read the description and acceptance "
            "criteria carefully. Your job is to decide: is this item shippable as described?\n"
            "- `pass` — implementation is correct, complete, and safe to merge\n"
            "- `reject` — you found a real issue that must be fixed before done\n\n"
            "Do not pass and log a follow-up for a known bug. If you can see it, block it."
        )
        lines.append("")

    lines.append("## Item")
    lines.append(f"**Title**: {target.get('title', '')}")
    lines.append(f"**Item ID**: {item_id}")
    lines.append(f"**Status**: {target.get('status', '')}")
    lines.append(f"**Complexity**: {target.get('complexity', '')}")
    lines.append(f"**Tags**: {', '.join(target.get('tags', []))}")
    lines.append("")
    lines.append("**Description**:")
    lines.append(target.get("description", "(no description)"))
    lines.append("")

    if linked_summaries:
        lines.append("## Linked Items")
        for ls in linked_summaries:
            lines.append(f"- [{ls['type']}] **{ls['title']}** (id={ls['id']}, status={ls['status']})")
            if ls.get("reason"):
                lines.append(f"  Reason: {ls['reason']}")
        lines.append("")

    if not review:
        lines.append("## Why This Item Was Picked")
        lines.append(pick_reason)
        if tribunal_info:
            picked_info = tribunal_info.get("picked") or {}
            lenses = picked_info.get("supporting_lenses", [])
            if lenses:
                lines.append("")
                lines.append("### Lens scores")
                for lens in lenses:
                    lines.append(f"- **{lens.get('lens', '')}**: weight={lens.get('weight', '')} — {lens.get('argument', '')}")
        lines.append("")
        lines.append("## Git Instructions")
        lines.append(f"1. At the start of your work, create branch `{branch_name}` from the current HEAD.")
        lines.append(f"   ```")
        lines.append(f"   git checkout -b {branch_name}")
        lines.append(f"   ```")
        lines.append(f"2. Do all work on that branch — do NOT commit to main.")
        lines.append(f"3. Before finishing, commit everything with:")
        lines.append(f"   ```")
        lines.append(f"   git add -A && git commit -m \"feat(item-{item_id}): {target.get('title', '')}\"")
        lines.append(f"   ```")
        lines.append(f"4. Do NOT push — commits stay local.")
        lines.append("")

    if review:
        item_branch = target.get("metadata", {}).get("branch_name", branch_name)
        lines.append("## Branch")
        lines.append(f"Branch: `{item_branch}`")
        lines.append("")
        lines.append(
            "Check out this branch and read the full implementation. "
            "Do not limit yourself to the diff — navigate the code as a senior engineer would."
        )
        lines.append("")

    lines.append("## Output Contract")
    lines.append(
        "When you finish, write ONLY the following JSON to stdout (no extra text before or after):"
    )
    lines.append("")
    lines.append("```json")
    lines.append(output_contract)
    lines.append("```")
    lines.append("")
    if review:
        lines.append(
            "Fields: `item_id` (string), `verdict` (pass|reject), "
            "`summary` (string), `issues` (array — empty on pass, blockers listed on reject)."
        )
    else:
        lines.append(
            "Fields: `item_id` (string), `status` (done|blocked|partial), "
            "`summary` (string), `bugs_found` (array), `follow_ups` (array), "
            "`blocker` (string, only if blocked), "
            f"`branch_name` (string — the branch you worked on, must be `{branch_name}`)."
        )

    prompt = "\n".join(lines)

    if dry_run:
        console.print(prompt, markup=False)
        return

    # ── Invoke claude ─────────────────────────────────────────────────────────
    if not shutil.which("claude"):
        err_console.print(
            "[red]Error:[/red] 'claude' CLI not found in PATH. "
            "Install it or use --dry-run to preview the prompt."
        )
        raise typer.Exit(1)

    if os.environ.get("CLAUDE_CODE_ENTRYPOINT"):
        console.print(
            "[yellow]Running inside Claude Code — nested claude sessions are blocked.[/yellow]\n"
            "Printing the review prompt below. Execute this review inline, write the artifact to\n"
            "handoff_results/review_<item_id>_<timestamp>.md, then run: backlog ingest <result_file>\n"
            "\n--- REVIEW PROMPT ---"
        )
        console.print(prompt, markup=False)
        raise typer.Exit(0)

    console.print(f"[dim]Invoking claude for item {target.get('id')} ...[/dim]")
    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True,
            text=True,
        )
    except OSError as e:
        err_console.print(f"[red]Error:[/red] Failed to run claude: {e}")
        raise typer.Exit(1)

    raw_output = result.stdout.strip()

    # ── Parse JSON report ─────────────────────────────────────────────────────
    report = None
    # Try to extract JSON block if claude wraps in markdown
    for candidate in [raw_output]:
        # Strip ```json fences if present
        stripped = candidate
        if "```" in stripped:
            import re
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
            if m:
                stripped = m.group(1)
        try:
            report = json.loads(stripped)
            break
        except json.JSONDecodeError:
            pass

    if report is None:
        err_console.print(
            "[yellow]Warning:[/yellow] Could not parse JSON report from claude output. "
            "Saving raw output."
        )
        report = {
            "item_id": target.get("id"),
            "status": "partial",
            "summary": "Raw output (JSON parse failed)",
            "raw_output": raw_output,
            "bugs_found": [],
            "follow_ups": [],
        }

    # ── Save result ───────────────────────────────────────────────────────────
    results_dir = backlog_dir / "handoff_results"
    results_dir.mkdir(exist_ok=True)
    timestamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    result_file = results_dir / f"{target.get('id')}_{timestamp}.json"
    result_file.write_text(json.dumps(report, indent=2), encoding="utf-8")

    console.print(f"[green]Handoff complete.[/green] Result saved to {result_file}")
    console.print_json(json.dumps(report))


@app.command()
@_handle
def ingest(
    result_file: str = typer.Argument(..., help="Path to handoff result JSON file"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Process a handoff result file and drive the backlog forward automatically."""
    import re as _re

    result_path = Path(result_file)
    if not result_path.exists():
        err_console.print(f"[red]Error:[/red] Result file not found: {result_file}")
        raise typer.Exit(1)

    try:
        raw = result_path.read_text(encoding="utf-8")
    except OSError as e:
        err_console.print(f"[red]Error:[/red] Cannot read result file: {e}")
        raise typer.Exit(1)

    # Strip optional markdown fences
    stripped = raw.strip()
    if "```" in stripped:
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, _re.DOTALL)
        if m:
            stripped = m.group(1)

    try:
        report = json.loads(stripped)
    except json.JSONDecodeError as e:
        err_console.print(f"[red]Error:[/red] Result file is not valid JSON: {e}")
        raise typer.Exit(1)

    store = _require_store(file)
    outcome = store.ingest_result(report)

    if json_out:
        console.print_json(json.dumps(outcome))
        return

    item_id = outcome["item_id"]
    status_applied = outcome["status_applied"]
    next_lane = outcome["next_lane"]

    branch_name_out = outcome.get("branch_name")
    if status_applied == "done":
        branch_note = f" (branch: {branch_name_out})" if branch_name_out else ""
        console.print(
            f"[green]Ingested[/green] item [cyan]{item_id}[/cyan] — "
            f"advanced to [bold]{next_lane}[/bold]{branch_note}"
        )
    else:
        console.print(
            f"[yellow]Ingested[/yellow] item [cyan]{item_id}[/cyan] — "
            f"stays in [bold]{next_lane}[/bold], thread opened (waiting_on=lead, status={status_applied})"
        )

    for ni in outcome.get("new_items", []):
        console.print(
            f"  [dim]+[/dim] [{ni['category']}] {ni['title']} [dim](id={ni['id']})[/dim]"
        )

    console.print(f"[dim]{outcome['note']}[/dim]")


# ── Staged actions (two-stage approval gate) ─────────────────────────────────


@app.command()
@_handle
def staged(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """List pending staged actions for an item."""
    store = _require_store(file)
    _, item = store.get_item(position)
    actions = [a for a in item.get("staged_actions", []) if a.get("status") == "pending"]

    if json_out:
        console.print_json(json.dumps(actions))
        return

    if not actions:
        console.print(f"[dim]No pending staged actions for item #{position}.[/dim]")
        return

    table = Table(box=box.SIMPLE, show_header=True)
    table.add_column("Action ID", style="cyan")
    table.add_column("Type")
    table.add_column("Description")
    table.add_column("Staged By")
    table.add_column("Staged At", style="dim")
    for a in actions:
        table.add_row(
            a.get("id", ""),
            a.get("type", ""),
            a.get("description", ""),
            a.get("staged_by", ""),
            a.get("staged_at", ""),
        )
    console.print(f"\n[bold]Pending staged actions for item #{position}[/bold]")
    console.print(table)


@app.command()
@_handle
def approve(
    position: int = typer.Argument(..., help="Item number"),
    action_id: str = typer.Argument(..., help="Staged action ID"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Approve a pending staged action."""
    store = _require_store(file)
    action = store.approve_action(position, action_id, approved_by="cli")
    console.print(
        f"[green]Approved[/green] action [cyan]{action_id}[/cyan] "
        f"({action.get('type', '')}: {action.get('description', '')})"
    )


@app.command()
@_handle
def reject(
    position: int = typer.Argument(..., help="Item number"),
    action_id: str = typer.Argument(..., help="Staged action ID"),
    reason: Optional[str] = typer.Option(None, "--reason", "-r", help="Rejection reason"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Reject a pending staged action."""
    store = _require_store(file)
    action = store.reject_action(position, action_id, rejected_by="cli", reason=reason)
    msg = f"[red]Rejected[/red] action [cyan]{action_id}[/cyan] ({action.get('type', '')})"
    if reason:
        msg += f" — reason: {reason}"
    console.print(msg)


# ── Link / Unlink commands ────────────────────────────────────────────────────

VALID_LINK_TYPES = ("blocks", "discovered-during", "follow-up", "related")


def _resolve_item_ref(data: dict, ref: str) -> tuple[int, dict]:
    """Resolve *ref* to (1-based position, item dict).

    *ref* may be either a 1-based position number (e.g. "3") or an 8-char item ID.
    Raises ItemNotFoundError if not found.
    """
    from .exceptions import ItemNotFoundError as _INF
    items = data.get("items", [])
    # Try position number first
    if ref.isdigit():
        pos = int(ref)
        idx = pos - 1
        if 0 <= idx < len(items):
            return pos, items[idx]
        raise _INF(f"Item #{pos} not found (backlog has {len(items)} item(s)).")
    # Fall back to ID match
    for idx, item in enumerate(items):
        if item.get("id") == ref:
            return idx + 1, item
    raise _INF(f"Item {ref!r} not found.")


@app.command(name="link")
@_handle
def link_cmd(
    source: Optional[str] = typer.Argument(None, help="Source item — position number or item ID"),
    file: Optional[str] = FILE_OPT,
    link_type: Optional[str] = typer.Option(None, "--type", "-t", help="Link type: blocks|discovered-during|follow-up|related"),
    target: Optional[str] = typer.Option(None, "--target", help="Target item — position number or item ID"),
    reason: Optional[str] = typer.Option(None, "--reason", "-r", help="One-sentence reason for this link"),
    list_links: Optional[str] = typer.Option(None, "--list", help="List all links for an item (position or ID)", metavar="ITEM"),
) -> None:
    """Add a directional link between two items, or list links for an item.

    Usage:
      backlog link <source> --type <type> --target <target> --reason "<reason>"
      backlog link --list <item>
    """
    store = _require_store(file)
    data = store.read()

    # ── --list mode ───────────────────────────────────────────────────────────
    if list_links is not None:
        pos, item = _resolve_item_ref(data, list_links)
        links = item.get("links", [])
        if not links:
            console.print(f"[dim]No links for item #{pos} \"{item.get('title', '')}\".[/dim]")
            return
        items_by_id = {i.get("id"): i for i in data.get("items", [])}
        console.print(f"\n[bold]Links for #{pos} \"{item.get('title', '')}\"[/bold]")
        for lnk in links:
            tgt_id = lnk.get("item_id", "")
            tgt_item = items_by_id.get(tgt_id)
            tgt_title = tgt_item.get("title", "(unknown)") if tgt_item else "(unknown)"
            ltype = lnk.get("type", "")
            lreason = lnk.get("reason", "")
            console.print(f"  [cyan]{ltype}[/cyan] → {tgt_title} [dim](id={tgt_id})[/dim]")
            if lreason:
                console.print(f"    {lreason}")
        console.print()
        return

    # ── Add-link mode ─────────────────────────────────────────────────────────
    if source is None:
        err_console.print("[red]Error:[/red] SOURCE argument is required when not using --list")
        raise typer.Exit(1)

    if reason is None:
        err_console.print("[red]Error:[/red] --reason is required for link")
        raise typer.Exit(1)

    if link_type is None:
        err_console.print(
            f"[red]Error:[/red] --type is required. "
            f"Valid types: {', '.join(VALID_LINK_TYPES)}"
        )
        raise typer.Exit(1)

    if link_type not in VALID_LINK_TYPES:
        err_console.print(
            f"[red]Error:[/red] Invalid type {link_type!r}. "
            f"Valid types: {', '.join(VALID_LINK_TYPES)}"
        )
        raise typer.Exit(1)

    if target is None:
        err_console.print("[red]Error:[/red] --target is required for link")
        raise typer.Exit(1)

    src_pos, src_item = _resolve_item_ref(data, source)
    tgt_pos, tgt_item = _resolve_item_ref(data, target)

    if src_item.get("id") == tgt_item.get("id"):
        err_console.print("[red]Error:[/red] Cannot link an item to itself")
        raise typer.Exit(1)

    # Duplicate guard: same source+target+type → skip silently
    tgt_id = tgt_item.get("id")
    existing = src_item.setdefault("links", [])
    for lnk in existing:
        if lnk.get("item_id") == tgt_id and lnk.get("type") == link_type:
            console.print(
                f"[yellow]Warning:[/yellow] Link #{src_pos} → #{tgt_pos} ({link_type}) already exists — skipped."
            )
            return

    existing.append({"item_id": tgt_id, "type": link_type, "reason": reason})
    src_item["updated_at"] = _now_iso()
    store.write(data, expected_version=data.get("version", 0))
    console.print(
        f"[green]Linked[/green] #{src_pos} → #{tgt_pos} ({link_type}): {reason}"
    )


@app.command(name="unlink")
@_handle
def unlink_cmd(
    source: str = typer.Argument(..., help="Source item — position number or item ID"),
    file: Optional[str] = FILE_OPT,
    target: str = typer.Option(..., "--target", help="Target item — position number or item ID"),
) -> None:
    """Remove a link from source item to target item."""
    store = _require_store(file)
    data = store.read()

    src_pos, src_item = _resolve_item_ref(data, source)
    tgt_pos, tgt_item = _resolve_item_ref(data, target)

    tgt_id = tgt_item.get("id")
    links = src_item.get("links", [])
    new_links = [lnk for lnk in links if lnk.get("item_id") != tgt_id]

    if len(new_links) == len(links):
        err_console.print(
            f"[red]Error:[/red] No link found from #{src_pos} to #{tgt_pos}"
        )
        raise typer.Exit(1)

    src_item["links"] = new_links
    src_item["updated_at"] = _now_iso()
    store.write(data, expected_version=data.get("version", 0))
    console.print(f"[green]Unlinked[/green] #{src_pos} → #{tgt_pos}")


@app.command(name="ref")
@_handle
def ref_cmd(
    item: Optional[str] = typer.Argument(None, help="Item — position number or item ID"),
    file: Optional[str] = FILE_OPT,
    system: Optional[str] = typer.Option(None, "--system", help="External system identifier (jira, github, linear, ...)"),
    ext_id: Optional[str] = typer.Option(None, "--id", help="Ticket key as the external tool displays it"),
    url: Optional[str] = typer.Option(None, "--url", help="URL for the external ticket (optional — enables clickable link on board)"),
    list_refs: Optional[str] = typer.Option(None, "--list", help="List all external refs for an item (position or ID)", metavar="ITEM"),
) -> None:
    """Add an external ticket reference to an item, or list refs for an item.

    Usage:
      backlog ref <item> --system jira --id PROJ-123 [--url https://...]
      backlog ref --list <item>
    """
    store = _require_store(file)
    data = store.read()

    # ── --list mode ───────────────────────────────────────────────────────────
    if list_refs is not None:
        pos, target_item = _resolve_item_ref(data, list_refs)
        refs = target_item.get("external_refs", [])
        if not refs:
            console.print(f"[dim]No external refs for item #{pos} \"{target_item.get('title', '')}\".[/dim]")
            return
        console.print(f"\n[bold]External refs for #{pos} \"{target_item.get('title', '')}\"[/bold]")
        for r in refs:
            sys_name = r.get("system", "")
            ref_id = r.get("id", "")
            ref_url = r.get("url", "")
            if ref_url:
                console.print(f"  [cyan]{sys_name}[/cyan] [bold]{ref_id}[/bold] {ref_url}")
            else:
                console.print(f"  [cyan]{sys_name}[/cyan] [bold]{ref_id}[/bold]")
        console.print()
        return

    # ── Add-ref mode ──────────────────────────────────────────────────────────
    if item is None:
        err_console.print("[red]Error:[/red] ITEM argument is required when not using --list")
        raise typer.Exit(1)

    if system is None:
        err_console.print("[red]Error:[/red] --system is required for ref")
        raise typer.Exit(1)

    if ext_id is None:
        err_console.print("[red]Error:[/red] --id is required for ref")
        raise typer.Exit(1)

    pos, target_item = _resolve_item_ref(data, item)

    new_ref: dict = {"system": system, "id": ext_id}
    if url is not None:
        new_ref["url"] = url

    target_item.setdefault("external_refs", []).append(new_ref)
    target_item["updated_at"] = _now_iso()
    store.write(data, expected_version=data.get("version", 0))
    console.print(
        f"[green]Added ref[/green] #{pos} → {system}:{ext_id}"
        + (f" ({url})" if url else "")
    )


@app.command(name="unref")
@_handle
def unref_cmd(
    item: str = typer.Argument(..., help="Item — position number or item ID"),
    file: Optional[str] = FILE_OPT,
    target: str = typer.Option(..., "--target", help="External ref id to remove (all entries with this id are removed)"),
) -> None:
    """Remove an external ticket reference from an item by its id."""
    store = _require_store(file)
    data = store.read()

    pos, target_item = _resolve_item_ref(data, item)

    refs = target_item.get("external_refs", [])
    new_refs = [r for r in refs if r.get("id") != target]

    if len(new_refs) == len(refs):
        err_console.print(
            f"[red]Error:[/red] No ref found on item #{pos} with id '{target}'"
        )
        raise typer.Exit(1)

    target_item["external_refs"] = new_refs
    target_item["updated_at"] = _now_iso()
    store.write(data, expected_version=data.get("version", 0))
    removed = len(refs) - len(new_refs)
    console.print(f"[green]Removed {removed} ref(s)[/green] from #{pos} with id '{target}'")


# ── Orchestrator helpers ──────────────────────────────────────────────────────


def _get_lead_agent(data: dict) -> Optional[tuple]:
    """Return (name, cfg) of the agent with role='lead', or None if none configured.

    Raises SystemExit with a clear message if more than one agent has role='lead'
    — ambiguity in who is lead would cause unpredictable orchestrator behavior.
    """
    agents_cfg = data.get("config", {}).get("agents", {})
    leads = [(name, cfg) for name, cfg in agents_cfg.items() if cfg.get("role") == "lead"]
    if len(leads) > 1:
        names = ", ".join(name for name, _ in leads)
        err_console.print(
            f"[red]Error:[/red] Multiple agents configured as lead: {names}\n"
            "Exactly one agent may have role='lead'. "
            "Update config.agents in backlog.json and restart."
        )
        raise typer.Exit(1)
    return leads[0] if leads else None


def _get_orchestrator_mode(data: dict, mode_override: Optional[str] = None) -> str:
    """Return orchestrator mode: 'supervised' (default) or 'auto'."""
    if mode_override:
        return mode_override
    return data.get("config", {}).get("orchestrator", {}).get("mode", "supervised")


def _assess_complexity(
    title: str,
    description: str,
    tags: list,
    log_prefix: str = "[auto]",
) -> tuple:
    """Ask Claude to classify item complexity as 'simple' or 'complex' with a reason.

    Heuristics used in the prompt:
    - complex: touches auth/security/data integrity, spans multiple components,
               introduces new patterns, has unclear edge cases, or estimate is high
    - simple:  single-file change, well-understood pattern, clear acceptance
               criteria, estimate is low or medium

    Returns (label, reason) where label is 'simple' or 'complex'.
    Falls back to 'complex' if the call fails (safer default).
    """
    import re

    tags_str = ", ".join(tags) if tags else "(none)"
    prompt = (
        "You are a lead agent classifying a backlog item's complexity before refinement closes.\n\n"
        f"Item title: {title}\n"
        f"Item description:\n{description or '(no description)'}\n"
        f"Tags: {tags_str}\n\n"
        "Classify as 'simple' or 'complex' using these heuristics:\n"
        "  complex — touches auth/security/data integrity, spans multiple components,\n"
        "            introduces new patterns, has unclear edge cases, or estimate is high\n"
        "  simple  — single-file change, well-understood pattern, clear acceptance\n"
        "            criteria, estimate is low or medium\n\n"
        'Reply with JSON only: {"complexity": "simple" | "complex", "reason": "<one-line reason>"}'
    )

    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True, text=True, timeout=60,
        )
        m = re.search(r'\{.*?\}', result.stdout, re.DOTALL)
        if not m:
            console.print(f"{log_prefix} could not parse complexity response — defaulting to complex")
            return ("complex", "parse error — defaulting to complex (safer)")
        parsed = json.loads(m.group(0))
        label = parsed.get("complexity", "complex")
        if label not in ("simple", "complex"):
            label = "complex"
        reason = parsed.get("reason", "")
        return (label, reason)
    except Exception as e:
        console.print(f"{log_prefix} complexity assessment error: {e} — defaulting to complex")
        return ("complex", f"error: {e} — defaulting to complex")


def _run_subleadagent_review(
    title: str,
    description: str,
    tags: list,
    log_prefix: str = "[auto]",
) -> tuple:
    """Run a focused readiness review for complex items via Claude sub-lead-agent.

    Checklist evaluated:
    1. Acceptance criteria are testable, not vague
    2. No hidden dependencies on unbuilt or unplanned pieces
    3. Estimate is realistic given typical codebase scope
    4. Edge cases and failure modes are called out
    5. Scope is clearly bounded with no implicit follow-on work

    Returns (passed: bool, findings: list[str]).
    findings contains blocker strings if passed=False, or praise if passed=True.
    Falls back to (True, []) if Claude is unavailable — do not block on tool absence.
    """
    tags_str = ", ".join(tags) if tags else "(none)"
    checklist = (
        "1. Are acceptance criteria testable and specific (not vague promises)?\n"
        "2. Are there hidden dependencies on unbuilt or unplanned pieces?\n"
        "3. Is the estimate realistic given a typical medium-sized codebase?\n"
        "4. Are edge cases and failure modes explicitly called out?\n"
        "5. Is scope clearly bounded with no implicit follow-on work?"
    )
    prompt = (
        "You are a sub-lead agent running a focused readiness review for a complex backlog item.\n"
        "Your job is to decide whether this item is ready to be worked on — not to review code.\n\n"
        f"Item title: {title}\n"
        f"Item description:\n{description or '(no description)'}\n"
        f"Tags: {tags_str}\n\n"
        f"Readiness checklist:\n{checklist}\n\n"
        "For each checklist item, note pass or fail. Then give an overall verdict.\n\n"
        "Reply with JSON only:\n"
        '{"passed": true | false, "findings": ["<finding1>", "<finding2>"]}\n'
        "findings = list of problems if passed=false, or list of strengths if passed=true.\n"
        "Keep each finding to one sentence."
    )

    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True, text=True, timeout=90,
        )
        text = result.stdout
        # Robust JSON extraction: find outermost { } via bracket counting
        # so that findings strings containing { or } don't truncate the match.
        start = text.find('{')
        if start == -1:
            console.print(f"{log_prefix} sub-lead review: could not parse response — treating as passed")
            return (True, [])
        depth = 0
        end = -1
        for idx in range(start, len(text)):
            if text[idx] == '{':
                depth += 1
            elif text[idx] == '}':
                depth -= 1
                if depth == 0:
                    end = idx
                    break
        if end == -1:
            console.print(f"{log_prefix} sub-lead review: could not parse response — treating as passed")
            return (True, [])
        parsed = json.loads(text[start:end + 1])
        passed = bool(parsed.get("passed", True))
        findings = parsed.get("findings", [])
        return (passed, findings)
    except Exception as e:
        console.print(f"{log_prefix} sub-lead review error: {e} — treating as passed")
        return (True, [])


def _auto_refine_tick(
    backlog_file: str,
    lead_name: str,
    dry_run: bool,
    log_prefix: str = "[auto]",
) -> None:
    """Auto mode: lead agent picks the highest-priority unstarted item and either
    moves it to ready (if actionable) or opens a thread asking the human blocking
    questions (max 2, most important first).

    After determining readiness the lead agent assigns a complexity label
    ('simple' | 'complex') and asks the human to confirm or override it.
    For complex items a sub-lead-agent readiness review runs before the spec gate.

    Items already waiting for human input (waiting_on='user' unresolved thread)
    are skipped — we don't pile on more questions.
    """
    import shutil

    store = BacklogStore(backlog_file)
    data = store.read()
    items = data.get("items", [])

    # Candidate: highest-priority item in backlog or refined that isn't blocked waiting for human
    candidate = None
    for item in items:
        status = item.get("status", "")
        if status not in ("backlog", "refined"):
            continue
        # Skip if there's already an unresolved thread waiting on the user
        threads = item.get("threads", [])
        if any(t.get("waiting_on") == "user" and not t.get("resolved") for t in threads):
            continue
        candidate = item
        break

    if not candidate:
        return  # Nothing to refine right now

    item_id = candidate.get("id")
    pos = _item_position(data, item_id)
    title = candidate.get("title", "")
    description = candidate.get("description", "")
    tags = candidate.get("tags", [])

    console.print(f"{log_prefix} auto mode — assessing item #{pos} '{title}'")

    if not shutil.which("claude"):
        console.print(f"{log_prefix} claude not found — cannot assess refinement, skipping")
        return

    prompt = (
        f"You are a lead agent reviewing a backlog item to decide if it is ready to start.\n\n"
        f"Item title: {title}\n"
        f"Item description:\n{description or '(no description)'}\n\n"
        f"Is this item actionable as written? "
        f"Could an agent pick it up and complete it without needing clarification?\n\n"
        f"If YES: reply with JSON: {{\"ready\": true}}\n"
        f"If NO: reply with JSON: {{\"ready\": false, \"questions\": [\"<most blocking question>\", \"<second most blocking question>\"]}} "
        f"(include at most 2 questions, most blocking first)"
    )

    try:
        result = subprocess.run(
            ["claude", "--print", prompt],
            capture_output=True, text=True, timeout=60,
        )
        import re
        m = re.search(r'\{.*?\}', result.stdout, re.DOTALL)
        if not m:
            console.print(f"{log_prefix} could not parse Claude response for item #{pos}, skipping")
            return
        parsed = json.loads(m.group(0))
    except Exception as e:
        console.print(f"{log_prefix} error assessing item #{pos}: {e}, skipping")
        return

    if parsed.get("ready"):
        # ── Step 1: assign refinement_gate label ─────────────────────────────
        stored_refinement_gate = candidate.get("refinement_gate")
        if stored_refinement_gate in ("simple", "complex"):
            # Human already set an explicit refinement_gate — honour it, skip Claude call.
            complexity_label = stored_refinement_gate
            complexity_reason = "human override"
        else:
            complexity_label, complexity_reason = _assess_complexity(
                title, description, tags, log_prefix=log_prefix
            )
        console.print(
            f"{log_prefix} item #{pos} refinement_gate → [bold]{complexity_label}[/bold] "
            f"({complexity_reason})"
        )

        # Notify human of the proposed label so they can override before refinement closes
        console.print(
            f"[yellow]REFINEMENT GATE:[/yellow] Item #{pos} '{title}' — "
            f"proposed as [bold]{complexity_label}[/bold]: {complexity_reason}\n"
            f"  To override: backlog edit {pos} --refinement-gate <simple|complex>"
        )

        # Write assessed label back to item (only if not already set by human)
        if not dry_run:
            data2 = store.read()
            items2 = data2.get("items", [])
            target = next((i for i in items2 if i.get("id") == item_id), None)
            if target is not None and not target.get("refinement_gate"):
                from .core import _now_iso
                target["refinement_gate"] = complexity_label
                target["updated_at"] = _now_iso()
                store.write(data2, expected_version=data2.get("version", 0))

        # ── Step 2: for complex items, run sub-lead-agent readiness review ────
        if complexity_label == "complex":
            console.print(
                f"{log_prefix} running sub-lead-agent readiness review for complex item #{pos} …"
            )
            passed, findings = _run_subleadagent_review(
                title, description, tags, log_prefix=log_prefix
            )

            if not passed:
                findings_text = "\n".join(f"- {f}" for f in findings)
                console.print(
                    f"{log_prefix} sub-lead review: item #{pos} has readiness issues — opening thread"
                )
                if not dry_run:
                    data2 = store.read()
                    items2 = data2.get("items", [])
                    target = next((i for i in items2 if i.get("id") == item_id), None)
                    if target is not None:
                        from .core import _now_iso, _generate_id
                        target.setdefault("threads", []).append({
                            "id": _generate_id(),
                            "topic": "Sub-lead readiness review — complex item",
                            "waiting_on": "user",
                            "body": (
                                f"Sub-lead-agent readiness review found issues "
                                f"for this complex item:\n{findings_text}\n\n"
                                "Please address these before refinement closes."
                            ),
                            "created_at": _now_iso(),
                            "resolved": False,
                        })
                        target["updated_at"] = _now_iso()
                        store.write(data2, expected_version=data2.get("version", 0))
                console.print(
                    f"[yellow]NOTIFICATION:[/yellow] Item #{pos} '{title}' failed sub-lead "
                    "readiness review. Issues added as a thread."
                )
                return  # Do not move to ready until issues are resolved

            # Review passed — log praise/notes if any
            if findings:
                console.print(
                    f"{log_prefix} sub-lead review passed for #{pos}. "
                    f"Notes: {'; '.join(findings)}"
                )
            else:
                console.print(f"{log_prefix} sub-lead review passed for #{pos} — no issues found")

        # ── Step 3: move to ready (spec gate applies as usual) ────────────────
        console.print(f"{log_prefix} item #{pos} is actionable → moving to ready")
        if not dry_run:
            try:
                store.move_item(pos, "ready", moved_by=lead_name)
            except Exception as e:
                console.print(f"{log_prefix} could not move item #{pos} to ready: {e}")
    else:
        questions = parsed.get("questions", [])
        if not questions:
            console.print(f"{log_prefix} Claude said not ready but gave no questions for #{pos}, skipping")
            return
        question_text = "\n".join(f"- {q}" for q in questions[:2])
        console.print(
            f"{log_prefix} item #{pos} needs clarification — opening thread with "
            f"{len(questions[:2])} question(s)"
        )
        if not dry_run:
            data2 = store.read()
            items2 = data2.get("items", [])
            target = next((i for i in items2 if i.get("id") == item_id), None)
            if target is not None:
                from .core import _now_iso, _generate_id
                target.setdefault("threads", []).append({
                    "id": _generate_id(),
                    "topic": "Refinement questions from lead agent",
                    "waiting_on": "user",
                    "body": f"To start this item, I need answers to:\n{question_text}",
                    "created_at": _now_iso(),
                    "resolved": False,
                })
                target["updated_at"] = _now_iso()
                store.write(data2, expected_version=data2.get("version", 0))
        console.print(
            f"[yellow]NOTIFICATION:[/yellow] Item #{pos} '{title}' needs your input "
            f"before it can start. Questions added as a thread."
        )


def _select_agent(
    data: dict,
    item: dict,
    exclude: Optional[str] = None,
    for_review: bool = False,
) -> Optional[str]:
    """Pick the best-fit agent from config.agents for an item.

    Scores agents by skill overlap with item tags, penalises agents at their
    max_active limit, excludes the optional *exclude* agent (for reviewer
    selection).

    When for_review=True, agents with role='reviewer' are preferred — they get
    a +10 score bonus to ensure they win over generalist agents unless at capacity.
    Returns None if no suitable agent found.
    """
    agents_cfg = data.get("config", {}).get("agents", {})
    if not agents_cfg:
        return None

    items = data.get("items", [])
    in_progress_counts: dict[str, int] = {
        name: sum(
            1 for i in items
            if i.get("assigned_to") == name and i.get("status") == "in-progress"
        )
        for name in agents_cfg
    }

    item_tags = set(t.lower() for t in item.get("tags", []))

    best_agent: Optional[str] = None
    best_score: float = -1.0

    for name, cfg in agents_cfg.items():
        if name == exclude:
            continue
        max_active = cfg.get("max_active", 2)
        current = in_progress_counts.get(name, 0)
        if current >= max_active:
            continue
        skills = set(s.lower() for s in cfg.get("skills", []))
        overlap = len(skills & item_tags)
        score = overlap - current * 0.1  # prefer idle agents on equal overlap
        if for_review and cfg.get("role") == "reviewer":
            score += 10  # reviewer agent wins by default unless at capacity
        if score > best_score:
            best_score = score
            best_agent = name

    return best_agent


def _semantic_next_action(
    statuses: list,
    current_lane: str,
    previous_lane: str,
    assigned_to: str,
) -> dict:
    """Ask Claude what to do next, falling back to a heuristic if Claude is absent."""
    import shutil

    lane_labels = [s.get("label", s.get("id", "")) for s in statuses]
    if shutil.which("claude"):
        prompt = (
            f'Given this workflow: {lane_labels}\n'
            f'Item is now in lane: "{current_lane}"\n'
            f'Previous lane: "{previous_lane}"\n'
            f'Built by: {assigned_to}\n\n'
            'What should happen next? Reply with JSON:\n'
            '{"action": "work" | "review" | "done" | "wait", "reason": "..."}'
        )
        try:
            result = subprocess.run(
                ["claude", "--print", prompt],
                capture_output=True, text=True, timeout=30,
            )
            import re
            m = re.search(r'\{[^{}]+\}', result.stdout, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
                if parsed.get("action") in ("work", "review", "done", "wait"):
                    return parsed
        except Exception:
            pass

    # Heuristic fallback
    terminal_ids = {s.get("id", "") for s in statuses if s.get("id") in ("done", "discarded")}
    # Find lanes that suggest review by label
    review_keywords = ("review", "qa", "test", "staging")
    current_label = next(
        (s.get("label", current_lane) for s in statuses if s.get("id") == current_lane),
        current_lane,
    ).lower()

    if current_lane in terminal_ids:
        return {"action": "done", "reason": "Item is in terminal lane"}
    if any(kw in current_label for kw in review_keywords):
        return {"action": "review", "reason": f"Lane '{current_lane}' looks like a review lane"}
    return {"action": "work", "reason": "Default: send to work agent"}


def _find_ready_items(data: dict) -> list:
    return [i for i in data.get("items", []) if i.get("status") == "ready"]


def _item_position(data: dict, item_id: str) -> Optional[int]:
    for idx, i in enumerate(data.get("items", [])):
        if i.get("id") == item_id:
            return idx + 1
    return None


def _run_handoff(backlog_file: str, agent: str, pos: int, dry_run: bool, review: bool = False) -> None:
    cmd = ["backlog", "--file", backlog_file, "handoff", agent, "--item", str(pos)]
    if review:
        cmd.append("--review")
    if dry_run:
        console.print(f"[dim]DRY-RUN would invoke:[/dim] {' '.join(cmd)}")
    else:
        subprocess.run(cmd)


def _run_ingest(backlog_file: str, result_file: str, dry_run: bool) -> None:
    cmd = ["backlog", "--file", backlog_file, "ingest", result_file]
    if dry_run:
        console.print(f"[dim]DRY-RUN would invoke:[/dim] {' '.join(cmd)}")
    else:
        subprocess.run(cmd)


def _scan_result_files(backlog_dir: Path) -> List[Path]:
    results_dir = backlog_dir / "handoff_results"
    if not results_dir.exists():
        return []
    return sorted(results_dir.glob("*.json"))


def _review_gate_satisfied(item: dict) -> bool:
    """Return True if a different agent has already reviewed this item.

    Heuristic: lane_history contains at least one entry with a 'by' field
    that differs from item['assigned_to'].
    """
    assigned_to = item.get("assigned_to") or ""
    for entry in item.get("lane_history", []):
        if isinstance(entry, dict):
            by = entry.get("by", "")
            if by and by != assigned_to:
                return True
    return False


def _has_review_lane(statuses: list) -> bool:
    """Return True if any configured lane label suggests a review step."""
    review_keywords = ("review", "qa", "test", "staging")
    for s in statuses:
        label = s.get("label", s.get("id", "")).lower()
        if any(kw in label for kw in review_keywords):
            return True
    return False


def _write_heartbeat(
    backlog_file: str,
    mode: str,
    items_in_flight: List[str],
    pending_result_files: int,
) -> None:
    """Write .orchestrator_state.json atomically (tmp + rename) next to the backlog file."""
    state_path = Path(backlog_file).parent / ".orchestrator_state.json"
    state = {
        "running": True,
        "mode": mode,
        "last_tick": datetime.now(timezone.utc).isoformat(),
        "items_in_flight": items_in_flight,
        "pending_result_files": pending_result_files,
    }
    dir_path = str(state_path.parent)
    try:
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_path)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, str(state_path))
    except OSError:
        pass  # best-effort; don't crash the orchestrator over a heartbeat write


def _orchestrate_tick(
    backlog_file: str,
    dry_run: bool,
    seen_result_files: set,
    log_prefix: str = "[tick]",
) -> None:
    store = BacklogStore(backlog_file)
    data = store.read()
    backlog_dir = Path(backlog_file).parent
    statuses = data.get("config", {}).get("statuses", DEFAULT_STATUSES)

    # 1. Ingest any new result files first (so lane state is fresh for step 2)
    import re as _re
    result_files = _scan_result_files(backlog_dir)
    for rf in result_files:
        if str(rf) in seen_result_files:
            continue
        console.print(f"{log_prefix} new result file → ingest {rf.name}")
        seen_result_files.add(str(rf))

        if dry_run:
            console.print(f"[dim]DRY-RUN would ingest:[/dim] {rf}")
            continue

        # Parse the result file directly so we can inspect verdict/status
        try:
            raw = rf.read_text(encoding="utf-8").strip()
            if "```" in raw:
                m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, _re.DOTALL)
                if m:
                    raw = m.group(1)
            report = json.loads(raw)
        except Exception as e:
            console.print(f"{log_prefix} [yellow]Warning:[/yellow] could not parse {rf.name}: {e} — falling back to subprocess ingest")
            _run_ingest(backlog_file, str(rf), dry_run)
            data = store.read()
            continue

        # Ingest via store directly to get the outcome dict
        try:
            outcome = store.ingest_result(report)
        except Exception as e:
            console.print(f"{log_prefix} [red]ingest error[/red] for {rf.name}: {e}")
            data = store.read()
            continue

        data = store.read()
        item_id = outcome.get("item_id", "")
        status_applied = outcome.get("status_applied", "")
        next_lane = outcome.get("next_lane", "")
        outcome_branch = outcome.get("branch_name")

        console.print(
            f"{log_prefix} ingested {rf.name} — item {item_id} "
            f"status={status_applied} next_lane={next_lane}"
        )

        # ── Post-ingest: passing review → git merge ───────────────────────────
        verdict = report.get("verdict")
        if verdict == "pass" and outcome_branch:
            # Find item title for merge message
            items_snap = data.get("items", [])
            item_snap = next((i for i in items_snap if i.get("id") == item_id), {})
            item_title = item_snap.get("title", item_id)
            merge_msg = f"merge: item-{item_id} {item_title}"
            console.print(
                f"{log_prefix} review passed — merging branch {outcome_branch!r} into current branch"
            )
            repo_root = backlog_dir
            # Walk up to find .git root
            search = backlog_dir
            for _ in range(10):
                if (search / ".git").exists():
                    repo_root = search
                    break
                parent = search.parent
                if parent == search:
                    break
                search = parent

            merge_result = subprocess.run(
                ["git", "merge", "--no-ff", outcome_branch, "-m", merge_msg],
                capture_output=True, text=True, cwd=str(repo_root),
            )
            if merge_result.returncode == 0:
                console.print(
                    f"{log_prefix} [green]Merge succeeded[/green] — "
                    f"branch {outcome_branch!r} merged into current branch"
                )
                console.print(f"[dim]{merge_result.stdout.strip()}[/dim]")
                # Delete the feature branch now that it's merged
                delete_result = subprocess.run(
                    ["git", "branch", "-d", outcome_branch],
                    capture_output=True, text=True, cwd=str(repo_root),
                )
                if delete_result.returncode == 0:
                    console.print(f"{log_prefix} deleted branch {outcome_branch!r}")
                else:
                    console.print(
                        f"{log_prefix} [yellow]could not delete branch {outcome_branch!r}:[/yellow] "
                        f"{delete_result.stderr.strip()}"
                    )
                # Ensure item is in done state (ingest may have moved to code-review first)
                pos = _item_position(data, item_id)
                if pos and item_snap.get("status") not in ("done", "discarded"):
                    try:
                        store.move_item(pos, "done", moved_by="orchestrator")
                        data = store.read()
                        console.print(f"{log_prefix} marked item {item_id} done after merge")
                    except Exception as me:
                        console.print(f"{log_prefix} [yellow]could not mark done:[/yellow] {me}")
            else:
                conflict_output = (merge_result.stdout + "\n" + merge_result.stderr).strip()
                console.print(
                    f"{log_prefix} [red]Merge conflict[/red] on branch {outcome_branch!r} — "
                    "escalating to user"
                )
                # Open a thread on the item waiting for user
                data2 = store.read()
                items2 = data2.get("items", [])
                item2 = next((i for i in items2 if i.get("id") == item_id), None)
                if item2 is not None:
                    from .core import _now_iso, _generate_id
                    item2.setdefault("threads", []).append({
                        "id": _generate_id(),
                        "topic": f"Merge conflict: {outcome_branch}",
                        "waiting_on": "user",
                        "body": (
                            f"Merge of branch `{outcome_branch}` into current branch failed.\n\n"
                            f"Git output:\n```\n{conflict_output}\n```\n\n"
                            "Resolve the conflict manually and merge."
                        ),
                        "created_at": _now_iso(),
                        "resolved": False,
                    })
                    item2["updated_at"] = _now_iso()
                    store.write(data2, expected_version=data2.get("version", 0))
                    data = store.read()
                pos = _item_position(data, item_id)
                console.print(
                    f"[yellow]NOTIFICATION:[/yellow] Item #{pos} '{item_title}' — "
                    f"merge conflict on branch {outcome_branch!r}. Manual resolution required."
                )

        # ── Post-ingest: reject → surgical or architectural escalation ────────
        elif verdict == "reject":
            issues = report.get("issues", [])
            blocker_text = report.get("blocker", "")
            if not blocker_text and issues:
                blocker_text = "; ".join(
                    i.get("description", "") for i in issues if i.get("severity") == "blocker"
                ) or "; ".join(i.get("description", "") for i in issues)

            # Check for file:line reference — surgical reject
            is_surgical = bool(_re.search(r"\w+\.\w+:\d+", blocker_text))

            if is_surgical:
                # Case 1: surgical — add blocker thread and re-invoke work agent
                console.print(
                    f"{log_prefix} reject is surgical (file:line found) — "
                    f"re-invoking work agent for item {item_id}"
                )
                data2 = store.read()
                items2 = data2.get("items", [])
                item2 = next((i for i in items2 if i.get("id") == item_id), None)
                if item2 is not None:
                    from .core import _now_iso, _generate_id
                    item2.setdefault("threads", []).append({
                        "id": _generate_id(),
                        "topic": "Review reject — surgical fix required",
                        "waiting_on": "agent",
                        "body": f"Reviewer blocked this item:\n\n{blocker_text}",
                        "created_at": _now_iso(),
                        "resolved": False,
                    })
                    item2["updated_at"] = _now_iso()
                    store.write(data2, expected_version=data2.get("version", 0))
                    data = store.read()
                pos = _item_position(data, item_id)
                if pos:
                    _run_handoff(backlog_file, data.get("items", [{}])[pos - 1].get("assigned_to") or "backend-dev", pos, dry_run)
            else:
                # Case 2: architectural — open thread waiting on user, print NOTIFICATION
                console.print(
                    f"{log_prefix} reject is architectural (no file:line) — "
                    f"escalating item {item_id} to user"
                )
                data2 = store.read()
                items2 = data2.get("items", [])
                item2 = next((i for i in items2 if i.get("id") == item_id), None)
                if item2 is not None:
                    from .core import _now_iso, _generate_id
                    item2.setdefault("threads", []).append({
                        "id": _generate_id(),
                        "topic": "Review reject — architectural issue",
                        "waiting_on": "user",
                        "body": (
                            f"Reviewer found an architectural issue that requires human decision:\n\n"
                            f"{blocker_text}"
                        ),
                        "created_at": _now_iso(),
                        "resolved": False,
                    })
                    item2["updated_at"] = _now_iso()
                    store.write(data2, expected_version=data2.get("version", 0))
                    data = store.read()
                pos = _item_position(data, item_id)
                item_snap = next((i for i in data.get("items", []) if i.get("id") == item_id), {})
                console.print(
                    f"[yellow]NOTIFICATION:[/yellow] Item #{pos} '{item_snap.get('title', item_id)}' — "
                    f"review rejected with architectural blocker. Human input required:\n  {blocker_text}"
                )

    # 2. Check items with waiting_on=lead threads
    items = data.get("items", [])
    for item in items:
        for thread in item.get("threads", []):
            if thread.get("resolved"):
                continue
            if thread.get("waiting_on") == "lead":
                item_id = item.get("id")
                pos = _item_position(data, item_id)
                console.print(
                    f"{log_prefix} item {item_id} has waiting_on=lead thread — "
                    "cannot auto-resolve, escalating to user"
                )
                if not dry_run:
                    # Mark as waiting_on=user so it surfaces correctly
                    thread["waiting_on"] = "user"
                    store.write(data)
                console.print(
                    f"[yellow]NOTIFICATION:[/yellow] Item #{pos} '{item.get('title','?')}' "
                    "needs user input (thread unresolvable by orchestrator)"
                )

    # 3. Re-read for fresh state
    data = store.read()
    statuses_list = data.get("config", {}).get("statuses", DEFAULT_STATUSES)

    # 4. Handle items that need lane-based action (review / work)
    items = data.get("items", [])
    acted = False
    for item in items:
        status = item.get("status", "")
        assigned_to = item.get("assigned_to") or ""
        item_id = item.get("id")
        pos = _item_position(data, item_id)

        # Determine previous lane from lane_history
        history = item.get("lane_history", [])
        previous_lane = ""
        if len(history) >= 2:
            prev = history[-2]
            previous_lane = prev if isinstance(prev, str) else prev.get("lane", "")

        # Skip if already terminal or waiting
        terminal_ids = {s.get("id") for s in statuses_list if s.get("id") in ("done", "discarded")}
        if status in terminal_ids or status in ("backlog", "refined"):
            continue

        # Skip if item has an unresolved agent-side thread — something is already in flight
        # (e.g. a surgical reject re-invocation). Without this guard the step-4 dispatch
        # loop would double-dispatch on every tick until the result file lands.
        if any(
            not t.get("resolved") and t.get("waiting_on") == "agent"
            for t in item.get("threads", [])
        ):
            continue

        # Skip if item has pending staged actions (two-stage approval gate)
        if BacklogStore.has_pending_staged_actions(item):
            pending_count = sum(1 for a in item.get("staged_actions", []) if a.get("status") == "pending")
            console.print(
                f"{log_prefix} item {item_id} has {pending_count} pending staged action(s) — "
                "blocked until resolved"
            )
            continue

        # Determine what the orchestrator should do for this lane
        decision = _semantic_next_action(statuses_list, status, previous_lane, assigned_to)
        action = decision.get("action")
        reason = decision.get("reason", "")

        if action == "done":
            console.print(f"{log_prefix} item {item_id} is terminal ({status}) — logging completion")
            continue

        if action == "wait":
            continue

        if action == "review":
            agent = _select_agent(data, item, exclude=assigned_to, for_review=True)
            if not agent:
                console.print(
                    f"{log_prefix} item {item_id} in {status} needs review "
                    "but no suitable reviewer found → notifying user"
                )
                continue
            console.print(
                f"{log_prefix} item {item_id} in {status} → review handoff {agent} --item {pos}"
            )
            _run_handoff(backlog_file, agent, pos, dry_run, review=True)
            acted = True
            continue

        if action == "work":
            # Only act on "ready" or lanes explicitly needing work assignment
            if status == "ready":
                agent = _select_agent(data, item)
                if not agent:
                    console.print(
                        f"{log_prefix} item {item_id} ready but no suitable agent → notifying user"
                    )
                    continue
                console.print(
                    f"{log_prefix} found 1 ready item → handoff {agent} --item {pos}"
                )
                _run_handoff(backlog_file, agent, pos, dry_run)
                acted = True
            elif status == "in-progress":
                # ── Review gate: inject inline review if no review lane exists ──
                require_review = data.get("config", {}).get("orchestrator", {}).get(
                    "require_review", True
                )
                if require_review and not _review_gate_satisfied(item) and not _has_review_lane(statuses_list):
                    sentinel_id = f"inline-review-{item_id}"
                    threads = item.get("threads", [])
                    review_in_flight = any(
                        t.get("id") == sentinel_id and not t.get("resolved")
                        for t in threads
                    )
                    if review_in_flight:
                        continue  # already dispatched, wait for result
                    reviewer = _select_agent(data, item, exclude=assigned_to, for_review=True)
                    if reviewer:
                        console.print(
                            f"{log_prefix} item {item_id} approaching done — "
                            f"no review lane exists, injecting inline review via {reviewer}"
                        )
                        if not dry_run:
                            from .core import _now_iso
                            item.setdefault("threads", []).append({
                                "id": sentinel_id,
                                "topic": "inline-review-dispatched",
                                "waiting_on": "agent",
                                "body": f"Inline review dispatched to {reviewer} at {_now_iso()}.",
                                "resolved": False,
                            })
                            store.write(data)
                        _run_handoff(backlog_file, reviewer, pos, dry_run, review=True)
                        acted = True
                    else:
                        console.print(
                            f"{log_prefix} item {item_id} approaching done — "
                            "no review lane and no suitable reviewer → notifying user"
                        )
                        if not dry_run:
                            no_reviewer_id = f"review-gate-{item_id}"
                            if not any(t.get("id") == no_reviewer_id for t in threads):
                                item.setdefault("threads", []).append({
                                    "id": no_reviewer_id,
                                    "waiting_on": "user",
                                    "body": "Review gate: no suitable reviewer agent available.",
                                    "resolved": False,
                                })
                                store.write(data)
                        console.print(
                            f"[yellow]NOTIFICATION:[/yellow] Item #{pos} "
                            f"'{item.get('title','?')}' needs peer review but no "
                            "reviewer is available — human input required"
                        )

    if not acted and not seen_result_files:
        pass  # silent clean tick


@app.command()
def orchestrate(
    file: Optional[str] = FILE_OPT,
    poll: int = typer.Option(10, "--poll", help="Seconds between ticks"),
    once: bool = typer.Option(False, "--once", help="Run one tick and exit"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print actions without invoking"),
    mode: Optional[str] = typer.Option(
        None, "--mode",
        help="Orchestrator mode: 'supervised' (default) or 'auto'. Overrides config.",
    ),
) -> None:
    """Persistent orchestrator: drive the dev cycle after human approves items to ready.

    Modes:
      supervised (default) — human moves items to ready; orchestrator drives execution from there.
      auto                 — lead agent picks, refines, and starts items autonomously;
                             asks human only when context is insufficient.
    """
    import time

    backlog_file = _resolve_file(file)
    seen_result_files: set = set()

    # ── Startup checks ────────────────────────────────────────────────────────
    try:
        _startup_data = BacklogStore(backlog_file).read()
    except Exception as e:
        err_console.print(f"[red]Error reading backlog:[/red] {e}")
        raise typer.Exit(1)

    orch_mode = _get_orchestrator_mode(_startup_data, mode)
    if orch_mode not in ("supervised", "auto"):
        err_console.print(
            f"[red]Error:[/red] Unknown orchestrator mode '{orch_mode}'. "
            "Must be 'supervised' or 'auto'."
        )
        raise typer.Exit(1)

    # Validate lead agent when auto mode is requested
    lead_name: Optional[str] = None
    if orch_mode == "auto":
        lead_entry = _get_lead_agent(_startup_data)
        if lead_entry is None:
            err_console.print(
                "[red]Error:[/red] Auto mode requires a lead agent. "
                "Set role='lead' on exactly one agent in config.agents."
            )
            raise typer.Exit(1)
        lead_name = lead_entry[0]
        console.print(f"[bold green]Auto mode[/bold green] — lead agent: {lead_name}")
    else:
        # Still validate if a lead is configured — catch misconfigurations early
        try:
            _get_lead_agent(_startup_data)
        except SystemExit:
            raise

    orch_cfg = _startup_data.get("config", {}).get("orchestrator", {})
    require_review = orch_cfg.get("require_review", True)
    if require_review is False:
        console.print(
            "[yellow]Warning:[/yellow]  require_review is disabled — "
            "items will reach done without peer review."
        )

    console.print(
        f"[bold green]Orchestrator started[/bold green] "
        f"(mode={orch_mode}, file={backlog_file}, poll={poll}s, once={once}, dry_run={dry_run})"
    )

    tick_count = 0
    try:
        while True:
            tick_count += 1
            console.print(f"[dim]--- tick {tick_count} ---[/dim]")
            if orch_mode == "auto" and lead_name:
                _auto_refine_tick(
                    backlog_file, lead_name, dry_run,
                    log_prefix=f"[tick {tick_count}][auto]",
                )
            _orchestrate_tick(backlog_file, dry_run, seen_result_files, log_prefix=f"[tick {tick_count}]")
            # Write heartbeat after each tick
            if not dry_run:
                try:
                    tick_data = BacklogStore(backlog_file).read()
                    in_flight = [
                        i["id"] for i in tick_data.get("items", [])
                        if i.get("status") == "in-progress"
                    ]
                    pending = len(_scan_result_files(Path(backlog_file).parent)) - len(seen_result_files)
                    _write_heartbeat(backlog_file, orch_mode, in_flight, max(pending, 0))
                except Exception:
                    pass  # best-effort
            if once:
                break
            time.sleep(poll)
    except KeyboardInterrupt:
        console.print(
            f"\n[bold]Orchestrator stopped[/bold] after {tick_count} tick(s). "
            f"Ingested {len(seen_result_files)} result file(s)."
        )


def main():
    app()


if __name__ == "__main__":
    main()
