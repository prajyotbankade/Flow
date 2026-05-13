"""Core CLI integration tests: add, move, done, pick, assign, discard, restore, edit."""

import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()

# ── Helpers ───────────────────────────────────────────────────────────────────

_ID_RE = re.compile(r"^[a-z0-9]{8}$")

_EMPTY_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}


def make_backlog(*items_data) -> dict:
    """Create a minimal backlog dict.

    Each element of items_data is either:
    - a str  → title, status defaults to 'backlog'
    - a dict → full item override (must include at least 'title')
    """
    items = []
    for i, spec in enumerate(items_data):
        if isinstance(spec, str):
            spec = {"title": spec}
        item = {
            "id": f"item{i:04d}ab",
            "title": spec.get("title", f"Item {i}"),
            "status": spec.get("status", "backlog"),
            "tags": spec.get("tags", []),
            "description": spec.get("description", ""),
            "assigned_to": spec.get("assigned_to", None),
            "links": [],
            "threads": [],
            "lane_history": [],
            "execution_history": [],
            "gate_from": 0,
            "reopen_count": 0,
            "skip_count": 0,
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        }
        # Strip None-valued optional fields to match real store behaviour
        item = {k: v for k, v in item.items() if v is not None or k in (
            "id", "title", "status", "description", "links", "threads",
            "lane_history", "execution_history", "gate_from", "reopen_count",
            "skip_count", "created_at", "updated_at", "tags",
        )}
        items.append(item)
    return {
        "version": 1,
        "config": {"scope": "project", "project_name": "test"},
        "items": items,
    }


def make_gated_backlog(*items_data) -> dict:
    """Backlog with gate rules: backlog→ready→in-progress→done, skipping is rejected."""
    data = make_backlog(*items_data)
    data["config"]["statuses"] = [
        {"id": "backlog",     "label": "Backlog",     "color": "#aaa"},
        {"id": "ready",       "label": "Ready",       "color": "#aaa", "requires": ["backlog"]},
        {"id": "in-progress", "label": "In Progress", "color": "#aaa", "requires": ["ready"]},
        {"id": "done",        "label": "Done",        "color": "#aaa", "requires": ["in-progress"]},
        {"id": "discarded",   "label": "Discarded",   "color": "#aaa"},
    ]
    return data


@pytest.fixture
def empty_file(tmp_path):
    """Pre-initialized empty backlog.json. Use for 'add' tests that need a clean slate."""
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
    return str(p)


@pytest.fixture
def backlog_file(tmp_path):
    """Isolated backlog.json with two backlog items."""
    data = make_backlog("Item Alpha", "Item Beta")
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(data, indent=2))
    return str(p)


@pytest.fixture
def gated_backlog_file(tmp_path):
    """Isolated backlog.json with gate rules that require ordered transitions."""
    data = make_gated_backlog("Item A", "Item B")
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(data, indent=2))
    return str(p)


# ── add ───────────────────────────────────────────────────────────────────────

class TestAdd:
    def test_add_creates_item(self, empty_file):
        result = runner.invoke(app, ["add", "My New Task", "--file", empty_file])
        assert result.exit_code == 0, result.output
        assert "My New Task" in result.output

    def test_add_id_is_8char_alphanumeric(self, empty_file):
        runner.invoke(app, ["add", "Task with ID", "--file", empty_file])
        data = json.loads(Path(empty_file).read_text())
        item = data["items"][0]
        assert _ID_RE.match(item["id"]), f"id={item['id']!r} is not 8-char alphanumeric"

    def test_add_default_status_is_backlog(self, empty_file):
        runner.invoke(app, ["add", "Status check", "--file", empty_file])
        data = json.loads(Path(empty_file).read_text())
        assert data["items"][0]["status"] == "backlog"

    def test_add_default_priority_weight_absent_when_not_specified(self, empty_file):
        # When --priority-weight is not given, the CLI passes None → field is stripped
        # from the JSON (store omits None-valued optional fields). The effective
        # in-store default is 5, but it is not persisted unless explicitly set.
        runner.invoke(app, ["add", "Weight check", "--file", empty_file])
        data = json.loads(Path(empty_file).read_text())
        item = data["items"][0]
        # priority_weight is absent (None-stripped) OR present with value 5
        pw = item.get("priority_weight")
        assert pw is None or pw == 5

    def test_add_default_tags_is_empty_list(self, empty_file):
        runner.invoke(app, ["add", "Tags check", "--file", empty_file])
        data = json.loads(Path(empty_file).read_text())
        assert data["items"][0]["tags"] == []

    def test_add_default_threads_links_lane_history_empty(self, empty_file):
        runner.invoke(app, ["add", "Defaults check", "--file", empty_file])
        data = json.loads(Path(empty_file).read_text())
        item = data["items"][0]
        assert item["threads"] == []
        assert item["links"] == []
        assert item["lane_history"] == []

    def test_add_with_tags(self, empty_file):
        runner.invoke(app, ["add", "Tagged", "--file", empty_file, "--tags", "alpha,beta"])
        data = json.loads(Path(empty_file).read_text())
        assert data["items"][0]["tags"] == ["alpha", "beta"]

    def test_add_with_description(self, empty_file):
        runner.invoke(app, ["add", "Described", "--file", empty_file, "--description", "Some detail"])
        data = json.loads(Path(empty_file).read_text())
        assert data["items"][0]["description"] == "Some detail"

    def test_add_with_priority_weight(self, empty_file):
        runner.invoke(app, ["add", "High priority", "--file", empty_file, "--priority-weight", "9"])
        data = json.loads(Path(empty_file).read_text())
        assert data["items"][0]["priority_weight"] == 9

    def test_add_env_var_instead_of_flag(self, tmp_path):
        p = tmp_path / "env_backlog.json"
        p.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
        # Pass env explicitly to CliRunner — monkeypatch only affects os.environ
        # but CliRunner uses the env param when provided
        result = runner.invoke(app, ["add", "Env var item"], env={"BACKLOG_FILE": str(p)})
        assert result.exit_code == 0, result.output
        data = json.loads(p.read_text())
        assert data["items"][0]["title"] == "Env var item"


