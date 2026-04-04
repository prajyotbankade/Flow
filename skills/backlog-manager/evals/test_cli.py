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


def test_no_file_specified_exits_with_error(monkeypatch):
    """No --file and BACKLOG_FILE not set → exit 1 with clear message."""
    monkeypatch.delenv("BACKLOG_FILE", raising=False)
    result = runner.invoke(app, ["list"])
    assert result.exit_code == 1, result.output
    assert "BACKLOG_FILE" in result.output or "backlog file" in result.output.lower()


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
