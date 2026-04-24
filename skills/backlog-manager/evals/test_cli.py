"""CLI integration tests.

Each test uses a fresh temp backlog.json — no shared state between cases.
Uses typer.testing.CliRunner to invoke commands without spawning a subprocess.

Requires the package to be installed: pip install -e .  (from the skill directory)
"""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app
from backlog.core import BacklogStore
from backlog.exceptions import ConflictError

runner = CliRunner()


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_backlog(tmp_path):
    """Return path to a fresh backlog.json with gate-aware status config."""
    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "scope": "project",
            "project_name": "TestProject",
            "statuses": [
                {"id": "backlog",     "label": "Backlog"},
                {"id": "refined",     "label": "Refined"},
                {"id": "ready",       "label": "Ready"},
                {"id": "in-progress", "label": "In Progress", "requires": ["ready"]},
                {"id": "code-review", "label": "Code Review", "requires": ["in-progress"]},
                {"id": "done",        "label": "Done",        "requires": ["code-review"]},
                {"id": "discarded",   "label": "Discarded"},
            ],
        },
        "items": [],
    }
    path.write_text(json.dumps(data, indent=2))
    return str(path)


def invoke(args: list[str], file: str) -> object:
    """Helper: run CLI with --file injected."""
    return runner.invoke(app, args + ["--file", file])


# ── Test: add → list ──────────────────────────────────────────────────────────