# ── move ──────────────────────────────────────────────────────────────────────

class TestMove:
    def test_move_forward_succeeds(self, backlog_file):
        result = runner.invoke(app, ["move", "1", "ready", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        assert "ready" in result.output.lower()

    def test_move_updates_status_in_file(self, backlog_file):
        runner.invoke(app, ["move", "1", "in-progress", "--file", backlog_file])
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "in-progress"

    def test_move_gate_violation_exits_1(self, gated_backlog_file):
        # backlog → done directly should fail (requires ready then in-progress)
        result = runner.invoke(app, ["move", "1", "done", "--file", gated_backlog_file])
        assert result.exit_code == 1

    def test_move_gate_violation_error_message(self, gated_backlog_file):
        result = runner.invoke(app, ["move", "1", "done", "--file", gated_backlog_file])
        assert result.exit_code == 1
        # CliRunner merges stderr into output
        assert "requires" in result.output.lower() or "gate" in result.output.lower() or "violation" in result.output.lower()

    def test_move_unknown_item_exits_1(self, backlog_file):
        result = runner.invoke(app, ["move", "99", "ready", "--file", backlog_file])
        assert result.exit_code == 1

    def test_move_backward_allowed_even_with_gates(self, gated_backlog_file):
        # Move forward step by step, then back
        runner.invoke(app, ["move", "1", "ready", "--file", gated_backlog_file])
        runner.invoke(app, ["move", "1", "in-progress", "--file", gated_backlog_file])
        result = runner.invoke(app, ["move", "1", "backlog", "--file", gated_backlog_file])
        assert result.exit_code == 0


# ── done ──────────────────────────────────────────────────────────────────────

class TestDone:
    def test_done_sets_status_done(self, backlog_file):
        # Default statuses have no gate requirements — done is allowed from anywhere
        result = runner.invoke(app, ["done", "1", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "done"

    def test_done_from_in_progress_succeeds(self, backlog_file):
        runner.invoke(app, ["move", "1", "in-progress", "--file", backlog_file])
        result = runner.invoke(app, ["done", "1", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "done"

    def test_done_gate_violation_when_requires_in_progress(self, gated_backlog_file):
        # With gated config, backlog→done skips required in-progress
        result = runner.invoke(app, ["done", "1", "--file", gated_backlog_file])
        assert result.exit_code == 1

    def test_done_in_progress_with_gates_succeeds(self, gated_backlog_file):
        # Walk item through required gates then done
        runner.invoke(app, ["move", "1", "ready", "--file", gated_backlog_file])
        runner.invoke(app, ["move", "1", "in-progress", "--file", gated_backlog_file])
        result = runner.invoke(app, ["done", "1", "--file", gated_backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(gated_backlog_file).read_text())
        assert data["items"][0]["status"] == "done"

    def test_done_unknown_item_exits_1(self, backlog_file):
        result = runner.invoke(app, ["done", "99", "--file", backlog_file])
        assert result.exit_code == 1


# ── pick ──────────────────────────────────────────────────────────────────────

class TestPick:
    def _ready_backlog(self, tmp_path, *titles):
        data = make_backlog(*[{"title": t, "status": "ready"} for t in titles])
        p = tmp_path / "backlog.json"
        p.write_text(json.dumps(data, indent=2))
        return str(p)

    def test_pick_sets_status_in_progress(self, tmp_path):
        p = self._ready_backlog(tmp_path, "Ready Task")
        result = runner.invoke(app, ["pick", "alice", "--file", p])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(p).read_text())
        assert data["items"][0]["status"] == "in-progress"

    def test_pick_sets_assigned_to(self, tmp_path):
        p = self._ready_backlog(tmp_path, "Ready Task")
        runner.invoke(app, ["pick", "alice", "--file", p])
        data = json.loads(Path(p).read_text())
        assert data["items"][0]["assigned_to"] == "alice"

    def test_pick_output_mentions_agent(self, tmp_path):
        p = self._ready_backlog(tmp_path, "Ready Task")
        result = runner.invoke(app, ["pick", "alice", "--file", p])
        assert "alice" in result.output

    def test_pick_no_ready_items_exits_1(self, backlog_file):
        # backlog_file has two items with status=backlog, not ready
        result = runner.invoke(app, ["pick", "alice", "--file", backlog_file])
        assert result.exit_code == 1

    def test_pick_no_ready_items_error_message(self, backlog_file):
        result = runner.invoke(app, ["pick", "alice", "--file", backlog_file])
        assert "ready" in result.output.lower() or "no" in result.output.lower()

    def test_pick_picks_first_ready_item(self, tmp_path):
        # Two ready items — first one (position 0) should be picked
        p = self._ready_backlog(tmp_path, "First Ready", "Second Ready")
        runner.invoke(app, ["pick", "bob", "--file", p])
        data = json.loads(Path(p).read_text())
        assert data["items"][0]["status"] == "in-progress"
        assert data["items"][1]["status"] == "ready"  # untouched

    def test_pick_json_flag_returns_json(self, tmp_path):
        p = self._ready_backlog(tmp_path, "JSON Task")
        result = runner.invoke(app, ["pick", "carol", "--file", p, "--json"])
        assert result.exit_code == 0, result.output
        # Should parse as JSON
        parsed = json.loads(result.output)
        assert parsed["status"] == "in-progress"
        assert parsed["assigned_to"] == "carol"


# ── assign ────────────────────────────────────────────────────────────────────

class TestAssign:
    def test_assign_sets_assigned_to(self, backlog_file):
        result = runner.invoke(app, ["assign", "1", "--to", "dave", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["assigned_to"] == "dave"

    def test_assign_output_mentions_assignee(self, backlog_file):
        result = runner.invoke(app, ["assign", "1", "--to", "eve", "--file", backlog_file])
        assert "eve" in result.output

    def test_assign_overwrite_existing(self, backlog_file):
        runner.invoke(app, ["assign", "1", "--to", "first", "--file", backlog_file])
        runner.invoke(app, ["assign", "1", "--to", "second", "--file", backlog_file])
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["assigned_to"] == "second"

    def test_assign_unknown_item_exits_1(self, backlog_file):
        result = runner.invoke(app, ["assign", "99", "--to", "frank", "--file", backlog_file])
        assert result.exit_code == 1

    def test_assign_missing_to_flag_exits_nonzero(self, backlog_file):
        result = runner.invoke(app, ["assign", "1", "--file", backlog_file])
        assert result.exit_code != 0

    def test_unassign_clears_assigned_to(self, backlog_file):
        runner.invoke(app, ["assign", "1", "--to", "george", "--file", backlog_file])
        result = runner.invoke(app, ["unassign", "1", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0].get("assigned_to") is None


# ── discard ───────────────────────────────────────────────────────────────────

class TestDiscard:
    def test_discard_sets_status_discarded(self, backlog_file):
        result = runner.invoke(app, ["discard", "1", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "discarded"

    def test_discard_from_in_progress(self, backlog_file):
        runner.invoke(app, ["move", "1", "in-progress", "--file", backlog_file])
        result = runner.invoke(app, ["discard", "1", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "discarded"

    def test_discard_from_any_lane_no_gate(self, gated_backlog_file):
        # Even with strict gate config, discard should always be allowed
        result = runner.invoke(app, ["discard", "1", "--file", gated_backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(gated_backlog_file).read_text())
        assert data["items"][0]["status"] == "discarded"

    def test_discard_idempotent(self, backlog_file):
        runner.invoke(app, ["discard", "1", "--file", backlog_file])
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "discarded"
        # Invoking once more should not crash (idempotent behavior)
        result = runner.invoke(app, ["discard", "1", "--file", backlog_file])
        # Either success or it remains discarded — no exception
        assert result.exit_code in (0, 1)
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "discarded"

    def test_discard_unknown_item_exits_1(self, backlog_file):
        result = runner.invoke(app, ["discard", "99", "--file", backlog_file])
        assert result.exit_code == 1


# ── restore ───────────────────────────────────────────────────────────────────

class TestRestore:
    def _discarded_backlog(self, tmp_path):
        data = make_backlog({"title": "Discarded Item", "status": "discarded"}, "Normal Item")
        p = tmp_path / "backlog.json"
        p.write_text(json.dumps(data, indent=2))
        return str(p)

    def test_restore_sets_status_backlog(self, tmp_path):
        p = self._discarded_backlog(tmp_path)
        result = runner.invoke(app, ["restore", "1", "--file", p])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(p).read_text())
        assert data["items"][0]["status"] == "backlog"

    def test_restore_output_mentions_backlog(self, tmp_path):
        p = self._discarded_backlog(tmp_path)
        result = runner.invoke(app, ["restore", "1", "--file", p])
        assert "backlog" in result.output.lower()

    def test_restore_from_any_lane(self, backlog_file):
        # restore works even from non-discarded status (always→backlog)
        runner.invoke(app, ["move", "1", "in-progress", "--file", backlog_file])
        result = runner.invoke(app, ["restore", "1", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["status"] == "backlog"

    def test_restore_nonexistent_item_exits_1(self, backlog_file):
        result = runner.invoke(app, ["restore", "99", "--file", backlog_file])
        assert result.exit_code == 1


# ── edit ──────────────────────────────────────────────────────────────────────

class TestEdit:
    def test_edit_title(self, backlog_file):
        result = runner.invoke(app, ["edit", "1", "--file", backlog_file, "--title", "Renamed"])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["title"] == "Renamed"

    def test_edit_does_not_change_other_fields(self, backlog_file):
        original = json.loads(Path(backlog_file).read_text())
        original_status = original["items"][0]["status"]
        runner.invoke(app, ["edit", "1", "--file", backlog_file, "--title", "Changed"])
        data = json.loads(Path(backlog_file).read_text())
        # Status should be unchanged
        assert data["items"][0]["status"] == original_status

    def test_edit_description(self, backlog_file):
        runner.invoke(app, ["edit", "1", "--file", backlog_file, "--description", "New desc"])
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["description"] == "New desc"

    def test_edit_tags(self, backlog_file):
        runner.invoke(app, ["edit", "1", "--file", backlog_file, "--tags", "x,y,z"])
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["tags"] == ["x", "y", "z"]

    def test_edit_priority_weight(self, backlog_file):
        runner.invoke(app, ["edit", "1", "--file", backlog_file, "--priority-weight", "8"])
        data = json.loads(Path(backlog_file).read_text())
        assert data["items"][0]["priority_weight"] == 8

    def test_edit_no_fields_exits_1(self, backlog_file):
        result = runner.invoke(app, ["edit", "1", "--file", backlog_file])
        assert result.exit_code == 1

    def test_edit_unknown_item_exits_1(self, backlog_file):
        result = runner.invoke(app, ["edit", "99", "--file", backlog_file, "--title", "Ghost"])
        assert result.exit_code == 1

    def test_edit_output_confirms_update(self, backlog_file):
        result = runner.invoke(app, ["edit", "1", "--file", backlog_file, "--title", "New Title"])
        assert result.exit_code == 0
        assert "Updated" in result.output or "New Title" in result.output

    def test_edit_multiple_fields_at_once(self, backlog_file):
        runner.invoke(app, [
            "edit", "1", "--file", backlog_file,
            "--title", "Multi Edit",
            "--description", "multi desc",
            "--tags", "a,b",
        ])
        data = json.loads(Path(backlog_file).read_text())
        item = data["items"][0]
        assert item["title"] == "Multi Edit"
        assert item["description"] == "multi desc"
        assert item["tags"] == ["a", "b"]


# ── env var isolation ─────────────────────────────────────────────────────────

class TestEnvVarIsolation:
    def test_each_test_has_isolated_file(self, tmp_path):
        p = tmp_path / "isolated.json"
        p.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
        result = runner.invoke(app, ["add", "Isolated item"], env={"BACKLOG_FILE": str(p)})
        assert result.exit_code == 0, result.output
        data = json.loads(p.read_text())
        assert len(data["items"]) == 1
        assert data["items"][0]["title"] == "Isolated item"

    def test_flag_overrides_env_var(self, tmp_path):
        env_file = tmp_path / "env.json"
        flag_file = tmp_path / "flag.json"
        env_file.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
        flag_file.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
        result = runner.invoke(
            app,
            ["add", "Flag item", "--file", str(flag_file)],
            env={"BACKLOG_FILE": str(env_file)},
        )
        assert result.exit_code == 0, result.output
        # Item should be in flag_file (--file wins)
        env_data = json.loads(env_file.read_text())
        flag_data = json.loads(flag_file.read_text())
        assert len(flag_data["items"]) == 1
        assert len(env_data["items"]) == 0
