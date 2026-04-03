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
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import box

from .core import BacklogStore, DEFAULT_STATUSES
from .exceptions import ConflictError, GateViolationError, ItemNotFoundError

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
    description: str = typer.Option("", "--description", "-d"),
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


def main():
    app()


if __name__ == "__main__":
    main()
