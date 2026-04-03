"""CLI integration tests.

Each test uses a fresh temp backlog.json — no shared state between cases.
Uses typer.testing.CliRunner to invoke commands without spawning a subprocess.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

# Make the backlog package importable from this test file
sys.path.insert(0, str(Path(__file__).parent.parent))

from backlog.cli import app
from backlog.core import BacklogStore

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
    assert result.exit_code == 0
    assert "Fix login bug" in result.output


# ── Test: valid lane sequence ─────────────────────────────────────────────────

def test_valid_lane_sequence(tmp_backlog):
    invoke(["add", "Feature A"], tmp_backlog)

    # backlog → ready (no gates on ready)
    result = invoke(["move", "1", "ready"], tmp_backlog)
    assert result.exit_code == 0, result.output

    # ready → in-progress (requires: ready — satisfied)
    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 0, result.output

    # in-progress → code-review (requires: in-progress — satisfied)
    result = invoke(["move", "1", "code-review"], tmp_backlog)
    assert result.exit_code == 0, result.output

    # code-review → done (requires: code-review — satisfied)
    result = invoke(["move", "1", "done"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "done"


# ── Test: gate violation ──────────────────────────────────────────────────────

def test_gate_violation_skips_required_lane(tmp_backlog):
    invoke(["add", "Feature B"], tmp_backlog)

    # Try to jump backlog → in-progress (requires: ready — not satisfied)
    result = invoke(["move", "1", "in-progress"], tmp_backlog)
    assert result.exit_code == 1
    assert "gate" in result.output.lower() or "requires" in result.output.lower()

    # Item must still be in backlog
    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "backlog"


# ── Test: backward move resets gate_from ─────────────────────────────────────

def test_backward_move_resets_gate_from(tmp_backlog):
    invoke(["add", "Feature C"], tmp_backlog)

    # Advance to in-progress legitimately
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)

    # Move backward to ready — always allowed
    result = invoke(["move", "1", "ready"], tmp_backlog)
    assert result.exit_code == 0, result.output

    # gate_from should be reset — in-progress is no longer in the "earned" window
    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    gate_from = item.get("gate_from", 0)
    # History from gate_from onward should NOT include in-progress
    relevant = [e["lane"] for e in item["lane_history"][gate_from:]]
    assert "in-progress" not in relevant, (
        "gate_from watermark should exclude prior in-progress from earned gates"
    )

    # Confirm: trying to move to code-review now should fail (in-progress not re-earned)
    result = invoke(["move", "1", "code-review"], tmp_backlog)
    assert result.exit_code == 1


# ── Test: pick ────────────────────────────────────────────────────────────────

def test_pick_highest_priority_ready_item(tmp_backlog):
    invoke(["add", "Low priority item"], tmp_backlog)
    invoke(["add", "High priority item"], tmp_backlog)

    # Move #1 (first = highest priority) to ready
    invoke(["move", "1", "ready"], tmp_backlog)

    result = invoke(["pick", "alice"], tmp_backlog)
    assert result.exit_code == 0, result.output
    assert "Low priority item" in result.output  # #1 is highest priority

    data = json.loads(Path(tmp_backlog).read_text())
    item = data["items"][0]
    assert item["status"] == "in-progress"
    assert item["assigned_to"] == "alice"


def test_pick_no_ready_items(tmp_backlog):
    invoke(["add", "Nothing ready"], tmp_backlog)
    result = invoke(["pick", "alice"], tmp_backlog)
    assert result.exit_code == 1
    assert "ready" in result.output.lower() or "no" in result.output.lower()


# ── Test: restore always goes to backlog ─────────────────────────────────────

def test_restore_goes_to_backlog(tmp_backlog):
    invoke(["add", "Feature D"], tmp_backlog)

    # Advance to in-progress and discard
    invoke(["move", "1", "ready"], tmp_backlog)
    invoke(["move", "1", "in-progress"], tmp_backlog)
    invoke(["discard", "1"], tmp_backlog)

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "discarded"

    result = invoke(["restore", "1"], tmp_backlog)
    assert result.exit_code == 0, result.output

    data = json.loads(Path(tmp_backlog).read_text())
    assert data["items"][0]["status"] == "backlog"


# ── Test: concurrent write conflict ──────────────────────────────────────────

def test_conflict_on_stale_version(tmp_backlog):
    """Simulate a conflict by manually writing a higher version to the file."""
    invoke(["add", "Conflict test item"], tmp_backlog)

    # Read current state
    data = json.loads(Path(tmp_backlog).read_text())

    # Simulate another writer bumping the version
    data["version"] += 10
    Path(tmp_backlog).write_text(json.dumps(data, indent=2))

    # Now try to write via BacklogStore with the old expected version
    store = BacklogStore(tmp_backlog)
    from backlog.exceptions import ConflictError
    with pytest.raises(ConflictError):
        store.write({"version": 0, "items": []}, expected_version=0)


# ── Test: BACKLOG_FILE not set ────────────────────────────────────────────────

def test_no_file_specified_exits_with_error():
    """No --file and BACKLOG_FILE not set → exit 1 with clear message."""
    env_backup = os.environ.pop("BACKLOG_FILE", None)
    try:
        result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "BACKLOG_FILE" in result.output or "backlog file" in result.output.lower()
    finally:
        if env_backup is not None:
            os.environ["BACKLOG_FILE"] = env_backup


# ── Test: --json output ────────────────────────────────────────────────────────

def test_json_output_is_valid(tmp_backlog):
    invoke(["add", "JSON test item"], tmp_backlog)
    result = invoke(["list", "--json"], tmp_backlog)
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output.strip())
    assert isinstance(parsed, list)
    assert parsed[0]["title"] == "JSON test item"


def test_show_json_output(tmp_backlog):
    invoke(["add", "Show JSON item"], tmp_backlog)
    result = runner.invoke(app, ["show", "1", "--json", "--file", tmp_backlog])
    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output.strip())
    assert parsed["title"] == "Show JSON item"
    assert "id" in parsed
    assert "status" in parsed
