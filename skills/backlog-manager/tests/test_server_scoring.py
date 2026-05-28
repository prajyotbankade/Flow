"""Unit tests for server.py scoring engine: compute_scores, evaluate_tribunal, and lens functions."""

import unittest
from datetime import datetime, timezone, timedelta

from backlog.server import (
    compute_scores,
    evaluate_tribunal,
    evaluate_lens_urgency,
    evaluate_lens_leverage,
    evaluate_lens_risk,
    evaluate_lens_momentum,
    evaluate_lens_strategic,
    evaluate_lens_agent_fit,
    compute_freshness,
    DEFAULT_SCORING,
    DEFAULT_STRATEGIC,
    LENS_WEIGHTS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_item(
    id="1",
    title="Test item",
    status="ready",
    priority_weight=5,
    complexity="medium",
    category="feature",
    tags=None,
    links=None,
    reopen_count=0,
    skip_count=0,
    updated_at=None,
    assigned_to=None,
    lane_history=None,
    readiness_signals=None,
):
    if updated_at is None:
        updated_at = datetime.now(timezone.utc).isoformat()
    return {
        "id": id,
        "title": title,
        "status": status,
        "priority_weight": priority_weight,
        "complexity": complexity,
        "category": category,
        "tags": tags or [],
        "links": links or [],
        "reopen_count": reopen_count,
        "skip_count": skip_count,
        "updated_at": updated_at,
        "assigned_to": assigned_to,
        "lane_history": lane_history or [],
        "readiness_signals": readiness_signals or [],
    }


def _make_data(items, agents=None, scoring=None, strategic=None):
    config = {}
    if agents:
        config["agents"] = agents
    if scoring:
        config["scoring"] = scoring
    if strategic:
        config["strategic"] = strategic
    return {"items": items, "config": config}


def _stale_ts(days):
    """Return ISO timestamp for a date *days* ago."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _fresh_ts(days=1):
    """Return ISO timestamp for a date *days* ago (recent)."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# compute_freshness
# ---------------------------------------------------------------------------

class TestComputeFreshness(unittest.TestCase):

    def test_very_recent_item_returns_positive(self):
        ts = _fresh_ts(0)
        result = compute_freshness(ts, DEFAULT_SCORING)
        self.assertGreater(result, 0.0)

    def test_within_boost_window_returns_positive(self):
        ts = _fresh_ts(2)
        result = compute_freshness(ts, DEFAULT_SCORING)
        self.assertGreater(result, 0.0)

    def test_neutral_between_boost_and_decay(self):
        # Between boost_days (3) and decay_days (14) — should return 0.0
        ts = _fresh_ts(7)
        result = compute_freshness(ts, DEFAULT_SCORING)
        self.assertEqual(result, 0.0)

    def test_stale_item_returns_negative(self):
        ts = _stale_ts(30)
        result = compute_freshness(ts, DEFAULT_SCORING)
        self.assertLess(result, 0.0)

    def test_invalid_timestamp_returns_zero(self):
        result = compute_freshness("not-a-date", DEFAULT_SCORING)
        self.assertEqual(result, 0.0)

    def test_empty_timestamp_returns_zero(self):
        result = compute_freshness("", DEFAULT_SCORING)
        self.assertEqual(result, 0.0)


# ---------------------------------------------------------------------------
# compute_scores
# ---------------------------------------------------------------------------

class TestComputeScores(unittest.TestCase):

    def test_empty_data_returns_empty_list(self):
        result = compute_scores({"items": [], "config": {}})
        self.assertEqual(result, [])

    def test_single_item_returns_one_result(self):
        item = _make_item(id="1", priority_weight=7)
        data = _make_data([item])
        results = compute_scores(data)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "1")

    def test_result_has_required_keys(self):
        item = _make_item(id="1")
        results = compute_scores(_make_data([item]))
        r = results[0]
        for key in ("id", "title", "status", "score", "score_breakdown", "readiness",
                    "recommended_agent", "recommended_model"):
            self.assertIn(key, r)

    def test_score_breakdown_has_expected_components(self):
        item = _make_item(id="1")
        results = compute_scores(_make_data([item]))
        breakdown = results[0]["score_breakdown"]
        for comp in ("base_priority", "unblock", "freshness", "complexity",
                     "blocked_penalty", "quick_win", "reopen", "skip_floor", "critical_bug"):
            self.assertIn(comp, breakdown)

    def test_critical_bug_boost_applied(self):
        bug = _make_item(id="1", category="bug", priority_weight=9)
        feature = _make_item(id="2", category="feature", priority_weight=9)
        results = compute_scores(_make_data([bug, feature]))
        bug_r = next(r for r in results if r["id"] == "1")
        feature_r = next(r for r in results if r["id"] == "2")
        self.assertGreater(bug_r["score_breakdown"]["critical_bug"], 0)
        self.assertEqual(feature_r["score_breakdown"]["critical_bug"], 0)

    def test_results_sorted_descending_by_score(self):
        # High priority item should outrank low priority
        high = _make_item(id="h", priority_weight=10)
        low = _make_item(id="l", priority_weight=1)
        results = compute_scores(_make_data([low, high]))
        self.assertEqual(results[0]["id"], "h")

    def test_reopen_penalty_reduces_score(self):
        clean = _make_item(id="c", priority_weight=5, reopen_count=0)
        reopened = _make_item(id="r", priority_weight=5, reopen_count=3)
        results = compute_scores(_make_data([clean, reopened]))
        clean_r = next(r for r in results if r["id"] == "c")
        reopen_r = next(r for r in results if r["id"] == "r")
        self.assertGreater(clean_r["score"], reopen_r["score"])

    def test_unblock_boost_for_item_that_blocks_others(self):
        # Item 1 blocks item 2
        blocker = _make_item(id="1", links=[{"type": "blocks", "item_id": "2"}])
        blocked = _make_item(id="2")
        results = compute_scores(_make_data([blocker, blocked]))
        blocker_r = next(r for r in results if r["id"] == "1")
        self.assertGreater(blocker_r["score_breakdown"]["unblock"], 0)

    def test_blocked_penalty_applied_to_blocked_item(self):
        blocker = _make_item(id="b1", status="in-progress")
        blocked = _make_item(id="b2")
        # b1 blocks b2: use links on b1
        blocker["links"] = [{"type": "blocks", "item_id": "b2"}]
        results = compute_scores(_make_data([blocker, blocked]))
        blocked_r = next(r for r in results if r["id"] == "b2")
        self.assertLess(blocked_r["score_breakdown"]["blocked_penalty"], 0)

    def test_readiness_level_not_ready_for_backlog_item(self):
        item = _make_item(id="1", status="backlog")
        results = compute_scores(_make_data([item]))
        self.assertEqual(results[0]["readiness"]["level"], "not_ready")

    def test_recommended_model_follows_complexity(self):
        low = _make_item(id="l", complexity="low")
        high = _make_item(id="h", complexity="high")
        results = compute_scores(_make_data([low, high]))
        low_r = next(r for r in results if r["id"] == "l")
        high_r = next(r for r in results if r["id"] == "h")
        self.assertEqual(low_r["recommended_model"], "haiku")
        self.assertEqual(high_r["recommended_model"], "opus")

    def test_agent_recommended_when_skill_matches(self):
        item = _make_item(id="1", tags=["python"])
        agents = {"alice": {"skills": ["python"], "max_active": 3, "preferred_complexity": []}}
        results = compute_scores(_make_data([item], agents=agents))
        self.assertEqual(results[0]["recommended_agent"], "alice")

    def test_no_agent_recommended_when_no_match(self):
        item = _make_item(id="1", tags=["rust"])
        agents = {"alice": {"skills": ["python"], "max_active": 3, "preferred_complexity": []}}
        results = compute_scores(_make_data([item], agents=agents))
        # affinity is 0 (no tag match), best_affinity <= 0 => None
        self.assertIsNone(results[0]["recommended_agent"])

    def test_skip_floor_raises_score(self):
        # skip_floor_per is positive, so more skips = higher skip_floor component
        item_skipped = _make_item(id="s", skip_count=5, priority_weight=5)
        item_clean = _make_item(id="c", skip_count=0, priority_weight=5)
        results = compute_scores(_make_data([item_skipped, item_clean]))
        s_r = next(r for r in results if r["id"] == "s")
        c_r = next(r for r in results if r["id"] == "c")
        self.assertGreater(s_r["score_breakdown"]["skip_floor"], c_r["score_breakdown"]["skip_floor"])


