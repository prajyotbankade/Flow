"""Unit tests for policy engine functions in backlog.server.

Covers:
- detect_policy_conflicts: no-conflict, conflicting, mixed, empty
- execute_policy_actions: all action types, item-not-found, unknown type, exception path
- run_policy_engine: no active policies, fires with no conflicts, conflict detected, no fires
"""

import pytest
from unittest.mock import patch, MagicMock

from backlog.server import (
    detect_policy_conflicts,
    execute_policy_actions,
    run_policy_engine,
    _CONTRADICTORY_PAIRS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_item(iid="item-1", priority_weight=5, assigned_to=None, skip_count=0, category=None):
    item = {
        "id": iid,
        "title": "Test item",
        "status": "backlog",
        "priority_weight": priority_weight,
        "skip_count": skip_count,
        "threads": [],
        "execution_history": [],
        "tags": [],
    }
    if assigned_to is not None:
        item["assigned_to"] = assigned_to
    if category is not None:
        item["category"] = category
    return item


def _make_fired_policy(policy_id, policy_name, actions):
    return {"policy_id": policy_id, "policy_name": policy_name, "actions": actions}


def _make_action_entry(action, policy_name="p1", policy_id="pid-1"):
    return {"action": action, "policy_name": policy_name, "policy_id": policy_id}


def _make_data(*items):
    return {"items": list(items)}


# ── detect_policy_conflicts ───────────────────────────────────────────────────


class TestDetectPolicyConflicts:
    def test_empty_input_returns_empty_lists(self):
        result = detect_policy_conflicts([])
        assert result == {"conflicts": [], "clean": []}

    def test_no_conflict_path_all_clean(self):
        fired = [
            _make_fired_policy("p1", "P1", [{"type": "reprioritize", "item_id": "item-1", "priority_weight": 8}]),
            _make_fired_policy("p2", "P2", [{"type": "notify",        "item_id": "item-2", "message": "hi"}]),
        ]
        result = detect_policy_conflicts(fired)
        assert result["conflicts"] == []
        assert len(result["clean"]) == 2

    def test_contradictory_pair_escalate_skip_force_detected(self):
        # ("escalate", "skip_force") is a known contradictory pair
        fired = [
            _make_fired_policy("p1", "P1", [{"type": "escalate",   "item_id": "item-1"}]),
            _make_fired_policy("p2", "P2", [{"type": "skip_force", "item_id": "item-1"}]),
        ]
        result = detect_policy_conflicts(fired)
        assert len(result["conflicts"]) == 1
        assert result["clean"] == []
        conflict = result["conflicts"][0]
        assert conflict["item_id"] == "item-1"
        assert set(conflict["policy_names"]) == {"P1", "P2"}

    def test_contradictory_pair_escalate_block_detected(self):
        # ("block", "escalate") is a known contradictory pair
        fired = [
            _make_fired_policy("p1", "P1", [{"type": "escalate", "item_id": "item-7"}]),
            _make_fired_policy("p2", "P2", [{"type": "block",    "item_id": "item-7"}]),
        ]
        result = detect_policy_conflicts(fired)
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["item_id"] == "item-7"

    def test_mixed_input_some_clean_some_conflicting(self):
        # item-A conflicts, item-B does not
        fired = [
            _make_fired_policy("p1", "P1", [
                {"type": "escalate",      "item_id": "item-A"},
                {"type": "reprioritize",  "item_id": "item-B", "priority_weight": 3},
            ]),
            _make_fired_policy("p2", "P2", [
                {"type": "skip_force", "item_id": "item-A"},
            ]),
        ]
        result = detect_policy_conflicts(fired)
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["item_id"] == "item-A"
        # item-B action must land in clean
        clean_ids = [e["action"]["item_id"] for e in result["clean"]]
        assert "item-B" in clean_ids

    def test_non_conflicting_actions_on_same_item_are_clean(self):
        # reprioritize + notify on same item — not a contradictory pair
        fired = [
            _make_fired_policy("p1", "P1", [{"type": "reprioritize", "item_id": "item-1", "priority_weight": 7}]),
            _make_fired_policy("p2", "P2", [{"type": "notify",       "item_id": "item-1", "message": "heads-up"}]),
        ]
        result = detect_policy_conflicts(fired)
        assert result["conflicts"] == []
        assert len(result["clean"]) == 2

    def test_global_actions_without_item_id_grouped_under_global(self):
        fired = [
            _make_fired_policy("p1", "P1", [{"type": "notify", "message": "no item"}]),
        ]
        result = detect_policy_conflicts(fired)
        assert result["conflicts"] == []
        assert len(result["clean"]) == 1


# ── execute_policy_actions ────────────────────────────────────────────────────


class TestExecutePolicyActions:
    def test_reprioritize_updates_priority_weight(self):
        item = _make_item("i1", priority_weight=3)
        data = _make_data(item)
        entries = [_make_action_entry({"type": "reprioritize", "item_id": "i1", "priority_weight": 9})]
        results = execute_policy_actions(entries, data)
        assert results[0]["status"] == "executed"
        assert item["priority_weight"] == 9

    def test_reassign_updates_assigned_to(self):
        item = _make_item("i1")
        data = _make_data(item)
        entries = [_make_action_entry({"type": "reassign", "item_id": "i1", "agent": "alice"})]
        results = execute_policy_actions(entries, data)
        assert results[0]["status"] == "executed"
        assert item["assigned_to"] == "alice"

    def test_escalate_sets_priority_weight_to_max_9(self):
        item = _make_item("i1", priority_weight=5)
        data = _make_data(item)
        entries = [_make_action_entry({"type": "escalate", "item_id": "i1"})]
        execute_policy_actions(entries, data)
        assert item["priority_weight"] == 9

    def test_escalate_does_not_lower_already_high_priority(self):
        item = _make_item("i1", priority_weight=10)
        data = _make_data(item)
        entries = [_make_action_entry({"type": "escalate", "item_id": "i1"})]
        execute_policy_actions(entries, data)
        assert item["priority_weight"] == 10

    def test_escalate_sets_category_to_bug_when_missing(self):
        item = _make_item("i1")
        data = _make_data(item)
        entries = [_make_action_entry({"type": "escalate", "item_id": "i1"})]
        execute_policy_actions(entries, data)
        assert item.get("category") == "bug"

    def test_escalate_does_not_overwrite_existing_category(self):
        item = _make_item("i1", category="feature")
        data = _make_data(item)
        entries = [_make_action_entry({"type": "escalate", "item_id": "i1"})]
        execute_policy_actions(entries, data)
        assert item["category"] == "feature"

    def test_block_adds_thread_with_waiting_on_user(self):
        item = _make_item("i1")
        data = _make_data(item)
        entries = [_make_action_entry({"type": "block", "item_id": "i1", "reason": "needs review"})]
        results = execute_policy_actions(entries, data)
        assert results[0]["status"] == "executed"
        assert len(item["threads"]) == 1
        assert item["threads"][0]["waiting_on"] == "user"

    def test_notify_returns_notified_status_without_mutating_item(self):
        item = _make_item("i1")
        data = _make_data(item)
        entries = [_make_action_entry({"type": "notify", "item_id": "i1", "message": "alert!"})]
        results = execute_policy_actions(entries, data)
        assert results[0]["status"] == "notified"
        # Item should not be mutated by notify
        assert item.get("updated_at") is None

    def test_skip_force_increments_skip_count(self):
        item = _make_item("i1", skip_count=2)
        data = _make_data(item)
        entries = [_make_action_entry({"type": "skip_force", "item_id": "i1"})]
        results = execute_policy_actions(entries, data)
        assert results[0]["status"] == "executed"
        assert item["skip_count"] == 3

    def test_item_not_found_returns_skipped(self):
        data = _make_data()  # no items
        entries = [_make_action_entry({"type": "reprioritize", "item_id": "ghost-id", "priority_weight": 8})]
        results = execute_policy_actions(entries, data)
        assert results[0]["status"] == "skipped"

    def test_unknown_action_type_returns_skipped(self):
        item = _make_item("i1")
        data = _make_data(item)
        entries = [_make_action_entry({"type": "BOGUS_ACTION", "item_id": "i1"})]
        results = execute_policy_actions(entries, data)
        assert results[0]["status"] == "skipped"

    def test_exception_mid_action_returns_error_and_continues_remaining(self):
        # Cause an exception by passing a non-dict action to force an AttributeError
        # We'll use a bad priority_weight that triggers on string concat in detail
        item = _make_item("i1", priority_weight=None)
        data = _make_data(item)

        # Patch the item to raise when accessed inside execute_policy_actions
        class _BadDict(dict):
            def get(self, key, default=None):
                if key == "priority_weight":
                    raise RuntimeError("forced error")
                return super().get(key, default)

        bad_item = _BadDict(_make_item("i1"))
        bad_data = {"items": [bad_item]}

        good_item = _make_item("i2", priority_weight=3)
        bad_data["items"].append(good_item)

        entries = [
            _make_action_entry({"type": "reprioritize", "item_id": "i1", "priority_weight": 5}),
            _make_action_entry({"type": "reprioritize", "item_id": "i2", "priority_weight": 8}),
        ]
        results = execute_policy_actions(entries, bad_data)
        assert results[0]["status"] == "error"
        assert results[1]["status"] == "executed"
        assert good_item["priority_weight"] == 8

    def test_empty_action_entries_returns_empty_list(self):
        data = _make_data(_make_item("i1"))
        results = execute_policy_actions([], data)
        assert results == []


# ── run_policy_engine ─────────────────────────────────────────────────────────


_MODULE = "backlog.server"


class TestRunPolicyEngine:
    def _base_patches(self):
        """Return a dict of patch targets with safe defaults."""
        return {
            f"{_MODULE}.read_policies": MagicMock(return_value={"policies": []}),
            f"{_MODULE}.get_policies_path": MagicMock(return_value="/fake/policies.json"),
            f"{_MODULE}.get_policy_log_path": MagicMock(return_value="/fake/policy_log.json"),
            f"{_MODULE}.append_policy_log_entry": MagicMock(),
            f"{_MODULE}.save_policies": MagicMock(),
            f"{_MODULE}.evaluate_policy_structured": MagicMock(return_value={"fires": False, "reasoning": "", "actions": []}),
            f"{_MODULE}.evaluate_policy_with_llm": MagicMock(return_value={"fires": False, "reasoning": "", "actions": []}),
            f"{_MODULE}.resolve_conflicts_with_llm": MagicMock(return_value=[]),
        }

    def test_no_active_policies_returns_zero_result(self):
        patches = self._base_patches()
        patches[f"{_MODULE}.read_policies"] = MagicMock(return_value={"policies": []})
        data = _make_data(_make_item("i1"))
        with _multi_patch(patches):
            result = run_policy_engine(data, "/fake/backlog.json")
        assert result == {"fires": 0, "actions_executed": 0, "notifications": [], "log_ids": []}

    def test_no_policies_fire_returns_zero_result_with_log_ids(self):
        policy = {"id": "pol-1", "name": "P1", "active": True, "conditions": {"field": "status", "op": "eq", "value": "x"}, "priority": 5}
        patches = self._base_patches()
        patches[f"{_MODULE}.read_policies"] = MagicMock(return_value={"policies": [policy]})
        patches[f"{_MODULE}.evaluate_policy_structured"] = MagicMock(
            return_value={"fires": False, "reasoning": "nope", "actions": []}
        )
        data = _make_data(_make_item("i1"))
        with _multi_patch(patches):
            result = run_policy_engine(data, "/fake/backlog.json")
        assert result["fires"] == 0
        assert result["actions_executed"] == 0
        assert len(result["log_ids"]) == 1  # one log entry written even when no fire

    def test_policies_fire_no_conflicts_actions_executed(self):
        item = _make_item("i1", priority_weight=3)
        data = _make_data(item)
        policy = {"id": "pol-1", "name": "P1", "active": True, "conditions": {"field": "status"}, "priority": 5}
        action = {"type": "reprioritize", "item_id": "i1", "priority_weight": 9}
        patches = self._base_patches()
        patches[f"{_MODULE}.read_policies"] = MagicMock(return_value={"policies": [policy]})
        patches[f"{_MODULE}.evaluate_policy_structured"] = MagicMock(
            return_value={"fires": True, "reasoning": "yes", "actions": [action]}
        )
        with _multi_patch(patches):
            result = run_policy_engine(data, "/fake/backlog.json")
        assert result["fires"] == 1
        assert result["actions_executed"] == 1
        assert item["priority_weight"] == 9

    def test_conflict_detected_resolved_actions_executed(self):
        item = _make_item("i1", priority_weight=4)
        data = _make_data(item)
        policy1 = {"id": "p1", "name": "P1", "active": True, "conditions": {"field": "x"}, "priority": 5}
        policy2 = {"id": "p2", "name": "P2", "active": True, "conditions": {"field": "y"}, "priority": 4}
        escalate_action = {"type": "escalate",   "item_id": "i1"}
        skip_action     = {"type": "skip_force", "item_id": "i1"}
        resolved_action = {"type": "escalate",   "item_id": "i1"}  # winner chosen

        patches = self._base_patches()
        patches[f"{_MODULE}.read_policies"] = MagicMock(return_value={"policies": [policy1, policy2]})
        call_count = {"n": 0}

        def _eval_side_effect(policy, context):
            call_count["n"] += 1
            if policy["id"] == "p1":
                return {"fires": True, "reasoning": "r1", "actions": [escalate_action]}
            return {"fires": True, "reasoning": "r2", "actions": [skip_action]}

        patches[f"{_MODULE}.evaluate_policy_structured"] = MagicMock(side_effect=_eval_side_effect)
        patches[f"{_MODULE}.resolve_conflicts_with_llm"] = MagicMock(
            return_value=[{"action": resolved_action, "resolution_reasoning": "P1 wins", "conflict_item_id": "i1"}]
        )
        with _multi_patch(patches):
            result = run_policy_engine(data, "/fake/backlog.json")
        assert result["fires"] == 2
        # The resolved action (escalate) must have been executed
        assert result["actions_executed"] == 1
        assert item["priority_weight"] == 9

    def test_inactive_policy_skipped(self):
        policy = {"id": "pol-1", "name": "P1", "active": False, "conditions": {}, "priority": 5}
        patches = self._base_patches()
        patches[f"{_MODULE}.read_policies"] = MagicMock(return_value={"policies": [policy]})
        data = _make_data(_make_item("i1"))
        with _multi_patch(patches):
            result = run_policy_engine(data, "/fake/backlog.json")
        assert result == {"fires": 0, "actions_executed": 0, "notifications": [], "log_ids": []}

    def test_notify_action_surfaces_in_notifications(self):
        item = _make_item("i1")
        data = _make_data(item)
        policy = {"id": "pol-1", "name": "P1", "active": True, "conditions": {"field": "x"}, "priority": 5}
        notify_action = {"type": "notify", "item_id": "i1", "message": "critical alert"}
        patches = self._base_patches()
        patches[f"{_MODULE}.read_policies"] = MagicMock(return_value={"policies": [policy]})
        patches[f"{_MODULE}.evaluate_policy_structured"] = MagicMock(
            return_value={"fires": True, "reasoning": "yes", "actions": [notify_action]}
        )
        with _multi_patch(patches):
            result = run_policy_engine(data, "/fake/backlog.json")
        assert "critical alert" in result["notifications"]


# ── Context manager helper ────────────────────────────────────────────────────


class _multi_patch:
    """Apply multiple patches as a single context manager."""

    def __init__(self, patch_dict):
        self._patch_dict = patch_dict
        self._patchers = []

    def __enter__(self):
        for target, mock_obj in self._patch_dict.items():
            p = patch(target, mock_obj)
            p.start()
            self._patchers.append(p)
        return self

    def __exit__(self, *args):
        for p in reversed(self._patchers):
            p.stop()
