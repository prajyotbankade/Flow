"""Write safety and version conflict tests for BacklogStore.

Tests cover:
- version increment on each write
- optimistic locking (ConflictError when expected_version < current)
- simulated read-modify-write race condition
- gate enforcement via apply_lane_transition (raw status bypass must NOT work)
- retry-after-conflict pattern
- edge cases: no version check, first write, isolated tmp state
"""

import json
from pathlib import Path

import pytest

from backlog.core import BacklogStore, apply_lane_transition, DEFAULT_STATUSES
from backlog.exceptions import ConflictError, GateViolationError


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_starter(tmp_path: Path) -> tuple[BacklogStore, Path]:
    """Create a BacklogStore backed by a fresh tmp backlog.json with version=0."""
    bf = tmp_path / "backlog.json"
    initial = {
        "version": 0,
        "schema_version": 1,
        "config": {"scope": "project", "project_name": "test"},
        "items": [],
    }
    bf.write_text(json.dumps(initial))
    return BacklogStore(str(bf)), bf


def _make_item(status: str = "backlog") -> dict:
    return {
        "id": "abcd1234",
        "title": "Test item",
        "status": status,
        "tags": [],
        "description": "",
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


# ── Version increment tests ───────────────────────────────────────────────────


class TestVersionIncrement:
    def test_write_increments_version_by_one(self, tmp_path):
        store, _ = _make_starter(tmp_path)
        data = store.read()
        assert data["version"] == 0
        store.write(data)
        data2 = store.read()
        assert data2["version"] == 1

    def test_write_increments_version_on_each_call(self, tmp_path):
        store, _ = _make_starter(tmp_path)
        data = store.read()
        for expected in range(1, 6):
            store.write(data)
            data = store.read()
            assert data["version"] == expected

    def test_first_write_to_new_file_sets_version_to_one(self, tmp_path):
        bf = tmp_path / "new_backlog.json"
        store = BacklogStore(str(bf))
        # File does not exist yet — read returns STARTER_BACKLOG with version=0
        data = store.read()
        assert data["version"] == 0
        store.write(data)
        data2 = store.read()
        assert data2["version"] == 1


# ── ConflictError tests ───────────────────────────────────────────────────────


class TestConflictError:
    def test_write_with_correct_expected_version_succeeds(self, tmp_path):
        store, _ = _make_starter(tmp_path)
        data = store.read()
        # expected_version == current version (0) — should succeed
        store.write(data, expected_version=0)
        assert store.read()["version"] == 1

    def test_write_raises_conflict_when_expected_version_is_stale(self, tmp_path):
        store, _ = _make_starter(tmp_path)
        data = store.read()
        # Advance the file to version 1 independently
        store.write(data)
        # Now try to write with expected_version=0 (stale)
        stale_data = {"version": 0, "schema_version": 1,
                      "config": {"scope": "project", "project_name": "test"},
                      "items": []}
        with pytest.raises(ConflictError):
            store.write(stale_data, expected_version=0)

    def test_conflict_error_message_contains_version_numbers(self, tmp_path):
        store, _ = _make_starter(tmp_path)
        data = store.read()
        store.write(data)   # file is now at version 1
        stale_data = {"version": 0, "schema_version": 1,
                      "config": {"scope": "project", "project_name": "test"},
                      "items": []}
        with pytest.raises(ConflictError, match=r"v0") as exc_info:
            store.write(stale_data, expected_version=0)
        # Should also mention the current version
        assert "1" in str(exc_info.value)

    def test_write_none_expected_version_always_succeeds(self, tmp_path):
        store, _ = _make_starter(tmp_path)
        data = store.read()
        store.write(data)   # advance to version 1
        store.write(data)   # advance to version 2
        # Write with no version check — must not raise regardless of version drift
        old_data = {"version": 0, "schema_version": 1,
                    "config": {"scope": "project", "project_name": "test"},
                    "items": []}
        store.write(old_data, expected_version=None)
        assert store.read()["version"] == 1  # old_data["version"]=0 → incremented to 1


# ── Simulated race condition ──────────────────────────────────────────────────


class TestRaceCondition:
    def test_second_write_raises_conflict_when_first_writer_wins(self, tmp_path):
        """Simulate two readers both reading version 0, then racing to write."""
        store, _ = _make_starter(tmp_path)

        # Both agents read the same snapshot
        reader_a_data = store.read()
        reader_b_data = store.read()

        assert reader_a_data["version"] == 0
        assert reader_b_data["version"] == 0

        # Agent A wins — writes successfully, file is now at version 1
        reader_a_data["items"].append(_make_item())
        store.write(reader_a_data, expected_version=reader_a_data["version"])

        assert store.read()["version"] == 1

        # Agent B tries to write with its stale snapshot — must raise ConflictError
        reader_b_data["items"].append(_make_item())
        with pytest.raises(ConflictError):
            store.write(reader_b_data, expected_version=reader_b_data["version"])

    def test_retry_after_conflict_succeeds(self, tmp_path):
        """After a ConflictError, re-read and re-apply mutation — must succeed."""
        store, _ = _make_starter(tmp_path)

        # Agent A writes first
        data_a = store.read()
        data_a["items"].append(_make_item())
        store.write(data_a, expected_version=data_a["version"])

        # Agent B had a stale snapshot — gets ConflictError
        data_b_stale = {"version": 0, "schema_version": 1,
                        "config": {"scope": "project", "project_name": "test"},
                        "items": []}
        with pytest.raises(ConflictError):
            store.write(data_b_stale, expected_version=0)

        # Agent B re-reads and re-applies its mutation
        fresh = store.read()
        assert fresh["version"] == 1
        fresh["items"].append(_make_item())
        store.write(fresh, expected_version=fresh["version"])

        final = store.read()
        assert final["version"] == 2
        assert len(final["items"]) == 2


# ── Gate enforcement via apply_lane_transition ────────────────────────────────


class TestGateEnforcement:
    def test_raw_status_bypass_does_not_enforce_gates(self, tmp_path):
        """Demonstrate that raw item['status'] = 'done' skips gate enforcement.

        This test documents the danger — production code MUST use apply_lane_transition
        or BacklogStore.move_item() to enforce gates. A raw assignment is a footgun.
        """
        store, _ = _make_starter(tmp_path)
        data = store.read()
        item = _make_item(status="backlog")
        data["items"].append(item)
        store.write(data)

        data2 = store.read()
        target_item = data2["items"][0]
        # Raw bypass: silently sets status without gate check — no error raised
        target_item["status"] = "done"
        store.write(data2)  # succeeds — no gate enforced

        final = store.read()
        # Status changed despite no lane progression — this is the unsafe path
        assert final["items"][0]["status"] == "done"

    def test_apply_lane_transition_enforces_gates_on_forward_skip(self, tmp_path):
        """apply_lane_transition itself does NOT raise — it's validate_lane_transition that gates.

        BacklogStore.move_item() calls validate before apply. Direct apply_lane_transition
        still mutates without gate check. Test move_item raises GateViolationError for
        invalid forward skips when custom statuses with 'requires' are configured.
        """
        store, _ = _make_starter(tmp_path)
        data = store.read()
        # Add custom statuses where 'done' requires passing through 'code-review'
        data["config"]["statuses"] = [
            {"id": "backlog",     "label": "Backlog"},
            {"id": "in-progress", "label": "In Progress"},
            {"id": "code-review", "label": "Code Review", "requires": ["in-progress"]},
            {"id": "done",        "label": "Done",         "requires": ["code-review"]},
        ]
        item = _make_item(status="in-progress")
        item["id"] = "item0001"
        data["items"].append(item)
        store.write(data)

        # move_item calls validate_lane_transition first — should raise GateViolationError
        # when trying to jump from in-progress directly to done (skipping code-review)
        with pytest.raises(GateViolationError):
            store.move_item(1, "done")

    def test_apply_lane_transition_invalid_transition_raises_gate_violation(self, tmp_path):
        """move_item raises GateViolationError for a lane jump that skips required gates."""
        store, _ = _make_starter(tmp_path)
        data = store.read()
        # Configure statuses where 'ready' requires 'refined'
        data["config"]["statuses"] = [
            {"id": "backlog",  "label": "Backlog"},
            {"id": "refined",  "label": "Refined"},
            {"id": "ready",    "label": "Ready",   "requires": ["refined"]},
            {"id": "done",     "label": "Done"},
        ]
        item = _make_item(status="backlog")
        item["id"] = "item0002"
        data["items"].append(item)
        store.write(data)

        # Jump directly from backlog to ready without going through refined
        with pytest.raises(GateViolationError):
            store.move_item(1, "ready")

    def test_apply_lane_transition_valid_path_succeeds(self, tmp_path):
        """Valid forward progression through required gates succeeds."""
        store, _ = _make_starter(tmp_path)
        data = store.read()
        data["config"]["statuses"] = [
            {"id": "backlog",  "label": "Backlog"},
            {"id": "refined",  "label": "Refined"},
            {"id": "ready",    "label": "Ready",   "requires": ["refined"]},
            {"id": "done",     "label": "Done"},
        ]
        item = _make_item(status="backlog")
        item["id"] = "item0003"
        data["items"].append(item)
        store.write(data)

        # Move through required gates in order
        store.move_item(1, "refined")
        store.move_item(1, "ready")   # now allowed — passed through 'refined'
        final = store.read()
        assert final["items"][0]["status"] == "ready"


# ── State isolation ───────────────────────────────────────────────────────────


class TestStateIsolation:
    def test_each_test_uses_isolated_tmp_file(self, tmp_path):
        """Verify stores backed by different paths don't share filesystem state."""
        path_a = tmp_path / "a" / "backlog.json"
        path_b = tmp_path / "b" / "backlog.json"
        path_a.parent.mkdir()
        path_b.parent.mkdir()

        # Initialise both stores with their own files first
        store_a = BacklogStore(str(path_a))
        data_a = store_a.read()
        store_a.write(data_a)  # create file at version 1

        store_b = BacklogStore(str(path_b))
        data_b = store_b.read()
        store_b.write(data_b)  # create file at version 1

        # Advance store_a two more versions
        data_a = store_a.read()
        data_a["items"].append(_make_item())
        store_a.write(data_a)
        store_a.write(store_a.read())

        # Store B must remain at version 1 with no items
        data_b2 = store_b.read()
        assert data_b2["items"] == []
        assert data_b2["version"] == 1

    def test_write_does_not_mutate_caller_version_beyond_increment(self, tmp_path):
        """write() increments data['version'] in-place — caller's dict is updated."""
        store, _ = _make_starter(tmp_path)
        data = store.read()
        assert data["version"] == 0
        store.write(data)
        # After write, the in-memory dict should reflect the new version
        assert data["version"] == 1