# ---------------------------------------------------------------------------
# evaluate_lens_urgency
# ---------------------------------------------------------------------------

class TestEvaluateLensUrgency(unittest.TestCase):

    def _call(self, item, breakdown=None):
        return evaluate_lens_urgency(item, breakdown or {}, DEFAULT_SCORING)

    def test_returns_lens_name_urgency(self):
        item = _make_item(id="1")
        result = self._call(item)
        self.assertEqual(result["lens"], "urgency")

    def test_critical_bug_adds_ten_points(self):
        item = _make_item(id="1", category="bug", priority_weight=9)
        breakdown = {"critical_bug": 5.0}
        result = self._call(item, breakdown)
        self.assertGreaterEqual(result["score"], 10.0)

    def test_high_priority_adds_score(self):
        item = _make_item(id="1", priority_weight=9)
        result = self._call(item)
        self.assertGreater(result["score"], 0)
        self.assertIn("9/10", result["argument"])

    def test_medium_priority_adds_partial_score(self):
        item = _make_item(id="1", priority_weight=6)
        result = self._call(item)
        self.assertGreater(result["score"], 0)
        # should not include "High priority" reasoning
        self.assertNotIn("High priority", result["argument"] or "")

    def test_low_priority_adds_zero_score(self):
        item = _make_item(id="1", priority_weight=3)
        result = self._call(item)
        # pw < 5, no freshness issue: score should be 0
        self.assertEqual(result["score"], 0.0)

    def test_stale_item_adds_urgency(self):
        item = _make_item(id="1", priority_weight=3, updated_at=_stale_ts(30))
        breakdown = {"freshness": -0.5}
        result = self._call(item, breakdown)
        self.assertGreater(result["score"], 0)
        self.assertIn("stale", result["argument"])

    def test_no_reason_when_score_zero(self):
        item = _make_item(id="1", priority_weight=3)
        result = self._call(item)
        # argument is None or empty when no reasons
        self.assertTrue(result["argument"] is None or result["argument"] == "")


