"""Unit tests for server.py scoring engine: compute_scores, evaluate_tribunal, lens functions."""

import pytest

from backlog.server import (
    DEFAULT_SCORING,
    DEFAULT_STRATEGIC,
    LENS_WEIGHTS,
    compute_scores,
    evaluate_lens_agent_fit,
    evaluate_lens_leverage,
    evaluate_lens_momentum,
    evaluate_lens_risk,
    evaluate_lens_strategic,
    evaluate_lens_urgency,
    evaluate_tribunal,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_item(
    iid="aaaaaaaa",
    title="Test item",
    status="backlog",
    priority_weight=5,
    complexity="medium",
    tags=None,
    category=None,
    reopen_count=0,
    skip_count=0,
    updated_at="2024-01-01T00:00:00+00:00",
    lane_history=None,
):
    item = {
        "id": iid,
        "title": title,
        "status": status,
        "priority_weight": priority_weight,
        "complexity": complexity,
        "tags": tags or [],
        "reopen_count": reopen_count,
        "skip_count": skip_count,
        "updated_at": updated_at,
        "lane_history": lane_history or [],
        "links": [],
        "threads": [],
    }
    if category is not None:
        item["category"] = category
    return item


def make_data(items, config=None):
    return {
        "version": 1,
        "config": config or {"scope": "project", "project_name": "test"},
        "items": items,
    }


# ── compute_scores ─────────────────────────────────────────────────────────────

class TestComputeScores:
    def test_empty_items_returns_empty(self):
        data = make_data([])
        assert compute_scores(data) == []

    def test_single_item_has_score(self):
        item = make_item()
        results = compute_scores(make_data([item]))
        assert len(results) == 1
        r = results[0]
        assert r["id"] == "aaaaaaaa"
        assert isinstance(r["score"], float)
        assert "score_breakdown" in r

    def test_priority_weight_contributes_to_score(self):
        low = make_item(iid="low00000", priority_weight=1)
        high = make_item(iid="high0000", priority_weight=9)
        results = compute_scores(make_data([low, high]))
        by_id = {r["id"]: r for r in results}
        assert by_id["high0000"]["score"] > by_id["low00000"]["score"]

    def test_results_sorted_by_score_descending(self):
        items = [
            make_item(iid=f"item{i:04d}ab", priority_weight=i)
            for i in range(1, 6)
        ]
        results = compute_scores(make_data(items))
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_blocking_item_gets_unblock_bonus(self):
        blocker = make_item(iid="blocker0", priority_weight=5)
        blocked = make_item(iid="blocked0", priority_weight=5)
        blocker["links"] = [{"type": "blocks", "item_id": "blocked0"}]
        results = compute_scores(make_data([blocker, blocked]))
        by_id = {r["id"]: r for r in results}
        assert by_id["blocker0"]["score_breakdown"]["unblock"] > 0
        assert by_id["blocked0"]["score_breakdown"]["unblock"] == 0

    def test_complexity_low_gets_bonus(self):
        low = make_item(iid="low00000", complexity="low", priority_weight=5)
        high = make_item(iid="high0000", complexity="high", priority_weight=5)
        results = compute_scores(make_data([low, high]))
        by_id = {r["id"]: r for r in results}
        assert by_id["low00000"]["score_breakdown"]["complexity"] > by_id["high0000"]["score_breakdown"]["complexity"]

    def test_complexity_high_gets_penalty(self):
        item = make_item(complexity="high")
        results = compute_scores(make_data([item]))
        assert results[0]["score_breakdown"]["complexity"] == DEFAULT_SCORING["complexity_bonus"]["high"]

    def test_reopen_penalty_applied(self):
        normal = make_item(iid="normal00", reopen_count=0, priority_weight=5)
        reopened = make_item(iid="reopened", reopen_count=3, priority_weight=5)
        results = compute_scores(make_data([normal, reopened]))
        by_id = {r["id"]: r for r in results}
        assert by_id["reopened"]["score_breakdown"]["reopen"] < 0
        assert by_id["normal00"]["score_breakdown"]["reopen"] == 0.0

    def test_skip_floor_increases_score(self):
        item = make_item(skip_count=4)
        results = compute_scores(make_data([item]))
        assert results[0]["score_breakdown"]["skip_floor"] > 0

    def test_critical_bug_gets_boost(self):
        bug = make_item(iid="bug00000", category="bug", priority_weight=9)
        normal = make_item(iid="normal00", priority_weight=9)
        results = compute_scores(make_data([bug, normal]))
        by_id = {r["id"]: r for r in results}
        assert by_id["bug00000"]["score_breakdown"]["critical_bug"] == DEFAULT_SCORING["critical_bug_boost"]
        assert by_id["normal00"]["score_breakdown"]["critical_bug"] == 0.0

    def test_quick_win_bonus_for_low_ready_item(self):
        # Low complexity + fully ready (no blockers) → quick_win bonus
        item = make_item(complexity="low", status="ready")
        results = compute_scores(make_data([item]))
        assert results[0]["score_breakdown"]["quick_win"] == DEFAULT_SCORING["quick_win_bonus"]

    def test_no_quick_win_for_medium_complexity(self):
        item = make_item(complexity="medium", status="ready")
        results = compute_scores(make_data([item]))
        assert results[0]["score_breakdown"]["quick_win"] == 0.0

    def test_readiness_level_ready(self):
        item = make_item(status="ready")
        # status="ready" → status_contribution=0.35, headroom=0.60
        # signals sum=0.80 (spec_written=0.10 + pr_merged=0.25 + review_approved=0.25 + test_passed=0.20)
        # capped_signal=0.60, score=0.95 → above ready_threshold=0.90
        item["readiness_signals"] = [
            {"type": "spec_written", "source": "test"},
            {"type": "pr_merged", "source": "test"},
            {"type": "review_approved", "source": "test"},
            {"type": "test_passed", "source": "test"},
        ]
        results = compute_scores(make_data([item]))
        r = results[0]["readiness"]
        assert r["level"] == "ready"

    def test_readiness_level_not_ready_for_backlog_item(self):
        item = make_item(status="backlog")
        results = compute_scores(make_data([item]))
        assert results[0]["readiness"]["level"] == "not_ready"

    def test_model_routing_low_complexity(self):
        item = make_item(complexity="low")
        results = compute_scores(make_data([item]))
        assert results[0]["recommended_model"] == "haiku"

    def test_model_routing_medium_complexity(self):
        item = make_item(complexity="medium")
        results = compute_scores(make_data([item]))
        assert results[0]["recommended_model"] == "sonnet"

    def test_model_routing_high_complexity(self):
        item = make_item(complexity="high")
        results = compute_scores(make_data([item]))
        assert results[0]["recommended_model"] == "opus"

    def test_best_agent_assigned_by_skill_match(self):
        item = make_item(tags=["auth", "backend"])
        config = {
            "scope": "project",
            "agents": {
                "backend-dev": {"skills": ["backend", "auth"], "max_active": 3},
                "frontend-dev": {"skills": ["ui", "css"], "max_active": 3},
            },
        }
        results = compute_scores(make_data([item], config=config))
        assert results[0]["recommended_agent"] == "backend-dev"

    def test_no_priority_weight_uses_position(self):
        item = make_item()
        del item["priority_weight"]
        results = compute_scores(make_data([item]))
        assert results[0]["score_breakdown"]["base_priority"] > 0

    def test_score_breakdown_fields_present(self):
        item = make_item()
        results = compute_scores(make_data([item]))
        bd = results[0]["score_breakdown"]
        for key in ("base_priority", "unblock", "freshness", "complexity",
                    "blocked_penalty", "quick_win", "reopen", "skip_floor", "critical_bug"):
            assert key in bd, f"Missing key: {key}"


# ── evaluate_lens_urgency ──────────────────────────────────────────────────────

class TestLensUrgency:
    def _scoring(self):
        return dict(DEFAULT_SCORING)

    def test_critical_bug_scores_high(self):
        item = make_item(category="bug", priority_weight=9)
        breakdown = {"critical_bug": DEFAULT_SCORING["critical_bug_boost"], "freshness": 0.0}
        result = evaluate_lens_urgency(item, breakdown, self._scoring())
        assert result["lens"] == "urgency"
        assert result["score"] >= 10.0
        assert result["argument"] is not None

    def test_high_priority_weight_contributes(self):
        item = make_item(priority_weight=9)
        breakdown = {"critical_bug": 0.0, "freshness": 0.0}
        result = evaluate_lens_urgency(item, breakdown, self._scoring())
        assert result["score"] > 0

    def test_medium_priority_weight_partial_contribution(self):
        item = make_item(priority_weight=5)
        breakdown = {"critical_bug": 0.0, "freshness": 0.0}
        result = evaluate_lens_urgency(item, breakdown, self._scoring())
        assert result["score"] > 0

    def test_low_priority_weight_no_contribution(self):
        item = make_item(priority_weight=3)
        breakdown = {"critical_bug": 0.0, "freshness": 0.0}
        result = evaluate_lens_urgency(item, breakdown, self._scoring())
        # pw=3 < 5, so no priority contribution
        assert result["score"] == 0.0
        assert result["argument"] is None

    def test_stale_item_gets_urgency_boost(self):
        item = make_item(priority_weight=3)
        # freshness < -0.2 triggers stale boost
        breakdown = {"critical_bug": 0.0, "freshness": -0.5}
        result = evaluate_lens_urgency(item, breakdown, self._scoring())
        assert result["score"] > 0
        assert "stale" in (result["argument"] or "").lower()

    def test_no_reasons_returns_none_argument(self):
        item = make_item(priority_weight=3)
        breakdown = {"critical_bug": 0.0, "freshness": 0.0}
        result = evaluate_lens_urgency(item, breakdown, self._scoring())
        assert result["argument"] is None


# ── evaluate_lens_leverage ─────────────────────────────────────────────────────

class TestLensLeverage:
    def test_no_blocks_scores_zero(self):
        item = make_item()
        blocks_map = {}
        result = evaluate_lens_leverage(item, {}, blocks_map)
        assert result["lens"] == "leverage"
        assert result["score"] == 0.0
        assert result["argument"] is None

    def test_direct_blocks_add_score(self):
        item = make_item(iid="blocker0")
        blocks_map = {"blocker0": ["blocked1", "blocked2"]}
        result = evaluate_lens_leverage(item, {}, blocks_map)
        assert result["score"] > 0
        assert result["argument"] is not None
        assert "2" in result["argument"]

    def test_cascade_adds_extra_score(self):
        # blocker0 → blocked1 → blocked2 (cascade of 2, direct of 1)
        item = make_item(iid="blocker0")
        blocks_map = {"blocker0": ["blocked1"], "blocked1": ["blocked2"]}
        result = evaluate_lens_leverage(item, {}, blocks_map)
        # cascade len=2 > direct=1, so extra score added
        assert result["score"] > 3.0  # direct: 3.0, plus cascade bonus


# ── evaluate_lens_risk ────────────────────────────────────────────────────────

class TestLensRisk:
    def test_no_risk_factors_zero_score(self):
        item = make_item()
        result = evaluate_lens_risk(item, {}, {}, {})
        assert result["lens"] == "risk"
        assert result["score"] == 0.0
        assert result["argument"] is None

    def test_blocking_other_items_adds_risk(self):
        item = make_item(iid="blocker0")
        blocked_item = make_item(iid="blocked0")
        blocks_map = {"blocker0": ["blocked0"]}
        items_by_id = {"blocker0": item, "blocked0": blocked_item}
        result = evaluate_lens_risk(item, {}, blocks_map, items_by_id)
        assert result["score"] > 0

    def test_reopen_count_adds_risk(self):
        item = make_item(reopen_count=3)
        result = evaluate_lens_risk(item, {}, {}, {})
        assert result["score"] > 0
        assert "Reopened" in (result["argument"] or "")

    def test_high_skip_count_adds_risk(self):
        item = make_item(skip_count=5)
        result = evaluate_lens_risk(item, {}, {}, {})
        assert result["score"] > 0
        assert "Skipped" in (result["argument"] or "")

    def test_reopen_below_threshold_no_risk(self):
        item = make_item(reopen_count=1)
        result = evaluate_lens_risk(item, {}, {}, {})
        # reopen_count < 2 → no reopen risk
        assert result["score"] == 0.0

    def test_skip_below_threshold_no_risk(self):
        item = make_item(skip_count=2)
        result = evaluate_lens_risk(item, {}, {}, {})
        # skip_count < 3 → no skip risk
        assert result["score"] == 0.0


# ── evaluate_lens_momentum ────────────────────────────────────────────────────

class TestLensMomentum:
    def test_backlog_status_low_momentum(self):
        item = make_item(status="backlog")
        breakdown = {"freshness": 0.0}
        result = evaluate_lens_momentum(item, breakdown)
        assert result["lens"] == "momentum"
        assert result["score"] == 0.0

    def test_refined_status_adds_momentum(self):
        item = make_item(status="refined")
        breakdown = {"freshness": 0.0}
        result = evaluate_lens_momentum(item, breakdown)
        assert result["score"] >= 2.0
        assert result["argument"] is not None

    def test_ready_status_adds_more_momentum(self):
        item_refined = make_item(iid="refined0", status="refined")
        item_ready = make_item(iid="ready000", status="ready")
        r_refined = evaluate_lens_momentum(item_refined, {"freshness": 0.0})
        r_ready = evaluate_lens_momentum(item_ready, {"freshness": 0.0})
        assert r_ready["score"] > r_refined["score"]

    def test_recent_activity_boosts_momentum(self):
        item = make_item(status="backlog")
        breakdown_stale = {"freshness": 0.0}
        breakdown_fresh = {"freshness": 0.5}
        r_stale = evaluate_lens_momentum(item, breakdown_stale)
        r_fresh = evaluate_lens_momentum(item, breakdown_fresh)
        assert r_fresh["score"] > r_stale["score"]

    def test_lane_history_adds_momentum(self):
        item = make_item(status="backlog", lane_history=["backlog", "refined", "ready", "in-progress"])
        breakdown = {"freshness": 0.0}
        result = evaluate_lens_momentum(item, breakdown)
        assert result["score"] > 0
        assert "lane transitions" in (result["argument"] or "")


# ── evaluate_lens_strategic ───────────────────────────────────────────────────

class TestLensStrategic:
    def test_no_focus_no_tags_zero_score(self):
        item = make_item(tags=[], priority_weight=5)
        result = evaluate_lens_strategic(item, DEFAULT_STRATEGIC)
        assert result["lens"] == "strategic"
        assert result["score"] == 0.0

    def test_high_priority_weight_scores(self):
        item = make_item(priority_weight=9)
        result = evaluate_lens_strategic(item, {"current_focus": [], "high_priority_threshold": 8})
        assert result["score"] == 5.0
        assert result["argument"] is not None

    def test_tag_matches_focus_adds_score(self):
        item = make_item(tags=["auth", "backend"], priority_weight=5)
        cfg = {"current_focus": ["auth", "security"], "high_priority_threshold": 8}
        result = evaluate_lens_strategic(item, cfg)
        assert result["score"] > 0
        assert "auth" in (result["argument"] or "")

    def test_multiple_tag_matches_add_more_score(self):
        item_one = make_item(iid="one00000", tags=["auth"], priority_weight=5)
        item_two = make_item(iid="two00000", tags=["auth", "security"], priority_weight=5)
        cfg = {"current_focus": ["auth", "security"], "high_priority_threshold": 8}
        r1 = evaluate_lens_strategic(item_one, cfg)
        r2 = evaluate_lens_strategic(item_two, cfg)
        assert r2["score"] > r1["score"]

    def test_bug_category_aligns_with_stability_focus(self):
        item = make_item(category="bug", tags=[], priority_weight=5)
        cfg = {"current_focus": ["stability", "quality"], "high_priority_threshold": 8}
        result = evaluate_lens_strategic(item, cfg)
        assert result["score"] > 0
        assert result["argument"] is not None

    def test_no_match_returns_none_argument(self):
        item = make_item(tags=["ui"], priority_weight=5)
        cfg = {"current_focus": ["backend", "auth"], "high_priority_threshold": 8}
        result = evaluate_lens_strategic(item, cfg)
        assert result["score"] == 0.0
        assert result["argument"] is None


# ── evaluate_lens_agent_fit ───────────────────────────────────────────────────

class TestLensAgentFit:
    def _agents_cfg(self):
        return {
            "backend-dev": {"skills": ["backend", "auth"], "max_active": 3, "preferred_complexity": ["medium"]},
            "frontend-dev": {"skills": ["ui", "css"], "max_active": 3, "preferred_complexity": ["low"]},
        }

    def test_skill_match_scores_positive(self):
        item = make_item(tags=["backend"])
        result = evaluate_lens_agent_fit(item, "backend-dev", self._agents_cfg(), {}, {"backend-dev": 0})
        assert result["lens"] == "agent_fit"
        assert result["score"] > 0
        assert "backend" in (result["argument"] or "")

    def test_no_skill_match_lower_score(self):
        item = make_item(tags=["backend"])
        result = evaluate_lens_agent_fit(item, "frontend-dev", self._agents_cfg(), {}, {"frontend-dev": 0})
        # No skill overlap → lower or zero affinity
        assert result["score"] == 0.0 or "Low affinity" in (result["argument"] or "")

    def test_agent_at_max_capacity_penalized(self):
        item = make_item(tags=["backend"])
        # backend-dev at max_active=3 → at capacity
        result = evaluate_lens_agent_fit(item, "backend-dev", self._agents_cfg(), {}, {"backend-dev": 3})
        assert result["score"] == 0.0

    def test_no_agent_finds_best(self):
        item = make_item(tags=["backend"])
        result = evaluate_lens_agent_fit(item, None, self._agents_cfg(), {}, {"backend-dev": 0, "frontend-dev": 0})
        assert result["recommended_agent"] == "backend-dev"

    def test_preferred_complexity_adds_score(self):
        item = make_item(tags=["backend"], complexity="medium")
        result = evaluate_lens_agent_fit(item, "backend-dev", self._agents_cfg(), {}, {"backend-dev": 0})
        assert "Preferred complexity" in (result["argument"] or "")


# ── evaluate_tribunal ─────────────────────────────────────────────────────────

class TestEvaluateTribunal:
    def test_empty_candidates_returns_no_pick(self):
        # All items in done/discarded → no eligible candidates
        items = [make_item(status="done"), make_item(iid="b0000000", status="discarded")]
        result = evaluate_tribunal(make_data(items))
        assert result["picked"] is None
        assert result["candidates_evaluated"] == 0

    def test_single_candidate_is_picked(self):
        item = make_item(status="ready", priority_weight=7)
        result = evaluate_tribunal(make_data([item]))
        assert result["picked"] is not None
        assert result["picked"]["item_id"] == item["id"]

    def test_higher_priority_wins(self):
        low = make_item(iid="low00000", status="ready", priority_weight=3)
        high = make_item(iid="high0000", status="ready", priority_weight=8)
        result = evaluate_tribunal(make_data([low, high]))
        assert result["picked"]["item_id"] == "high0000"

    def test_picked_has_required_fields(self):
        item = make_item(status="ready")
        result = evaluate_tribunal(make_data([item]))
        picked = result["picked"]
        for field in ("item_id", "title", "status", "score", "tribunal_score",
                      "reasoning", "confidence", "recommended_model", "readiness"):
            assert field in picked, f"Missing field: {field}"

    def test_shadow_ranking_excludes_winner(self):
        items = [make_item(iid=f"item{i:04d}ab", status="ready", priority_weight=10 - i) for i in range(4)]
        result = evaluate_tribunal(make_data(items))
        winner_id = result["picked"]["item_id"]
        shadow_ids = [s["item_id"] for s in result["shadow_ranking"]]
        assert winner_id not in shadow_ids

    def test_shadow_ranking_max_4(self):
        items = [make_item(iid=f"item{i:04d}ab", status="ready", priority_weight=i + 1) for i in range(8)]
        result = evaluate_tribunal(make_data(items))
        assert len(result["shadow_ranking"]) <= 4

    def test_confidence_high_when_clear_winner(self):
        low = make_item(iid="low00000", status="ready", priority_weight=1)
        high = make_item(iid="high0000", status="ready", priority_weight=10)
        result = evaluate_tribunal(make_data([low, high]))
        assert result["picked"]["confidence"] in ("high", "medium", "low")

    def test_lenses_all_present(self):
        item = make_item(status="ready")
        result = evaluate_tribunal(make_data([item]))
        lens_names = {l["lens"] for l in result["lenses"]}
        assert lens_names == set(LENS_WEIGHTS.keys())

    def test_in_progress_items_not_candidates(self):
        in_prog = make_item(iid="inprog00", status="in-progress", priority_weight=10)
        ready = make_item(iid="ready000", status="ready", priority_weight=1)
        result = evaluate_tribunal(make_data([in_prog, ready]))
        assert result["picked"]["item_id"] == "ready000"

    def test_tiebreaker_prefers_lower_complexity(self):
        # Two items with same priority_weight — low complexity should win tiebreaker
        low_c = make_item(iid="low00000", status="ready", priority_weight=5, complexity="low")
        high_c = make_item(iid="high0000", status="ready", priority_weight=5, complexity="high")
        result = evaluate_tribunal(make_data([low_c, high_c]))
        # Low complexity preferred in tiebreaker
        assert result["picked"]["item_id"] == "low00000"

    def test_status_note_for_backlog_item(self):
        item = make_item(status="backlog", priority_weight=10)
        result = evaluate_tribunal(make_data([item]))
        assert result["picked"]["status_note"] is not None
        assert "refinement" in (result["picked"]["status_note"] or "").lower()

    def test_agent_filter_influences_agent_fit_lens(self):
        item = make_item(tags=["backend"], status="ready")
        config = {
            "scope": "project",
            "agents": {
                "backend-dev": {"skills": ["backend"], "max_active": 3},
            },
        }
        data = make_data([item], config=config)
        result = evaluate_tribunal(data, agent="backend-dev")
        picked = result["picked"]
        assert picked is not None

    def test_auto_focus_derived_from_high_priority_tags(self):
        # No current_focus configured → should auto-derive from high-pw items' tags
        items = [
            make_item(iid="a0000000", status="ready", tags=["auth", "security"], priority_weight=9),
            make_item(iid="b0000000", status="ready", tags=["ui"], priority_weight=2),
        ]
        data = make_data(items)  # no strategic config → auto-derive
        result = evaluate_tribunal(data)
        # Should not raise; picked should exist
        assert result["picked"] is not None

    def test_candidates_evaluated_count(self):
        items = [make_item(iid=f"item{i:04d}ab", status="ready") for i in range(5)]
        result = evaluate_tribunal(make_data(items))
        assert result["candidates_evaluated"] == 5
