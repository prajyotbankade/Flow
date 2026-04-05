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
from typing import List, Optional

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
) -> None:
    """Assemble a structured work brief and hand it off to claude CLI."""
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
    output_contract = json.dumps({
        "item_id": target.get("id"),
        "status": "done | blocked | partial",
        "summary": "...",
        "bugs_found": [{"title": "...", "description": "..."}],
        "follow_ups": [{"title": "...", "description": "..."}],
        "blocker": "... (only if status=blocked)",
    }, indent=2)

    lines = []
    lines.append("# Work Brief")
    lines.append("")
    if persona_text:
        lines.append("## Agent Persona")
        lines.append(persona_text.strip())
        lines.append("")
    lines.append("## Task")
    lines.append(f"**Title**: {target.get('title', '')}")
    lines.append(f"**Item ID**: {target.get('id', '')}")
    lines.append(f"**Status**: {target.get('status', '')}")
    lines.append(f"**Priority weight**: {target.get('priority_weight', '')}")
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

    lines.append("## Output Contract")
    lines.append(
        "When you finish, write ONLY the following JSON to stdout (no extra text before or after):"
    )
    lines.append("")
    lines.append("```json")
    lines.append(output_contract)
    lines.append("```")
    lines.append("")
    lines.append(
        "Fields: `item_id` (string), `status` (done|blocked|partial), "
        "`summary` (string), `bugs_found` (array), `follow_ups` (array), "
        "`blocker` (string, only if blocked)."
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

    if status_applied == "done":
        console.print(
            f"[green]Ingested[/green] item [cyan]{item_id}[/cyan] — "
            f"advanced to [bold]{next_lane}[/bold]"
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


def _select_agent(data: dict, item: dict, exclude: Optional[str] = None) -> Optional[str]:
    """Pick the best-fit agent from config.agents for an item.

    Scores agents by skill overlap with item tags, penalises agents at their
    max_active limit, excludes the optional *exclude* agent (for reviewer
    selection).  Returns None if no suitable agent found.
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


def _run_handoff(backlog_file: str, agent: str, pos: int, dry_run: bool) -> None:
    cmd = ["backlog", "--file", backlog_file, "handoff", agent, "--item", str(pos)]
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
    result_files = _scan_result_files(backlog_dir)
    for rf in result_files:
        if str(rf) not in seen_result_files:
            console.print(f"{log_prefix} new result file → ingest {rf.name}")
            _run_ingest(backlog_file, str(rf), dry_run)
            seen_result_files.add(str(rf))
            if not dry_run:
                # Re-read after ingest so we see updated lane
                data = store.read()

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
            agent = _select_agent(data, item, exclude=assigned_to)
            if not agent:
                console.print(
                    f"{log_prefix} item {item_id} in {status} needs review "
                    "but no suitable reviewer found → notifying user"
                )
                continue
            console.print(
                f"{log_prefix} item {item_id} in {status} → review handoff {agent} --item {pos}"
            )
            _run_handoff(backlog_file, agent, pos, dry_run)
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
                    reviewer = _select_agent(data, item, exclude=assigned_to)
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
                        _run_handoff(backlog_file, reviewer, pos, dry_run)
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