# ---------------------------------------------------------------------------
# evaluate_lens_leverage
# ---------------------------------------------------------------------------

class TestEvaluateLensLeverage(unittest.TestCase):

    def test_no_blocks_returns_zero(self):
        item = _make_item(id="1")
        result = evaluate_lens_leverage(item, {}, {})
        self.assertEqual(result["score"], 0.0)
        self.assertIsNone(result["argument"])

    def test_direct_blocks_adds_score(self):
        item = _make_item(id="1")
        blocks_map = {"1": ["2", "3"]}
        result = evaluate_lens_leverage(item, {}, blocks_map)
        self.assertGreater(result["score"], 0)
        self.assertIn("2", result["argument"])

    def test_cascade_adds_extra_score(self):
        # 1 -> 2 -> 3 (cascade of 2, direct blocks 1)
        item = _make_item(id="1")
        blocks_map = {"1": ["2"], "2": ["3"]}
        result = evaluate_lens_leverage(item, {}, blocks_map)
        self.assertGreater(result["score"], 3.0)  # direct (3.0) + cascade bonus

    def test_lens_name_is_leverage(self):
        item = _make_item(id="1")
        result = evaluate_lens_leverage(item, {}, {})
        self.assertEqual(result["lens"], "leverage")


# ---------------------------------------------------------------------------
# evaluate_lens_risk
# ---------------------------------------------------------------------------