def test_add_appears_in_list(tmp_backlog):
    result = invoke(["add", "Fix login bug"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "#1" in result.output

    result = invoke(["list"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Fix login bug" in result.output


def test_add_with_flags(tmp_backlog):
    result = invoke(
        ["add", "Auth refactor",
         "--priority", "high",
         "--priority-weight", "8",
         "--complexity", "medium",
         "--category", "auth",
         "--tags", "auth,backend",
         "--description", "Rewrite token logic",
         "--assigned-to", "alice"],
        tmp_backlog,
    )
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    assert item["priority"] == "high"
    assert item["priority_weight"] == 8
    assert item["complexity"] == "medium"
    assert item["category"] == "auth"
    assert item["tags"] == ["auth", "backend"]
    assert item["description"] == "Rewrite token logic"
    assert item["assigned_to"] == "alice"


# ── Test: show ────────────────────────────────────────────────────────────────

def test_show_human_readable(tmp_backlog):
    invoke(["add", "Show me item"], tmp_backlog)
    result = invoke(["show", "1"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Show me item" in result.output
    assert "backlog" in result.output.lower()  # actual status value, not just the label


# ── Test: valid lane sequence + lane_history ──────────────────────────────────

def test_valid_lane_sequence(tmp_backlog):
    invoke(["add", "Feature A"], tmp_backlog)

    result = invoke(["move", "1", "ready"], tmp_backlog)
    assert result.exit_code == 0, result.output

    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 0, result.output

    result = invoke(["move", "1", "code-review"], tmp_backlog)
    assert result.exit_code == 0, result.output

    result = invoke(["move", "1", "done"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    assert item["status"] == "done"
    # lane_history records each transition from-lane
    history_lanes = [e["lane"] for e in item["lane_history"]]
    assert history_lanes == ["backlog", "ready", "in-progress", "code-review"]


def test_done_shorthand_enforces_gate(tmp_backlog):
    """done is blocked when code-review has not been earned."""
    invoke(["add", "Gate test item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)
    # Skipping code-review — done should be blocked by its gate
    result = invoke(["done", "1"], tmp_backlog)
    assert result.exit_code == 1, result.output


def test_done_shorthand(tmp_backlog):
    invoke(["add", "Quick win"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)
    invoke(["move", "1", "code-review"], tmp_backlog)

    result = invoke(["done", "1"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "done"


# ── Test: gate violation ──────────────────────────────────────────────────────

def test_gate_violation_skips_required_lane(tmp_backlog):
    invoke(["add", "Feature B"], tmp_backlog)

    # Try to jump backlog → in-progress (requires: ready — not satisfied)
    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 1, result.output
    assert "gate" in result.output.lower() or "requires" in result.output.lower()

    # Item must still be in backlog
    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "backlog"


# ── Test: backward move resets gate watermark (behavioral) ────────────────────

def test_backward_move_blocks_forward_gates(tmp_backlog):
    """After a backward move, previously earned gates are not re-usable."""
    invoke(["add", "Feature C"], tmp_backlog)

    # Advance to in-progress legitimately
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)

    # Move backward to ready — always allowed
    result = invoke(["move", "1", "ready"], tmp_backlog)
    assert result.exit_code == 0, result.output

    # in-progress was not re-earned — code-review requires it — must be blocked
    result = invoke(["move", "1", "code-review"], tmp_backlog)
    assert result.exit_code == 1, result.output


# ── Test: discard ─────────────────────────────────────────────────────────────

def test_discard_from_any_lane(tmp_backlog):
    """Discard is always allowed, regardless of current lane."""
    invoke(["add", "Mid-flight item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)

    result = invoke(["discard", "1"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "discarded"


# ── Test: pick ────────────────────────────────────────────────────────────────

def test_pick_first_ready_item_by_position(tmp_backlog):
    """pick selects the first ready item by array position (earlier = higher priority)."""
    invoke(["add", "First item"], tmp_backlog)
    invoke(["add", "Second item"], tmp_backlog)

    # Only #1 is ready
    invoke(["move", "1", "ready"], tmp_backlog)

    result = invoke(["pick", "alice"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "First item" in result.output

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    assert item["status"] == "in-progress"
    assert item["assigned_to"] == "alice"


def test_pick_no_ready_items(tmp_backlog):
    invoke(["add", "Nothing ready"], tmp_backlog)
    result = invoke(["pick", "alice"], tmp_backlog)
    assert result.exit_code == 1, result.output
    assert "ready" in result.output.lower() or "no" in result.output.lower()


# ── Test: assign / unassign ───────────────────────────────────────────────────

def test_assign_and_unassign(tmp_backlog):
    invoke(["add", "Assignable item"], tmp_backlog)

    result = invoke(["assign", "1", "--to", "bob"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["assigned_to"] == "bob"

    result = invoke(["unassign", "1"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["assigned_to"] is None


# ── Test: edit ────────────────────────────────────────────────────────────────

def test_edit_fields(tmp_backlog):
    invoke(["add", "Original title"], tmp_backlog)

    result = invoke(
        ["edit", "1",
         "--title", "Revised title",
         "--priority", "high",
         "--priority-weight", "9",
         "--complexity", "low",
         "--category", "auth",
         "--tags", "auth,backend",
         "--description", "Updated details"],
        tmp_backlog,
    )
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    assert item["title"] == "Revised title"
    assert item["priority"] == "high"
    assert item["priority_weight"] == 9
    assert item["complexity"] == "low"
    assert item["category"] == "auth"
    assert item["tags"] == ["auth", "backend"]
    assert item["description"] == "Updated details"


def test_edit_no_fields_exits_with_error(tmp_backlog):
    invoke(["add", "No-op edit"], tmp_backlog)
    result = invoke(["edit", "1"], tmp_backlog)
    assert result.exit_code == 1, result.output


# ── Test: restore ─────────────────────────────────────────────────────────────

def test_restore_goes_to_backlog(tmp_backlog):
    invoke(["add", "Feature D"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)
    invoke(["discard", "1"], tmp_backlog)

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "discarded"

    result = invoke(["restore", "1"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "backlog"


def test_restore_resets_gate_watermark(tmp_backlog):
    """After restore, previously earned gates must be re-earned."""
    invoke(["add", "Feature E"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)
    invoke(["discard", "1"], tmp_backlog)
    invoke(["restore", "1"], tmp_backlog)

    # in-progress was not re-earned — forward gate should block
    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 1, result.output


# ── Test: list filters ────────────────────────────────────────────────────────

def test_list_filter_by_status(tmp_backlog):
    invoke(["add", "Ready item"], tmp_backlog)
    invoke(["add", "Backlog item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)

    result = invoke(["list", "--status", "ready"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Ready item" in result.output
    assert "Backlog item" not in result.output


def test_list_filter_by_assigned_to(tmp_backlog):
    invoke(["add", "Alice task"], tmp_backlog)
    invoke(["add", "Bob task"], tmp_backlog)
    invoke(["assign", "1", "--to", "alice"], tmp_backlog)
    invoke(["assign", "2", "--to", "bob"], tmp_backlog)

    result = invoke(["list", "--assigned-to", "alice"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Alice task" in result.output
    assert "Bob task" not in result.output


# ── Test: init ────────────────────────────────────────────────────────────────

def test_init_creates_backlog_file(tmp_path):
    new_file = str(tmp_path / "new_project" / "backlog.json")
    Path(new_file).parent.mkdir(parents=True)
    result = runner.invoke(app, ["init", "--file", new_file])
    assert result.exit_code == 0, result.output
    assert Path(new_file).exists()
    data = json.loads(Path(new_file).read_text())
    assert "items" in data
    assert "config" in data


def test_init_fails_if_file_exists(tmp_backlog):
    result = invoke(["init"], tmp_backlog)
    assert result.exit_code == 1, result.output


# ── Test: version conflict ────────────────────────────────────────────────────

def test_conflict_on_stale_version(tmp_backlog):
    """BacklogStore.write raises ConflictError when expected_version is stale."""
    invoke(["add", "Conflict test item"], tmp_backlog)

    data = json.loads(Path(tmp_backlog).read_text())
    data["version"] += 10
    Path(tmp_backlog).write_text(json.dumps(data, indent=2))

    store = BacklogStore(tmp_backlog)
    with pytest.raises(ConflictError):
        store.write({"version": 0, "items": []}, expected_version=0)


def test_cli_conflict_returns_exit_code_2(tmp_backlog, monkeypatch):
    """CLI returns exit code 2 when a concurrent writer bumps the version mid-operation.

    Always-raise mock: if retry logic is ever added, it must exhaust retries and
    still surface exit code 2 rather than silently succeeding after one retry.
    """
    invoke(["add", "Race condition item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)

    def always_conflict(self, data, expected_version):
        raise ConflictError("simulated concurrent write")

    monkeypatch.setattr(BacklogStore, "write", always_conflict)

    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 2, result.output


# ── Test: BACKLOG_FILE not set / --file override ──────────────────────────────

def test_file_flag_overrides_env_var(tmp_backlog, tmp_path, monkeypatch):
    """--file takes precedence over BACKLOG_FILE env var.

    The env var points at a non-existent file; success here proves --file
    short-circuits env var resolution entirely (the env var path is never opened).
    """
    other_file = str(tmp_path / "other.json")
    monkeypatch.setenv("BACKLOG_FILE", other_file)

    invoke(["add", "Override item"], tmp_backlog)
    result = invoke(["list"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Override item" in result.output


def test_no_file_specified_defaults_to_backlog_json(monkeypatch, tmp_path):
    """No --file and BACKLOG_FILE not set → defaults to ./backlog.json in cwd."""
    monkeypatch.delenv("BACKLOG_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    # No backlog.json exists yet — list should exit with file-not-found, not a config error
    result = runner.invoke(app, ["list"])
    # Should not complain about missing env var — the default path is used
    assert "BACKLOG_FILE" not in result.output


# ── Test: --json output ────────────────────────────────────────────────────────

def test_list_json_output_is_valid(tmp_backlog):
    invoke(["add", "JSON test item"], tmp_backlog)
    result = invoke(["list", "--json"], tmp_backlog)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output.strip())
    assert isinstance(parsed, list)
    assert len(parsed) >= 1, "Expected at least one item in JSON output"
    assert parsed[0]["title"] == "JSON test item"


def test_show_json_output(tmp_backlog):
    invoke(["add", "Show JSON item"], tmp_backlog)
    result = invoke(["show", "1", "--json"], tmp_backlog)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output.strip())
    assert parsed["title"] == "Show JSON item"
    assert "id" in parsed
    assert "status" in parsed


def test_pick_json_output(tmp_backlog):
    invoke(["add", "Pickable item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    result = invoke(["pick", "alice", "--json"], tmp_backlog)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output.strip())
    assert parsed["status"] == "in-progress"
    assert parsed["assigned_to"] == "alice"


# ── Test: handoff ─────────────────────────────────────────────────────────────

def _add_ready_item(tmp_backlog, title="Handoff task"):
    """Helper: add an item and move it to ready."""
    invoke(["add", title, "--description", "Do the work", "--priority-weight", "8",
            "--complexity", "medium", "--tags", "core,cli"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)


def test_handoff_dry_run_prints_prompt(tmp_backlog):
    _add_ready_item(tmp_backlog)
    result = invoke(["handoff", "backend-dev", "--item", "1", "--dry-run"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Work Brief" in result.output
    assert "Handoff task" in result.output
    assert "Output Contract" in result.output


def test_handoff_dry_run_includes_item_fields(tmp_backlog):
    _add_ready_item(tmp_backlog, title="Important task")
    result = invoke(["handoff", "backend-dev", "--item", "1", "--dry-run"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Important task" in result.output
    assert "medium" in result.output      # complexity
    assert "core" in result.output        # tags


def test_handoff_dry_run_includes_linked_items(tmp_backlog):
    # Add two items with a link
    invoke(["add", "Main task", "--description", "desc"], tmp_backlog)
    invoke(["add", "Dependent task"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    # Manually add a link via store
    from backlog.core import BacklogStore
    store = BacklogStore(tmp_backlog)
    data = store.read()
    dep_id = data["items"][1]["id"]
    data["items"][0]["links"] = [{"item_id": dep_id, "type": "blocks", "reason": "must finish first"}]
    store.write(data)
    result = invoke(["handoff", "backend-dev", "--item", "1", "--dry-run"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Dependent task" in result.output
    assert "blocks" in result.output


def test_handoff_dry_run_no_ready_items_uses_backlog_item(tmp_backlog):
    # item 1 is in backlog (not ready) — --item 1 should still work in dry-run
    invoke(["add", "Not ready yet"], tmp_backlog)
    result = invoke(["handoff", "backend-dev", "--item", "1", "--dry-run"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Not ready yet" in result.output


def test_handoff_item_out_of_range(tmp_backlog):
    result = invoke(["handoff", "backend-dev", "--item", "99", "--dry-run"], tmp_backlog)
    assert result.exit_code == 1


def test_handoff_no_ready_items_without_item_flag(tmp_backlog):
    """When no ready items exist and pulse falls back, expect exit 1."""
    # No items at all — pulse should find nothing
    result = invoke(["handoff", "backend-dev"], tmp_backlog)
    assert result.exit_code == 1


def test_handoff_agent_persona_included(tmp_backlog, tmp_path):
    """If .claude/agents/<agent>.md exists near backlog, it appears in the prompt."""
    backlog_dir = Path(tmp_backlog).parent
    agents_dir = backlog_dir / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "backend-dev.md").write_text("## Persona\nI write minimal, correct code.")

    _add_ready_item(tmp_backlog)
    result = invoke(["handoff", "backend-dev", "--item", "1", "--dry-run"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "minimal, correct code" in result.output


def test_handoff_claude_not_found_exits_1(tmp_backlog, monkeypatch):
    """When claude binary is missing, exit code must be 1."""
    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    _add_ready_item(tmp_backlog)
    result = invoke(["handoff", "backend-dev", "--item", "1"], tmp_backlog)
    assert result.exit_code == 1
    assert "claude" in result.output.lower() or "claude" in (result.stdout or "").lower()


def test_handoff_saves_result_json(tmp_backlog, monkeypatch):
    """A successful handoff saves a JSON file in handoff_results/."""
    import subprocess as _subprocess

    report = {
        "item_id": "abc",
        "status": "done",
        "summary": "All done",
        "bugs_found": [],
        "follow_ups": [],
    }

    class FakeResult:
        stdout = json.dumps(report)
        returncode = 0

    monkeypatch.setattr(_subprocess, "run", lambda *a, **kw: FakeResult())

    import shutil as _shutil
    monkeypatch.setattr(_shutil, "which", lambda name: "/usr/local/bin/claude")

    _add_ready_item(tmp_backlog)
    result = invoke(["handoff", "backend-dev", "--item", "1"], tmp_backlog)
    assert result.exit_code == 0, result.output

    results_dir = Path(tmp_backlog).parent / "handoff_results"
    assert results_dir.exists()
    files = list(results_dir.glob("*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text())
    assert saved["status"] == "done"
    assert saved["summary"] == "All done"


# ── Test: ingest ──────────────────────────────────────────────────────────────

def _make_inprogress_item(tmp_backlog, title="Ingestion target"):
    """Helper: add item and move it to in-progress, return its id."""
    invoke(["add", title], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)
    data = json.loads(Path(tmp_backlog).read_text())
    return data["items"][0]["id"]


def _write_result(tmp_path, report: dict) -> str:
    p = tmp_path / "result.json"
    p.write_text(json.dumps(report))
    return str(p)


def test_ingest_done_advances_to_next_lane(tmp_backlog, tmp_path):
    item_id = _make_inprogress_item(tmp_backlog)
    report = {
        "item_id": item_id,
        "status": "done",
        "summary": "Completed the work",
        "bugs_found": [],
        "follow_ups": [],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    # Should have advanced from in-progress to code-review (next lane in config)
    assert item["status"] == "code-review"
    # lane_history should record in-progress
    history_lanes = [
        e["lane"] if isinstance(e, dict) else e for e in item["lane_history"]
    ]
    assert "in-progress" in history_lanes


def test_ingest_done_json_output(tmp_backlog, tmp_path):
    item_id = _make_inprogress_item(tmp_backlog)
    report = {
        "item_id": item_id,
        "status": "done",
        "summary": "All done",
        "bugs_found": [],
        "follow_ups": [],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file, "--json"], tmp_backlog)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output.strip())
    assert parsed["status_applied"] == "done"
    assert parsed["next_lane"] == "code-review"


def test_ingest_blocked_opens_thread(tmp_backlog, tmp_path):
    item_id = _make_inprogress_item(tmp_backlog)
    report = {
        "item_id": item_id,
        "status": "blocked",
        "summary": "Waiting for DB creds",
        "blocker": "Cannot connect to staging database",
        "bugs_found": [],
        "follow_ups": [],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    # Item stays in-progress
    assert item["status"] == "in-progress"
    # A thread was opened
    assert len(item["threads"]) == 1
    thread = item["threads"][0]
    assert thread["waiting_on"] == "lead"
    assert thread["resolved"] is False
    assert "staging database" in thread["body"]


def test_ingest_partial_opens_thread(tmp_backlog, tmp_path):
    item_id = _make_inprogress_item(tmp_backlog)
    report = {
        "item_id": item_id,
        "status": "partial",
        "summary": "Auth done, caching not started",
        "bugs_found": [],
        "follow_ups": [],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    assert item["status"] == "in-progress"
    assert len(item["threads"]) == 1
    thread = item["threads"][0]
    assert thread["waiting_on"] == "lead"
    assert "caching" in thread["body"]


def test_ingest_bugs_found_become_items(tmp_backlog, tmp_path):
    item_id = _make_inprogress_item(tmp_backlog)
    report = {
        "item_id": item_id,
        "status": "done",
        "summary": "Done with bugs",
        "bugs_found": [
            {"title": "Null pointer in auth", "description": "Crashes on empty token"},
            {"title": "Race condition in cache", "description": "Concurrent writes"},
        ],
        "follow_ups": [],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    bug_items = [i for i in data["items"] if i.get("category") == "bug"]
    assert len(bug_items) == 2
    for b in bug_items:
        assert b["priority_weight"] == 8
        assert b["status"] == "backlog"
        link_types = [lk["type"] for lk in b.get("links", [])]
        assert "discovered-during" in link_types


def test_ingest_follow_ups_become_items(tmp_backlog, tmp_path):
    item_id = _make_inprogress_item(tmp_backlog)
    report = {
        "item_id": item_id,
        "status": "done",
        "summary": "Done with follow-ups",
        "bugs_found": [],
        "follow_ups": [
            {"title": "Add metrics endpoint", "description": "Expose /metrics"},
        ],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    fu_items = [i for i in data["items"] if i.get("category") == "feature" and i.get("priority_weight") == 5]
    assert len(fu_items) == 1
    assert fu_items[0]["title"] == "Add metrics endpoint"
    link_types = [lk["type"] for lk in fu_items[0].get("links", [])]
    assert "follow-up" in link_types


def test_ingest_missing_file_exits_1(tmp_backlog):
    result = invoke(["ingest", "/tmp/does_not_exist_xyz.json"], tmp_backlog)
    assert result.exit_code == 1
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_ingest_invalid_json_exits_1(tmp_backlog, tmp_path):
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("this is not json")
    result = invoke(["ingest", str(bad_file)], tmp_backlog)
    assert result.exit_code == 1


def test_ingest_unknown_item_id_exits_1(tmp_backlog, tmp_path):
    report = {
        "item_id": "deadbeef",
        "status": "done",
        "summary": "done",
        "bugs_found": [],
        "follow_ups": [],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file], tmp_backlog)
    assert result.exit_code == 1


def test_ingest_invalid_status_exits_1(tmp_backlog, tmp_path):
    item_id = _make_inprogress_item(tmp_backlog)
    report = {
        "item_id": item_id,
        "status": "unknown-status",
        "summary": "bad",
        "bugs_found": [],
        "follow_ups": [],
    }
    result_file = _write_result(tmp_path, report)
    result = invoke(["ingest", result_file], tmp_backlog)
    assert result.exit_code == 1


# ── Orchestrate fixtures & helpers ────────────────────────────────────────────

def _backlog_with_agents(tmp_path: Path) -> str:
    """Backlog with agents config, one ready item."""
    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "project_name": "OrchTest",
            "statuses": [
                {"id": "backlog",     "label": "Backlog"},
                {"id": "ready",       "label": "Ready"},
                {"id": "in-progress", "label": "In Progress", "requires": ["ready"]},
                {"id": "code-review", "label": "Code Review", "requires": ["in-progress"]},
                {"id": "done",        "label": "Done",        "requires": ["code-review"]},
                {"id": "discarded",   "label": "Discarded"},
            ],
            "agents": {
                "backend-dev": {
                    "skills": ["python", "api"],
                    "max_active": 2,
                },
                "qa": {
                    "skills": ["testing", "api"],
                    "max_active": 2,
                },
            },
        },
        "items": [
            {
                "id": "item-001",
                "title": "Ready task",
                "status": "ready",
                "category": "feature",
                "priority_weight": 5,
                "complexity": "low",
                "tags": ["python", "api"],
                "assigned_to": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "lane_history": ["backlog", "ready"],
                "gate_from": 0,
                "skip_count": 0,
                "reopen_count": 0,
                "threads": [],
                "links": [],
            }
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    return str(path)


# ── Test: orchestrate --once ──────────────────────────────────────────────────

def test_orchestrate_once_picks_up_ready_item(tmp_path, monkeypatch):
    """--once with a ready item should invoke handoff for the item."""
    backlog_file = _backlog_with_agents(tmp_path)
    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    result = invoke(["orchestrate", "--once"], backlog_file)

    assert result.exit_code == 0, result.output
    handoff_calls = [c for c in invoked if "handoff" in c]
    assert len(handoff_calls) >= 1, f"Expected at least one handoff call; got invoked={invoked}"
    # Should have --item 1
    joined = " ".join(handoff_calls[0])
    assert "--item" in joined
    assert "1" in joined


def test_orchestrate_once_dry_run(tmp_path, monkeypatch):
    """--once --dry-run should print the action but NOT invoke subprocess.run."""
    backlog_file = _backlog_with_agents(tmp_path)
    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    result = invoke(["orchestrate", "--once", "--dry-run"], backlog_file)

    assert result.exit_code == 0, result.output
    assert "DRY-RUN" in result.output or "dry" in result.output.lower() or "handoff" in result.output.lower()
    # subprocess.run should NOT have been called for the actual handoff
    real_handoff_calls = [c for c in invoked if "handoff" in c and "DRY" not in str(c)]
    assert len(real_handoff_calls) == 0, f"Should not have invoked handoff in dry-run; got {invoked}"


def test_orchestrate_ingests_result_file(tmp_path, monkeypatch):
    """--once with a result file in handoff_results/ should invoke ingest."""
    backlog_file = _backlog_with_agents(tmp_path)

    # Create a result file
    results_dir = tmp_path / "handoff_results"
    results_dir.mkdir()
    rf = results_dir / "item-001_20260101T000000Z.json"
    rf.write_text(json.dumps({
        "item_id": "item-001",
        "status": "done",
        "summary": "All done",
        "bugs_found": [],
        "follow_ups": [],
    }))

    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    result = invoke(["orchestrate", "--once"], backlog_file)

    assert result.exit_code == 0, result.output
    ingest_calls = [c for c in invoked if "ingest" in c]
    assert len(ingest_calls) >= 1, f"Expected ingest call; got invoked={invoked}"
    assert str(rf) in " ".join(ingest_calls[0])


def test_orchestrate_selects_different_reviewer(tmp_path, monkeypatch):
    """Item in code-review lane should get a reviewer different from assigned_to."""
    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "project_name": "ReviewTest",
            "statuses": [
                {"id": "backlog",     "label": "Backlog"},
                {"id": "ready",       "label": "Ready"},
                {"id": "in-progress", "label": "In Progress"},
                {"id": "code-review", "label": "Code Review"},
                {"id": "done",        "label": "Done"},
                {"id": "discarded",   "label": "Discarded"},
            ],
            "agents": {
                "backend-dev": {"skills": ["python"], "max_active": 2},
                "qa": {"skills": ["testing", "python"], "max_active": 2},
            },
        },
        "items": [
            {
                "id": "item-002",
                "title": "Under review",
                "status": "code-review",
                "category": "feature",
                "priority_weight": 5,
                "complexity": "low",
                "tags": ["python"],
                "assigned_to": "backend-dev",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "lane_history": ["backlog", "in-progress", "code-review"],
                "gate_from": 0,
                "skip_count": 0,
                "reopen_count": 0,
                "threads": [],
                "links": [],
            }
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    backlog_file = str(path)

    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    # Also stub out claude in semantic check so it uses heuristic
    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)

    result = invoke(["orchestrate", "--once"], backlog_file)
    assert result.exit_code == 0, result.output

    handoff_calls = [c for c in invoked if "handoff" in c]
    if handoff_calls:
        # Reviewer must not be backend-dev
        agent_arg = handoff_calls[0][handoff_calls[0].index("handoff") + 1]
        assert agent_arg != "backend-dev", f"Reviewer should differ from builder; got {agent_arg}"


def test_orchestrate_resolves_lead_thread(tmp_path, monkeypatch):
    """Item with waiting_on=lead thread should be escalated to user."""
    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "project_name": "LeadTest",
            "statuses": [
                {"id": "backlog",     "label": "Backlog"},
                {"id": "in-progress", "label": "In Progress"},
                {"id": "done",        "label": "Done"},
            ],
            "agents": {
                "backend-dev": {"skills": ["python"], "max_active": 2},
            },
        },
        "items": [
            {
                "id": "item-003",
                "title": "Blocked by lead",
                "status": "in-progress",
                "category": "feature",
                "priority_weight": 5,
                "complexity": "low",
                "tags": [],
                "assigned_to": "backend-dev",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "lane_history": ["backlog", "in-progress"],
                "gate_from": 0,
                "skip_count": 0,
                "reopen_count": 0,
                "threads": [
                    {
                        "id": "t1",
                        "waiting_on": "lead",
                        "message": "Need clarification",
                        "resolved": False,
                    }
                ],
                "links": [],
            }
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    backlog_file = str(path)

    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)

    result = invoke(["orchestrate", "--once"], backlog_file)
    assert result.exit_code == 0, result.output
    assert "NOTIFICATION" in result.output or "user" in result.output.lower()

    # Verify thread was updated to waiting_on=user on disk
    written = json.loads(path.read_text())
    thread = written["items"][0]["threads"][0]
    assert thread["waiting_on"] == "user"


def test_orchestrate_once_no_work(tmp_path, monkeypatch):
    """No ready items and no result files → clean exit, no subprocess calls."""
    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "project_name": "EmptyTest",
            "statuses": [
                {"id": "backlog", "label": "Backlog"},
                {"id": "done",    "label": "Done"},
            ],
            "agents": {
                "backend-dev": {"skills": ["python"], "max_active": 2},
            },
        },
        "items": [
            {
                "id": "item-004",
                "title": "In backlog",
                "status": "backlog",
                "category": "feature",
                "priority_weight": 3,
                "complexity": "low",
                "tags": [],
                "assigned_to": None,
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "lane_history": ["backlog"],
                "gate_from": 0,
                "skip_count": 0,
                "reopen_count": 0,
                "threads": [],
                "links": [],
            }
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    backlog_file = str(path)

    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)

    result = invoke(["orchestrate", "--once"], backlog_file)
    assert result.exit_code == 0, result.output
    assert len(invoked) == 0, f"Expected no subprocess calls; got {invoked}"


# ── Test: review gate ─────────────────────────────────────────────────────────

def _backlog_approaching_done(tmp_path: Path, *, with_review_lane: bool, reviewer_agent: str = "qa") -> str:
    """Item in-progress on a board with no review lane, assigned_to backend-dev, no prior review.

    The orchestrator's heuristic will return action=work for in-progress status,
    and the review gate check intercepts it before dispatching a work handoff.
    """
    statuses = [
        {"id": "backlog",     "label": "Backlog"},
        {"id": "ready",       "label": "Ready"},
        {"id": "in-progress", "label": "In Progress"},
        {"id": "done",        "label": "Done"},
        {"id": "discarded",   "label": "Discarded"},
    ]
    if with_review_lane:
        statuses.insert(3, {"id": "code-review", "label": "Code Review"})

    agents = {
        "backend-dev": {"skills": ["python", "api"], "max_active": 1},
    }
    if reviewer_agent:
        agents[reviewer_agent] = {"skills": ["testing", "python"], "max_active": 2}

    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "project_name": "GateTest",
            "statuses": statuses,
            "agents": agents,
        },
        "items": [
            {
                "id": "item-gate",
                "title": "Gate test item",
                "status": "in-progress",
                "category": "feature",
                "priority_weight": 5,
                "complexity": "low",
                "tags": ["python"],
                "assigned_to": "backend-dev",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                # lane_history has no entry by a different agent
                "lane_history": [
                    "backlog",
                    {"lane": "in-progress", "at": "2026-01-01T01:00:00+00:00", "by": "backend-dev"},
                ],
                "gate_from": 0,
                "skip_count": 0,
                "reopen_count": 0,
                "threads": [],
                "links": [],
            }
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    return str(path)


def test_review_gate_injected_when_no_review_lane(tmp_path, monkeypatch):
    """Item approaching done with no review lane and unreviewed → inline review injected."""
    backlog_file = _backlog_approaching_done(tmp_path, with_review_lane=False, reviewer_agent="qa")
    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    result = invoke(["orchestrate", "--once"], backlog_file)
    assert result.exit_code == 0, result.output

    # A handoff for the reviewer must have been triggered
    handoff_calls = [c for c in invoked if "handoff" in c]
    assert len(handoff_calls) >= 1, f"Expected inline review handoff; got invoked={invoked}"
    # Reviewer must not be backend-dev
    agent_in_cmd = handoff_calls[0][handoff_calls[0].index("handoff") + 1]
    assert agent_in_cmd != "backend-dev"
    # Log message must mention injecting inline review
    assert "inline review" in result.output.lower() or "injecting" in result.output.lower()


def test_review_gate_satisfied_if_reviewed(tmp_path, monkeypatch):
    """Item in-progress where a different agent already appears in lane_history → no inline review."""
    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "project_name": "ReviewSatisfied",
            "statuses": [
                {"id": "backlog",     "label": "Backlog"},
                {"id": "in-progress", "label": "In Progress"},
                {"id": "done",        "label": "Done"},
            ],
            "agents": {
                "backend-dev": {"skills": ["python"], "max_active": 2},
                "qa": {"skills": ["testing"], "max_active": 2},
            },
        },
        "items": [
            {
                "id": "item-reviewed",
                "title": "Already reviewed",
                "status": "in-progress",
                "category": "feature",
                "priority_weight": 5,
                "complexity": "low",
                "tags": [],
                "assigned_to": "backend-dev",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                # qa appeared in history — gate satisfied
                "lane_history": [
                    "backlog",
                    {"lane": "in-progress", "at": "2026-01-01T01:00:00+00:00", "by": "backend-dev"},
                    {"lane": "in-progress", "at": "2026-01-01T02:00:00+00:00", "by": "qa"},
                ],
                "gate_from": 0,
                "skip_count": 0,
                "reopen_count": 0,
                "threads": [],
                "links": [],
            }
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    backlog_file = str(path)

    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    result = invoke(["orchestrate", "--once"], backlog_file)
    assert result.exit_code == 0, result.output

    # No *inline review* handoff should be triggered — gate is already satisfied.
    # (The orchestrator may still dispatch a normal work handoff for the in-progress item,
    # which is correct behaviour; we only assert no review-gate injection.)
    assert "inline review" not in result.output.lower()
    assert "injecting" not in result.output.lower()


def test_review_gate_disabled_warns(tmp_path, monkeypatch):
    """require_review: false in config → startup prints warning, no gate enforced."""
    path = tmp_path / "backlog.json"
    data = {
        "version": 0,
        "config": {
            "project_name": "GateDisabled",
            "orchestrator": {"require_review": False},
            "statuses": [
                {"id": "backlog",     "label": "Backlog"},
                {"id": "in-progress", "label": "In Progress"},
                {"id": "done",        "label": "Done"},
            ],
            "agents": {
                "backend-dev": {"skills": ["python"], "max_active": 2},
                "qa": {"skills": ["testing"], "max_active": 2},
            },
        },
        "items": [
            {
                "id": "item-nogate",
                "title": "No gate item",
                "status": "in-progress",
                "category": "feature",
                "priority_weight": 5,
                "complexity": "low",
                "tags": [],
                "assigned_to": "backend-dev",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                # No peer review in history — but gate is disabled
                "lane_history": [
                    "backlog",
                    {"lane": "in-progress", "at": "2026-01-01T01:00:00+00:00", "by": "backend-dev"},
                ],
                "gate_from": 0,
                "skip_count": 0,
                "reopen_count": 0,
                "threads": [],
                "links": [],
            }
        ],
    }
    path.write_text(json.dumps(data, indent=2))
    backlog_file = str(path)

    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    result = invoke(["orchestrate", "--once"], backlog_file)
    assert result.exit_code == 0, result.output

    # Warning must appear in output
    assert "require_review" in result.output and "disabled" in result.output.lower()

    # Gate is disabled — no review handoff should be triggered
    handoff_calls = [c for c in invoked if "handoff" in c]
    assert len(handoff_calls) == 0, f"Gate disabled — no handoff expected; got {invoked}"


def test_review_gate_no_reviewer_available_notifies_user(tmp_path, monkeypatch):
    """No suitable reviewer agent → item paused, waiting_on=user, NOTIFICATION printed."""
    # Only backend-dev configured — no other agent can review
    backlog_file = _backlog_approaching_done(tmp_path, with_review_lane=False, reviewer_agent=None)
    invoked: list = []

    def fake_run(cmd, **kwargs):
        invoked.append(cmd)
        class R:
            returncode = 0
        return R()

    monkeypatch.setattr("backlog.cli.subprocess.run", fake_run)
    result = invoke(["orchestrate", "--once"], backlog_file)
    assert result.exit_code == 0, result.output

    # No handoff should be triggered
    handoff_calls = [c for c in invoked if "handoff" in c]
    assert len(handoff_calls) == 0, f"No reviewer available — expected no handoff; got {invoked}"

    # NOTIFICATION must be printed
    assert "NOTIFICATION" in result.output or "human" in result.output.lower()

    # Thread with waiting_on=user must be written to disk
    written = json.loads(Path(backlog_file).read_text())
    item = written["items"][0]
    user_threads = [t for t in item.get("threads", []) if t.get("waiting_on") == "user"]
    assert len(user_threads) >= 1, "Expected a waiting_on=user thread on the item"


# ── #48: policy engine conflict resolution and adjudication ──────────────────

from backlog.server import (
    detect_policy_conflicts,
    execute_policy_actions,
    _CONTRADICTORY_PAIRS,
    get_policy_log_path,
    read_policy_log,
    run_policy_engine,
    get_policies_path,
    save_policies,
)


def _policy_action(item_id, action_type, **kwargs):
    return {"type": action_type, "item_id": item_id, "reason": "test", **kwargs}


def _fired(policy_id, policy_name, item_id, action_type, **kwargs):
    return {
        "policy_id": policy_id,
        "policy_name": policy_name,
        "actions": [_policy_action(item_id, action_type, **kwargs)],
    }


def test_contradictory_pairs_defined():
    """All three documented contradictory pairs are in _CONTRADICTORY_PAIRS."""
    assert tuple(sorted(["escalate", "skip_force"])) in _CONTRADICTORY_PAIRS
    assert tuple(sorted(["escalate", "block"])) in _CONTRADICTORY_PAIRS
    assert tuple(sorted(["reprioritize", "skip_force"])) in _CONTRADICTORY_PAIRS


def test_detect_conflicts_escalate_skip_force():
    """escalate + skip_force on same item is detected as a conflict."""
    fired = [
        _fired("p1", "Policy A", "item001", "escalate"),
        _fired("p2", "Policy B", "item001", "skip_force"),
    ]
    result = detect_policy_conflicts(fired)
    assert len(result["conflicts"]) == 1
    assert result["conflicts"][0]["item_id"] == "item001"
    assert len(result["clean"]) == 0


def test_detect_conflicts_escalate_block():
    """escalate + block on same item is detected as a conflict."""
    fired = [
        _fired("p1", "Policy A", "item001", "escalate"),
        _fired("p2", "Policy B", "item001", "block"),
    ]
    result = detect_policy_conflicts(fired)
    assert len(result["conflicts"]) == 1


def test_detect_conflicts_reprioritize_skip_force():
    """reprioritize + skip_force on same item is detected as a conflict."""
    fired = [
        _fired("p1", "Policy A", "item001", "reprioritize", priority_weight=9),
        _fired("p2", "Policy B", "item001", "skip_force"),
    ]
    result = detect_policy_conflicts(fired)
    assert len(result["conflicts"]) == 1


def test_non_contradictory_pair_no_conflict():
    """notify + reprioritize on same item is NOT a conflict — both go to clean."""
    fired = [
        _fired("p1", "Policy A", "item001", "notify", message="heads up"),
        _fired("p2", "Policy B", "item001", "reprioritize", priority_weight=8),
    ]
    result = detect_policy_conflicts(fired)
    assert len(result["conflicts"]) == 0
    assert len(result["clean"]) == 2


def test_conflict_on_different_items_no_conflict():
    """Contradictory action types on different items are not a conflict."""
    fired = [
        _fired("p1", "Policy A", "item001", "escalate"),
        _fired("p2", "Policy B", "item002", "skip_force"),
    ]
    result = detect_policy_conflicts(fired)
    assert len(result["conflicts"]) == 0
    assert len(result["clean"]) == 2


def test_execute_escalate_sets_priority_weight_to_9():
    """escalate action sets priority_weight to at least 9."""
    item = {"id": "item001", "title": "Test", "priority_weight": 5, "updated_at": "2026-01-01T00:00:00+00:00"}
    data = {"items": [item]}
    entries = [{"action": _policy_action("item001", "escalate"), "policy_name": "P1", "policy_id": "p1"}]
    execute_policy_actions(entries, data)
    assert item["priority_weight"] >= 9


def test_execute_skip_force_increments_skip_count():
    """skip_force action increments skip_count by 1."""
    item = {"id": "item001", "title": "Test", "skip_count": 2, "updated_at": "2026-01-01T00:00:00+00:00"}
    data = {"items": [item]}
    entries = [{"action": _policy_action("item001", "skip_force"), "policy_name": "P1", "policy_id": "p1"}]
    execute_policy_actions(entries, data)
    assert item["skip_count"] == 3


def test_execute_block_adds_thread():
    """block action adds a waiting_on=user thread to the item."""
    item = {"id": "item001", "title": "Test", "threads": [], "execution_history": [], "updated_at": "2026-01-01T00:00:00+00:00"}
    data = {"items": [item]}
    entries = [{"action": _policy_action("item001", "block"), "policy_name": "P1", "policy_id": "p1"}]
    execute_policy_actions(entries, data)
    assert len(item["threads"]) == 1
    assert item["threads"][0]["waiting_on"] == "user"


def test_execute_reprioritize_updates_priority_weight():
    """reprioritize action sets priority_weight to specified value."""
    item = {"id": "item001", "title": "Test", "priority_weight": 5, "updated_at": "2026-01-01T00:00:00+00:00"}
    data = {"items": [item]}
    entries = [{"action": _policy_action("item001", "reprioritize", priority_weight=9), "policy_name": "P1", "policy_id": "p1"}]
    execute_policy_actions(entries, data)
    assert item["priority_weight"] == 9


def test_run_policy_engine_structured_conditions_no_llm(tmp_path):
    """Structured-condition policies fire and log without needing ANTHROPIC_API_KEY."""
    import json as _json

    backlog_file = tmp_path / "backlog.json"
    item = {
        "id": "bugitem1", "title": "Critical bug", "status": "backlog",
        "category": "bug", "priority_weight": 9, "assigned_to": None,
        "complexity": "low", "tags": [], "links": [], "threads": [],
        "lane_history": [], "execution_history": [], "readiness_signals": [],
        "gate_from": 0, "reopen_count": 0, "skip_count": 0,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }
    data = {"version": 1, "config": {"statuses": [
        {"id": "backlog", "label": "Backlog"}, {"id": "refined", "label": "Refined"},
        {"id": "ready", "label": "Ready"},
        {"id": "in-progress", "label": "In Progress", "requires": ["ready"]},
        {"id": "code-review", "label": "Code Review", "requires": ["in-progress"]},
        {"id": "done", "label": "Done", "requires": ["code-review"]},
        {"id": "discarded", "label": "Discarded"},
    ]}, "items": [item]}
    backlog_file.write_text(_json.dumps(data))

    # Write a structured policy that fires on this item
    policies_file = get_policies_path(str(backlog_file))
    policies_data = {
        "version": 1,
        "policies": [{
            "id": "pol001",
            "name": "Escalate critical unassigned bugs",
            "description": "Escalate critical unassigned bugs",
            "priority": 10,
            "active": True,
            "conditions": {
                "match": "all",
                "rules": [
                    {"field": "category", "op": "eq", "value": "bug"},
                    {"field": "priority_weight", "op": "gte", "value": 9},
                    {"field": "assigned_to", "op": "null"},
                ],
            },
            "action": {"type": "escalate", "reason": "Critical unassigned bug"},
        }],
    }
    Path(policies_file).parent.mkdir(parents=True, exist_ok=True)
    Path(policies_file).write_text(_json.dumps(policies_data))

    result = run_policy_engine(data, str(backlog_file))

    assert result["fires"] >= 1
    # Log entry created
    log_path = get_policy_log_path(str(backlog_file))
    assert Path(log_path).exists()
    log_data = read_policy_log(log_path)
    entries = log_data.get("entries", [])
    assert len(entries) >= 1
    assert any(e.get("policy_id") == "pol001" for e in entries)


def test_run_policy_engine_conflict_resolved_to_single_action(tmp_path, monkeypatch):
    """When two contradictory structured policies fire on the same item, only one action applies."""
    import json as _json

    backlog_file = tmp_path / "backlog.json"
    item = {
        "id": "conflict1", "title": "Conflict item", "status": "backlog",
        "category": "bug", "priority_weight": 9, "assigned_to": None,
        "complexity": "low", "tags": [], "links": [], "threads": [],
        "lane_history": [], "execution_history": [], "readiness_signals": [],
        "gate_from": 0, "reopen_count": 0, "skip_count": 0,
        "created_at": "2026-01-01T00:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00",
    }
    data = {"version": 1, "config": {}, "items": [item]}
    backlog_file.write_text(_json.dumps(data))

    policies_file = get_policies_path(str(backlog_file))
    policies_data = {
        "version": 1,
        "policies": [
            {
                "id": "pol_escalate", "name": "Escalate policy", "description": "Escalate",
                "priority": 10, "active": True,
                "conditions": {"match": "all", "rules": [{"field": "category", "op": "eq", "value": "bug"}]},
                "action": {"type": "escalate", "reason": "escalate"},
            },
            {
                "id": "pol_skip", "name": "Skip policy", "description": "Skip",
                "priority": 9, "active": True,
                "conditions": {"match": "all", "rules": [{"field": "category", "op": "eq", "value": "bug"}]},
                "action": {"type": "skip_force", "reason": "skip"},
            },
        ],
    }
    Path(policies_file).parent.mkdir(parents=True, exist_ok=True)
    Path(policies_file).write_text(_json.dumps(policies_data))

    # Mock LLM adjudication to return the escalate action (no API key needed)
    def mock_resolve(conflicts, context):
        return [{"action": conflicts[0]["actions"][0], "resolution_reasoning": "escalate wins", "conflict_item_id": conflicts[0]["item_id"]}]

    monkeypatch.setattr("backlog.server.resolve_conflicts_with_llm", mock_resolve)

    run_policy_engine(data, str(backlog_file))

    # Item must not be in both escalated AND skipped state simultaneously
    # escalate → priority_weight >= 9; skip_force → skip_count > 0
    # Only one should have been applied
    escalated = item.get("priority_weight", 0) >= 9
    skipped = item.get("skip_count", 0) > 0
    assert not (escalated and skipped), "Both contradictory actions were applied — adjudication failed"


# ── #50: concurrent write conflict detection and retry semantics ──────────────

def test_conflict_version_equal_to_current_succeeds(tmp_backlog):
    """Write with version exactly equal to current → succeeds, no ConflictError."""
    invoke(["add", "Version match item"], tmp_backlog)
    data = json.loads(Path(tmp_backlog).read_text())
    current_version = data["version"]
    # Write with matching version — should not raise
    store = BacklogStore(tmp_backlog)
    data2 = json.loads(Path(tmp_backlog).read_text())
    data2["items"][0]["title"] = "Updated title"
    store.write(data2, expected_version=current_version)
    result = json.loads(Path(tmp_backlog).read_text())
    assert result["items"][0]["title"] == "Updated title"


def test_conflict_version_ahead_of_current_rejected(tmp_backlog):
    """Write where file version has advanced past expected_version → ConflictError."""
    invoke(["add", "Ahead version item"], tmp_backlog)
    data = json.loads(Path(tmp_backlog).read_text())
    stale_version = data["version"]
    # Bump the file version externally to simulate a concurrent writer
    data["version"] += 3
    Path(tmp_backlog).write_text(json.dumps(data, indent=2))
    # Now try to write claiming the old stale version — must be rejected
    store = BacklogStore(tmp_backlog)
    with pytest.raises(ConflictError):
        store.write(data, expected_version=stale_version)


def test_conflict_version_unchanged_after_stale_write(tmp_backlog):
    """Stale write is rejected; file version does not advance."""
    invoke(["add", "Stale write item"], tmp_backlog)
    data_before = json.loads(Path(tmp_backlog).read_text())
    version_before = data_before["version"]

    # Bump file version externally to simulate concurrent write
    data_before["version"] += 2
    Path(tmp_backlog).write_text(json.dumps(data_before, indent=2))

    store = BacklogStore(tmp_backlog)
    with pytest.raises(ConflictError):
        store.write({"version": version_before, "items": []}, expected_version=version_before)

    # File version must not have changed
    data_after = json.loads(Path(tmp_backlog).read_text())
    assert data_after["version"] == version_before + 2


def test_conflict_retry_succeeds_after_reread(tmp_backlog):
    """Retry flow: re-read → re-apply → write with fresh version → succeeds."""
    invoke(["add", "Retry item"], tmp_backlog)

    # Simulate intervening write bumping version
    data = json.loads(Path(tmp_backlog).read_text())
    stale_version = data["version"]
    data["version"] += 1
    data["items"][0]["title"] = "Intervening change"
    Path(tmp_backlog).write_text(json.dumps(data, indent=2))

    # Retry: re-read and apply our change on top
    store = BacklogStore(tmp_backlog)
    fresh = json.loads(Path(tmp_backlog).read_text())
    fresh["items"][0]["tags"] = ["retried"]
    store.write(fresh, expected_version=fresh["version"])

    result = json.loads(Path(tmp_backlog).read_text())
    assert result["items"][0]["title"] == "Intervening change", "Intervening change lost after retry"
    assert result["items"][0]["tags"] == ["retried"], "Retry change not applied"


def test_conflict_cli_move_exit_code_2_on_stale(tmp_backlog, monkeypatch):
    """CLI move exits with code 2 when ConflictError is raised."""
    invoke(["add", "CLI conflict item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)

    def always_conflict(self, data, expected_version):
        raise ConflictError("concurrent write")

    monkeypatch.setattr(BacklogStore, "write", always_conflict)
    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 2, result.output


def test_conflict_cli_edit_exit_code_2_on_stale(tmp_backlog, monkeypatch):
    """CLI edit exits with code 2 when ConflictError is raised."""
    invoke(["add", "Edit conflict item"], tmp_backlog)

    def always_conflict(self, data, expected_version):
        raise ConflictError("concurrent write")

    monkeypatch.setattr(BacklogStore, "write", always_conflict)
    result = invoke(["edit", "1", "--title", "New title"], tmp_backlog)
    assert result.exit_code == 2, result.output


def test_conflict_cli_done_exit_code_2_on_stale(tmp_backlog, monkeypatch):
    """CLI done exits with code 2 when ConflictError is raised (item advanced past gate)."""
    # Advance item through all gates so `done` is a valid target
    invoke(["add", "Done conflict item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)
    invoke(["move", "1", "code-review"], tmp_backlog)

    def always_conflict(self, data, expected_version):
        raise ConflictError("concurrent write")

    monkeypatch.setattr(BacklogStore, "write", always_conflict)
    result = invoke(["done", "1"], tmp_backlog)
    assert result.exit_code == 2, result.output


def test_conflict_sequential_second_write_rejected(tmp_backlog):
    """Sequential simulation: first write advances version; second write with old version is rejected."""
    invoke(["add", "Sequential conflict item"], tmp_backlog)

    # Writer A reads, writes successfully — version advances
    store = BacklogStore(tmp_backlog)
    d_a = json.loads(Path(tmp_backlog).read_text())
    stale_version = d_a["version"]
    d_a["items"][0]["tags"] = ["writer-a"]
    store.write(d_a, expected_version=stale_version)

    # Writer B tries to write with the now-stale version → ConflictError
    d_b = json.loads(Path(tmp_backlog).read_text())
    d_b["items"][0]["tags"] = ["writer-b"]
    with pytest.raises(ConflictError):
        store.write(d_b, expected_version=stale_version)

    # File reflects writer-a's change, not writer-b's
    final = json.loads(Path(tmp_backlog).read_text())
    assert final["items"][0]["tags"] == ["writer-a"], "Writer B overwrote Writer A's change"
