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
    assert "Status" in result.output


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


def test_cli_conflict_returns_exit_code_2(tmp_backlog):
    """CLI returns exit code 2 when a concurrent writer bumps the version mid-operation."""
    invoke(["add", "Race condition item"], tmp_backlog)
    invoke(["move", "1", "ready"], tmp_backlog)

    # Read current version so we know the baseline
    data = json.loads(Path(tmp_backlog).read_text())
    current_version = data["version"]

    # Simulate a concurrent writer bumping the version after the CLI reads but before it writes.
    # We do this by writing a higher version directly — the next CLI write will see a mismatch.
    data["version"] = current_version + 10
    Path(tmp_backlog).write_text(json.dumps(data, indent=2))

    # The CLI reads version N, but the file is now at N+10 — ConflictError → exit 2
    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 2, result.output


# ── Test: BACKLOG_FILE not set ────────────────────────────────────────────────

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