class TestEvaluateLensRisk(unittest.TestCase):

    def _call(self, item, blocks_map=None, items_by_id=None):
        return evaluate_lens_risk(item, {}, blocks_map or {}, items_by_id or {})

    def test_no_risk_factors_returns_zero(self):
        item = _make_item(id="1")
        result = self._call(item)
        self.assertEqual(result["score"], 0.0)

    def test_blocking_others_adds_score(self):
        item = _make_item(id="1")
        blocked = _make_item(id="2", title="Blocked task")
        result = self._call(item, blocks_map={"1": ["2"]}, items_by_id={"1": item, "2": blocked})
        self.assertGreater(result["score"], 0)

    def test_high_reopen_count_adds_score(self):
        item = _make_item(id="1", reopen_count=3)
        result = self._call(item)
        self.assertGreater(result["score"], 0)
        self.assertIn("Reopened", result["argument"])

    def test_high_skip_count_adds_score(self):
        item = _make_item(id="1", skip_count=4)
        result = self._call(item)
        self.assertGreater(result["score"], 0)
        self.assertIn("Skipped", result["argument"])

    def test_reopen_below_threshold_not_penalized(self):
        item = _make_item(id="1", reopen_count=1)
        result = self._call(item)
        # reopen_count < 2: no reopen penalty
        self.assertEqual(result["score"], 0.0)

    def test_skip_below_threshold_not_penalized(self):
        item = _make_item(id="1", skip_count=2)
        result = self._call(item)
        # skip_count < 3: no skip penalty
        self.assertEqual(result["score"], 0.0)

    def test_lens_name_is_risk(self):
        item = _make_item(id="1")
        result = self._call(item)
        self.assertEqual(result["lens"], "risk")


# ---------------------------------------------------------------------------
# evaluate_lens_momentum
# ---------------------------------------------------------------------------

class TestEvaluateLensMomentum(unittest.TestCase):

    def _call(self, item, breakdown=None):
        return evaluate_lens_momentum(item, breakdown or {})

    def test_ready_status_adds_four_points(self):
        item = _make_item(id="1", status="ready")
        result = self._call(item)
        self.assertGreaterEqual(result["score"], 4.0)

    def test_refined_status_adds_two_points(self):
        item = _make_item(id="1", status="refined")
        result = self._call(item)
        self.assertGreaterEqual(result["score"], 2.0)

    def test_backlog_status_adds_zero_base(self):
        item = _make_item(id="1", status="backlog")
        result = self._call(item)
        # No status progression boost for backlog
        self.assertLess(result["score"], 2.0)

    def test_recent_activity_adds_score(self):
        item = _make_item(id="1", status="backlog")
        result = self._call(item, breakdown={"freshness": 0.8})
        self.assertGreater(result["score"], 0)
        self.assertIn("Recently active", result["argument"])

    def test_lane_history_adds_score(self):
        history = [{"by": "alice"}, {"by": "bob"}, {"by": "alice"}]
        item = _make_item(id="1", status="backlog", lane_history=history)
        result = self._call(item)
        self.assertGreater(result["score"], 0)
        self.assertIn("lane transitions", result["argument"])

    def test_lens_name_is_momentum(self):
        item = _make_item(id="1")
        result = self._call(item)
        self.assertEqual(result["lens"], "momentum")


# ---------------------------------------------------------------------------
# evaluate_lens_strategic
# ---------------------------------------------------------------------------

class TestEvaluateLensStrategic(unittest.TestCase):

    def _call(self, item, strategic_cfg=None):
        return evaluate_lens_strategic(item, strategic_cfg or DEFAULT_STRATEGIC)

    def test_no_focus_no_priority_returns_zero(self):
        item = _make_item(id="1", priority_weight=5)
        result = self._call(item)
        self.assertEqual(result["score"], 0.0)

    def test_high_priority_above_threshold_adds_score(self):
        item = _make_item(id="1", priority_weight=9)
        result = self._call(item)
        self.assertGreater(result["score"], 0)
        self.assertIn("9/10", result["argument"])

    def test_focus_tag_match_adds_score(self):
        item = _make_item(id="1", tags=["auth", "security"])
        cfg = {"current_focus": ["security", "performance"], "high_priority_threshold": 8}
        result = self._call(item, cfg)
        self.assertGreater(result["score"], 0)
        self.assertIn("security", result["argument"])

    def test_bug_category_with_stability_focus_adds_score(self):
        item = _make_item(id="1", category="bug", tags=[])
        cfg = {"current_focus": ["stability"], "high_priority_threshold": 8}
        result = self._call(item, cfg)
        self.assertGreater(result["score"], 0)
        self.assertIn("stability", result["argument"])

    def test_debt_category_with_quality_focus_adds_score(self):
        item = _make_item(id="1", category="debt", tags=[])
        cfg = {"current_focus": ["quality"], "high_priority_threshold": 8}
        result = self._call(item, cfg)
        self.assertGreater(result["score"], 0)

    def test_lens_name_is_strategic(self):
        item = _make_item(id="1")
        result = self._call(item)
        self.assertEqual(result["lens"], "strategic")


