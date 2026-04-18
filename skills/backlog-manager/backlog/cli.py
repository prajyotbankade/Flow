"""Backlog CLI — thin Typer adapter over BacklogStore.

File resolution precedence:
  --file flag  >  BACKLOG_FILE env var  >  error

Exit codes:
  0 — success
  1 — item not found, gate violation, or validation error
  2 — version conflict (re-read and retry)
"""

import json
import os
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

from .core import BacklogStore, DEFAULT_STATUSES
from .exceptions import ConflictError, GateViolationError, ItemNotFoundError
from .server import compute_scores, compute_item_readiness

app = typer.Typer(
    name="backlog",
    help="Manage your project backlog from the terminal.",
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)

# ── File resolution ────────────────────────────────────────────────────────────

def _resolve_file(file_flag: Optional[str]) -> str:
    path = file_flag or os.environ.get("BACKLOG_FILE")
    if not path:
        err_console.print(
            "[red]Error:[/red] No backlog file specified. "
            "Use [bold]--file[/bold] or set [bold]BACKLOG_FILE[/bold]."
        )
        raise typer.Exit(1)
    return path


def _store(file_flag: Optional[str]) -> BacklogStore:
    return BacklogStore(_resolve_file(file_flag))


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
            unresolved = sum(
                1 for t in item.get("threads", []) if not t.get("resolved")
            )
            thread_flag = f" [yellow]⚠ {unresolved} unresolved[/yellow]" if unresolved else ""
            done_marker = "✓ " if sid == "done" else "  "
            arrow = "→ " if sid == "in-progress" else "  "
            console.print(
                f"  {arrow}[cyan]#{pos}[/cyan] {item.get('title', '')}"
                f"{assigned}{thread_flag}"
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
    store = _store(file)
    data = store.read()
    _print_board(data, filter_status=status, filter_assigned=assigned_to, as_json=json_out)


@app.command()
@_handle
def top(
    n: int = typer.Argument(5, help="Number of items to show (default 5)"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Show the top N prioritized items, ranked by score. No server needed."""
    store = _store(file)
    data = store.read()
    all_items = data.get("items", [])
    pos_map = {item.get("id"): idx + 1 for idx, item in enumerate(all_items)}

    skip = {"done", "discarded"}
    items_by_id = {item.get("id"): item for item in all_items}

    scored = [r for r in compute_scores(data) if r.get("status") not in skip]
    # compute_scores already sorts desc; just slice
    top_items = scored[:n]

    if json_out:
        # Enrich with raw item fields before outputting
        out = []
        for r in top_items:
            raw = items_by_id.get(r["id"], {})
            out.append({**r, "priority_weight": raw.get("priority_weight"), "tags": raw.get("tags", []), "assigned_to": raw.get("assigned_to")})
        console.print_json(json.dumps(out))
        return

    if not top_items:
        console.print("[dim]No active items.[/dim]")
        return

    console.print(f"\n[bold]Top {n} by score[/bold]\n")
    for rank, r in enumerate(top_items, 1):
        iid = r.get("id")
        raw = items_by_id.get(iid, {})
        pos = pos_map.get(iid, "?")
        score = r.get("score", 0)
        status = r.get("status", "")
        title = r.get("title", "")
        pw = raw.get("priority_weight") or "—"
        tags = ", ".join(raw.get("tags") or [])
        assigned = raw.get("assigned_to") or "unassigned"
        readiness_pct = int(r.get("readiness", {}).get("score", 0) * 100)

        status_color = {
            "in-progress": "yellow",
            "ready": "green",
            "refined": "blue",
            "backlog": "dim",
            "code-review": "magenta",
        }.get(status, "dim")

        console.print(
            f"  [bold]{rank}.[/bold] [cyan]#{pos}[/cyan] {title}  "
            f"[bold]score={score}[/bold]  pw={pw}  [{status_color}]{status}[/{status_color}]"
        )
        console.print(
            f"       readiness={readiness_pct}%  assigned={assigned}"
            + (f"  tags={tags}" if tags else "")
        )
    console.print()


@app.command()
@_handle
def show(
    position: int = typer.Argument(..., help="Item number (e.g. 3 for #3)"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Show full detail for one item."""
    store = _store(file)
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
    store = _store(file)
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
    store = _store(file)
    item = store.move_item(position, target_status)
    console.print(f"[green]Moved[/green] #{position} \"{item['title']}\" → {target_status}")


@app.command()
@_handle
def done(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Move an item to done."""
    store = _store(file)
    item = store.move_item(position, "done")
    console.print(f"[green]Done[/green] #{position} \"{item['title']}\"")


@app.command()
@_handle
def assign(
    position: int = typer.Argument(..., help="Item number"),
    to: str = typer.Option(..., "--to", help="Agent or person name"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Assign an item to an agent or person."""
    store = _store(file)
    item = store.assign_item(position, to)
    console.print(f"[green]Assigned[/green] #{position} \"{item['title']}\" → {to}")


@app.command()
@_handle
def unassign(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Remove assignment from an item."""
    store = _store(file)
    item = store.unassign_item(position)
    console.print(f"[green]Unassigned[/green] #{position} \"{item['title']}\"")


@app.command()
@_handle
def discard(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Discard an item (always allowed from any lane)."""
    store = _store(file)
    item = store.discard_item(position)
    console.print(f"[dim]Discarded[/dim] #{position} \"{item['title']}\"")


@app.command()
@_handle
def restore(
    position: int = typer.Argument(..., help="Item number"),
    file: Optional[str] = FILE_OPT,
) -> None:
    """Restore a discarded item back to backlog."""
    store = _store(file)
    item = store.restore_item(position)
    console.print(f"[green]Restored[/green] #{position} \"{item['title']}\" → backlog")


@app.command()
@_handle
def pick(
    agent: str = typer.Argument(..., help="Your agent/user name"),
    file: Optional[str] = FILE_OPT,
    json_out: bool = JSON_OPT,
) -> None:
    """Pick the highest-priority ready item, move to in-progress, and assign it."""
    store = _store(file)
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
) -> None:
    """Edit fields on an item (use 'move' to change status)."""
    store = _store(file)
    fields = {}
    if title is not None:          fields["title"] = title
    if description is not None:    fields["description"] = description
    if priority is not None:       fields["priority"] = priority
    if priority_weight is not None: fields["priority_weight"] = priority_weight
    if complexity is not None:     fields["complexity"] = complexity
    if category is not None:       fields["category"] = category
    if tags is not None:           fields["tags"] = [t.strip() for t in tags.split(",")]
    if assigned_to is not None:    fields["assigned_to"] = assigned_to
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
    store.init()
    console.print(f"[green]Created[/green] {store.file_path}")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print('  backlog add "Your first task"          [dim]# add an item[/dim]')
    console.print("  backlog board                           [dim]# open the visual board[/dim]")
    console.print("  backlog list                            [dim]# view your backlog[/dim]")
    console.print("  backlog doctor --fix                    [dim]# configure CLAUDE.md for agents[/dim]")


# ── CLAUDE.md snippet ─────────────────────────────────────────────────────────

_CLAUDE_MD_MARKER = "<!-- flow-backlog-setup -->"

_CLAUDE_MD_SNIPPET = """\
<!-- flow-backlog-setup -->
## Flow Backlog

This project uses the Flow backlog manager skill.

- **What to work on next:** `BACKLOG_FILE=./backlog.json backlog top`
- **Never reason about priorities yourself** — always check the backlog first
- **All backlog commands:** prefix with `BACKLOG_FILE=./backlog.json` or set the env var once:
  `export BACKLOG_FILE=./backlog.json`
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


def _snippet_present(claude_md: Path) -> bool:
    return _CLAUDE_MD_MARKER in claude_md.read_text(encoding="utf-8")


def _write_snippet(claude_md: Path) -> None:
    existing = claude_md.read_text(encoding="utf-8") if claude_md.exists() else ""
    separator = "\n" if existing and not existing.endswith("\n") else ""
    claude_md.write_text(existing + separator + "\n" + _CLAUDE_MD_SNIPPET, encoding="utf-8")


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

    # ── 2. CLAUDE.md snippet ─────────────────────────────────────────────────
    claude_md = _find_claude_md(cwd)
    snippet_ok = claude_md is not None and _snippet_present(claude_md)

    if snippet_ok:
        ok.append("CLAUDE.md has Flow setup — agents will use the backlog automatically")
    else:
        if fix:
            target = claude_md or (cwd / "CLAUDE.md")
            _write_snippet(target)
            ok.append(f"CLAUDE.md updated — Flow setup written to {target.name}")
        else:
            issues.append(
                "CLAUDE.md missing Flow setup — agents won't know to use the backlog. "
                "Run `backlog doctor --fix` to add it."
            )

    # ── 3. BACKLOG_FILE env var ───────────────────────────────────────────────
    if os.environ.get("BACKLOG_FILE"):
        ok.append(f"BACKLOG_FILE env var set → {os.environ['BACKLOG_FILE']}")
    else:
        issues.append("BACKLOG_FILE env var not set — commands need --file or the env var each time")

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
    try:
        subprocess.run(
            ["backlog-server", "--file", resolved, "--port", str(port)],
            env=env,
        )
    except FileNotFoundError:
        # Fallback: try running the server script directly
        script = Path(__file__).parent.parent / "scripts" / "backlog_server.py"
        subprocess.run(
            [sys.executable, str(script), "--file", resolved, "--port", str(port)],
            env=env,
        )


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

    store = _store(file)
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

    store = _store(file)
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
    store = _store(file)
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
    store = _store(file)
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
    store = _store(file)
    action = store.reject_action(position, action_id, rejected_by="cli", reason=reason)
    msg = f"[red]Rejected[/red] action [cyan]{action_id}[/cyan] ({action.get('type', '')})"
    if reason:
        msg += f" — reason: {reason}"
    console.print(msg)


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


def _auto_refine_tick(
    backlog_file: str,
    lead_name: str,
    dry_run: bool,
    log_prefix: str = "[auto]",
) -> None:
    """Auto mode: lead agent picks the highest-priority unstarted item and either
    moves it to ready (if actionable) or opens a thread asking the human blocking
    questions (max 2, most important first).

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