# ---------------------------------------------------------------------------
# evaluate_lens_agent_fit
# ---------------------------------------------------------------------------

class TestEvaluateLensAgentFit(unittest.TestCase):

    def _agents(self):
        return {
            "alice": {"skills": ["python", "backend"], "max_active": 3, "preferred_complexity": ["medium"]},
            "bob": {"skills": ["frontend", "css"], "max_active": 2, "preferred_complexity": ["low"]},
        }

    def test_returns_lens_name_agent_fit(self):
        item = _make_item(id="1")
        result = evaluate_lens_agent_fit(item, None, {}, {}, {})
        self.assertEqual(result["lens"], "agent_fit")

    def test_skill_match_gives_positive_score(self):
        item = _make_item(id="1", tags=["python"])
        result = evaluate_lens_agent_fit(item, "alice", self._agents(), {"1": item}, {})
        self.assertGreater(result["score"], 0)
        self.assertIn("python", result["argument"])

    def test_no_agents_returns_zero(self):
        item = _make_item(id="1", tags=["python"])
        result = evaluate_lens_agent_fit(item, None, {}, {"1": item}, {})
        self.assertEqual(result["score"], 0.0)

    def test_overloaded_agent_reduces_score(self):
        # alice has no tag match, no complexity match, and is at max capacity:
        # affinity = 0 (tags) + 0 (complexity) + 0 (links) - 5 (capacity) = -5
        # The threshold is <= -5, so "at max capacity" reason fires.
        agents = {
            "alice": {"skills": ["go"], "max_active": 3, "preferred_complexity": ["high"], "preferred_complexity": ["high"]},
        }
        item = _make_item(id="1", tags=["rust"], complexity="low")
        in_progress = {"alice": 3}  # at max_active
        result = evaluate_lens_agent_fit(item, "alice", agents, {"1": item}, in_progress)
        self.assertLessEqual(result["score"], 0)
        self.assertIn("capacity", result["argument"])

    def test_best_agent_selected_without_named_agent(self):
        item = _make_item(id="1", tags=["python"])
        result = evaluate_lens_agent_fit(item, None, self._agents(), {"1": item}, {})
        # alice has python skill, bob doesn't: alice should be recommended
        self.assertEqual(result["recommended_agent"], "alice")

    def test_complexity_preference_adds_affinity(self):
        item = _make_item(id="1", tags=["python"], complexity="medium")
        result = evaluate_lens_agent_fit(item, "alice", self._agents(), {"1": item}, {})
        self.assertIn("medium", result["argument"])

    def test_no_skill_match_gives_low_affinity_reason(self):
        # Use complexity that alice does NOT prefer to avoid any affinity boost
        agents = {
            "alice": {"skills": ["go"], "max_active": 3, "preferred_complexity": ["high"]},
        }
        item = _make_item(id="1", tags=["rust"], complexity="low")
        result = evaluate_lens_agent_fit(item, "alice", agents, {"1": item}, {})
        # affinity = 0 (no tags, no complexity match, no links) => "Low affinity for alice"
        self.assertIn("Low affinity", result["argument"])


# ---------------------------------------------------------------------------
# evaluate_tribunal
# ---------------------------------------------------------------------------

class TestEvaluateTribunal(unittest.TestCase):

    def _simple_data(self):
        items = [
            _make_item(id="1", title="Top item", priority_weight=9, status="ready"),
            _make_item(id="2", title="Low item", priority_weight=2, status="ready"),
        ]
        return _make_data(items)

    def test_returns_picked_and_shadow_ranking(self):
        result = evaluate_tribunal(self._simple_data())
        self.assertIn("picked", result)
        self.assertIn("shadow_ranking", result)
        self.assertIn("lenses", result)
        self.assertIn("candidates_evaluated", result)

    def test_picked_has_required_fields(self):
        result = evaluate_tribunal(self._simple_data())
        picked = result["picked"]
        for key in ("item_id", "title", "status", "score", "tribunal_score",
                    "reasoning", "confidence", "recommended_model", "supporting_lenses"):
            self.assertIn(key, picked)

    def test_high_priority_item_is_picked(self):
        result = evaluate_tribunal(self._simple_data())
        self.assertEqual(result["picked"]["item_id"], "1")

    def test_no_candidates_returns_none_picked(self):
        # All items in-progress — none eligible
        items = [_make_item(id="1", status="in-progress")]
        result = evaluate_tribunal(_make_data(items))
        self.assertIsNone(result["picked"])

    def test_candidates_evaluated_count_correct(self):
        result = evaluate_tribunal(self._simple_data())
        self.assertEqual(result["candidates_evaluated"], 2)

    def test_shadow_ranking_excludes_winner(self):
        result = evaluate_tribunal(self._simple_data())
        winner_id = result["picked"]["item_id"]
        shadow_ids = [s["item_id"] for s in result["shadow_ranking"]]
        self.assertNotIn(winner_id, shadow_ids)

    def test_lenses_list_has_six_entries(self):
        result = evaluate_tribunal(self._simple_data())
        lens_names = {l["lens"] for l in result["lenses"]}
        self.assertEqual(lens_names, {"urgency", "leverage", "risk", "momentum", "strategic", "agent_fit"})

    def test_confidence_high_when_large_margin(self):
        # Huge gap between items: margin > 5 => high confidence
        items = [
            _make_item(id="1", priority_weight=10, status="ready", category="bug"),
            _make_item(id="2", priority_weight=1, status="ready"),
        ]
        result = evaluate_tribunal(_make_data(items))
        self.assertEqual(result["picked"]["confidence"], "high")

    def test_status_note_for_backlog_item(self):
        items = [_make_item(id="1", status="backlog", priority_weight=9)]
        result = evaluate_tribunal(_make_data(items))
        self.assertIn("refinement", result["picked"]["status_note"])

    def test_status_note_for_refined_item(self):
        items = [_make_item(id="1", status="refined", priority_weight=9)]
        result = evaluate_tribunal(_make_data(items))
        self.assertIn("ready", result["picked"]["status_note"])

    def test_agent_filter_uses_named_agent(self):
        agents = {"alice": {"skills": ["python"], "max_active": 3, "preferred_complexity": []}}
        items = [_make_item(id="1", tags=["python"], status="ready", priority_weight=8)]
        result = evaluate_tribunal(_make_data(items, agents=agents), agent="alice")
        self.assertIsNotNone(result["picked"])

    def test_strategic_focus_auto_inferred_from_tags(self):
        # When no strategic focus set, it should be inferred from high-priority item tags
        items = [
            _make_item(id="1", priority_weight=8, status="ready", tags=["auth", "security"]),
            _make_item(id="2", priority_weight=3, status="ready", tags=["ui"]),
        ]
        data = _make_data(items)  # no strategic config
        result = evaluate_tribunal(data)
        # Should complete without error and pick item 1
        self.assertEqual(result["picked"]["item_id"], "1")

    def test_single_candidate_wins_with_high_confidence(self):
        items = [_make_item(id="1", status="ready", priority_weight=5)]
        result = evaluate_tribunal(_make_data(items))
        self.assertEqual(result["picked"]["item_id"], "1")
        self.assertEqual(result["picked"]["confidence"], "high")


if __name__ == "__main__":
    unittest.main()
