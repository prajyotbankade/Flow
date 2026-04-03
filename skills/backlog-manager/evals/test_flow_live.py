import os
import pytest
import requests
from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from eval_flow_skill import run_flow, run_flow_tribunal, run_flow_graph, run_flow_policy, EVAL_LLM, get_last_context

requires_anthropic = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set — policy engine LLM tests skipped",
)

BACKLOG_URL = "http://localhost:8089/api/backlog"

# Judge model — OpenAI for accuracy, Ollama for free local runs
if EVAL_LLM == "openai":
    judge_model = "gpt-4o-mini"
else:
    from deepeval.models import OllamaModel
    judge_model = OllamaModel(model="qwen2.5-coder:14b")

relevancy_metric = AnswerRelevancyMetric(threshold=0.6, model=judge_model)

# ---------------------------------------------------------------------------
# Scenarios
# Each scenario has:
#   input    — a precise natural language question (what the evaluator judges against)
#   items    — the backlog state to seed
#   expected — the ideal answer (used as ground truth for GEval)
#   criteria — laser-focused correctness criterion for GEval
# ---------------------------------------------------------------------------
SCENARIOS = [
    {
        "name": "Impact_vs_Unblock",
        "input": (
            "Two tasks exist: 'Low Value' (base_priority 1, no dependencies) and "
            "'High Value' (base_priority 10, no dependencies). Neither blocks the other. "
            "Which should be prioritized based on the score_breakdown, and what is the decisive factor?"
        ),
        "items": [
            {"id": "A", "title": "Low Value", "priority_weight": 1, "status": "ready"},
            {"id": "B", "title": "High Value", "priority_weight": 10, "status": "ready"},
        ],
        "expected": (
            "Recommend 'High Value': its base_priority of 10.0 dominates the scoring queue, "
            "outpacing 'Low Value' (base_priority 1.0) with no other active scoring signals."
        ),
        "criteria": (
            "The response recommends 'High Value' and references base_priority or priority weight "
            "as the decisive factor. It must NOT mention unblocking, bug boost, or blocked_penalty "
            "since none of those fields are non-zero for either task."
        ),
    },
    {
        "name": "Critical_Bug_Boost",
        "input": (
            "A critical bug 'System Crash' (BUG_911, priority 9, category=bug) competes "
            "with 'Feature Work' (TASK_1, priority 8, category=feature). "
            "Does the bug classification trigger a scoring boost that makes BUG_911 the top pick?"
        ),
        "items": [
            {"id": "TASK_1", "title": "Feature Work", "priority_weight": 8, "category": "feature"},
            {"id": "BUG_911", "title": "System Crash", "priority_weight": 9, "category": "bug"},
        ],
        "expected": (
            "Recommend 'System Crash': its critical_bug score_breakdown field gives it "
            "a decisive boost over 'Feature Work', which has no bug classification."
        ),
        "criteria": (
            "The response recommends 'System Crash' or BUG_911 and references critical_bug "
            "or a bug-category boost as the key scoring factor from the breakdown."
        ),
    },
    {
        "name": "Unblocker_Strategy",
        "input": (
            "'Root Task' (priority 2) has a blocking link to 'Blocked Task' (priority 1). "
            "Using the full score_breakdown — including the unblock multiplier and any "
            "blocked_penalty — which task should be the top pick and why?"
        ),
        "items": [
            {
                "id": "BLOCKER",
                "title": "Root Task",
                "priority_weight": 2,
                "links": [{"type": "blocks", "item_id": "DEPENDENT"}],
            },
            {"id": "DEPENDENT", "title": "Blocked Task", "priority_weight": 1},
        ],
        "expected": (
            "Recommend 'Root Task': its unblock score of +2.0 lifts its total to 4.0, "
            "while 'Blocked Task' is suppressed to -1.85 by a readiness-scaled blocked_penalty "
            "of -2.85 (Root Task is at 5% readiness — backlog status, no artifact signals, "
            "so 95% of the max -3.0 penalty applies)."
        ),
        "criteria": (
            "The response recommends 'Root Task' and references 'unblock' (the score_breakdown field) "
            "as the reason it outranks 'Blocked Task'. The word 'unblock' must appear in the explanation. "
            "Bonus if it notes the blocked_penalty applied to 'Blocked Task'."
        ),
    },
    {
        "name": "Readiness_Partial_Block",
        "input": (
            "'Feature Work' (priority 5, ready) is blocked by 'Auth Integration' (priority 3, "
            "in-progress, PR already merged). How does Auth Integration's readiness affect "
            "Feature Work's blocked_penalty and score? Is Feature Work startable now?"
        ),
        "items": [
            {
                "id": "AUTH",
                "title": "Auth Integration",
                "priority_weight": 3,
                "status": "in-progress",
                "complexity": "medium",
                "links": [{"type": "blocks", "item_id": "FEAT", "reason": "auth must land first"}],
                "readiness_signals": [
                    {"type": "pr_merged", "source": "worker-a", "timestamp": "2026-03-26T10:00:00Z"}
                ],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                    {"lane": "ready", "at": "2026-03-22T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-26T10:00:00Z",
                "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "FEAT",
                "title": "Feature Work",
                "priority_weight": 5,
                "status": "ready",
                "complexity": "medium",
                "links": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-18T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-19T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-20T10:00:00Z",
                "created_at": "2026-03-18T10:00:00Z",
            },
        ],
        "expected": (
            "Auth Integration is at 75% readiness (50% in-progress baseline + 25% from PR merged). "
            "Feature Work's blocked_penalty is -0.75 — only 25% of the max -3.0, because its "
            "blocker is 75% ready. Feature Work is startable with known risk (blocker above 70% "
            "readiness threshold)."
        ),
        "criteria": (
            "The response identifies Auth Integration's readiness as approximately 75% "
            "(in-progress + pr_merged signal). Feature Work's blocked_penalty must be described "
            "as partial or reduced (around -0.75), not the full -3.0. "
            "The response must indicate Feature Work is startable or above the 70% threshold, "
            "not fully blocked."
        ),
    },
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def restore_backlog():
    """Capture backlog state before each test and restore it after."""
    snapshot_resp = requests.get(BACKLOG_URL)
    snapshot_resp.raise_for_status()
    original_items = snapshot_resp.json().get("items", [])

    yield

    current_resp = requests.get(BACKLOG_URL)
    current_resp.raise_for_status()
    current_version = current_resp.json().get("version", 0)
    requests.put(
        BACKLOG_URL,
        json={"version": current_version, "items": original_items},
    ).raise_for_status()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@pytest.mark.timeout(300)
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["name"] for s in SCENARIOS])
def test_flow_skill_live(scenario):
    # 1. SETUP — get current version
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)

    # 2. SEED — inject scenario items
    requests.put(
        BACKLOG_URL,
        json={"version": version, "items": scenario["items"]},
    ).raise_for_status()

    # 3. EXECUTE — call the skill with a real natural-language question
    actual = run_flow(scenario["input"])

    # 4. EVALUATE — dual-metric: relevancy + strategic correctness
    test_case = LLMTestCase(
        input=scenario["input"],
        actual_output=actual,
        expected_output=scenario["expected"],
    )

    correctness = GEval(
        name="Strategic Correctness",
        criteria=scenario["criteria"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
        model=judge_model,
    )

    assert_test(test_case, [relevancy_metric, correctness])


# ---------------------------------------------------------------------------
# Tribunal Justification Scenarios
# These test the /api/recommend tribunal endpoint via run_flow_tribunal.
# Each scenario validates that lens arguments and counterfactuals are cited.
# ---------------------------------------------------------------------------
TRIBUNAL_SCENARIOS = [
    {
        "name": "Tribunal_Leverage_Wins",
        "input": (
            "The tribunal has evaluated all items. Which item should I work on next, "
            "and what lenses drove the decision? Explain why the alternatives were not picked."
        ),
        "items": [
            {
                "id": "BLOCKER",
                "title": "Fix auth crash",
                "priority_weight": 3,
                "status": "ready",
                "category": "bug",
                "tags": ["auth", "backend"],
                "links": [
                    {"type": "blocks", "item_id": "DEP1", "reason": "blocks session work"},
                    {"type": "blocks", "item_id": "DEP2", "reason": "blocks SSO"},
                ],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "complexity": "medium",
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-26T10:00:00Z",
                "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "DEP1",
                "title": "Session refactor",
                "priority_weight": 5,
                "status": "ready",
                "tags": ["auth"],
                "links": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-18T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-19T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "complexity": "high",
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-25T10:00:00Z",
                "created_at": "2026-03-18T10:00:00Z",
            },
            {
                "id": "DEP2",
                "title": "SSO integration",
                "priority_weight": 4,
                "status": "refined",
                "tags": ["auth", "frontend"],
                "links": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-19T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "complexity": "high",
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-24T10:00:00Z",
                "created_at": "2026-03-19T10:00:00Z",
            },
        ],
        "expected": (
            "Recommend 'Fix auth crash': the leverage lens is decisive — it directly "
            "unblocks 2 items (Session refactor and SSO integration). Session refactor "
            "was not picked despite higher priority because it is blocked by the auth crash. "
            "SSO integration lost because it is not in ready status."
        ),
        "criteria": (
            "The response recommends 'Fix auth crash' and cites the leverage lens "
            "(unblocking 2 items) as a key factor. It must explain why 'Session refactor' "
            "was not picked (blocked by auth crash or lower tribunal score) and why "
            "'SSO integration' was not picked (not ready or lower tribunal score). "
            "The response must reference specific tribunal concepts like lenses, "
            "counterfactuals, or confidence."
        ),
    },
    {
        "name": "Tribunal_Counterfactual_Reasoning",
        "input": (
            "The tribunal picked an item. I want to understand: why wasn't "
            "'Dashboard polish' picked instead? What specific lens did it lose on?"
        ),
        "items": [
            {
                "id": "CRITICAL",
                "title": "Payment API 500 errors",
                "priority_weight": 10,
                "status": "ready",
                "category": "bug",
                "tags": ["payment", "backend"],
                "links": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-25T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-26T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "complexity": "medium",
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-27T10:00:00Z",
                "created_at": "2026-03-25T10:00:00Z",
            },
            {
                "id": "POLISH",
                "title": "Dashboard polish",
                "priority_weight": 6,
                "status": "ready",
                "category": "feature",
                "tags": ["frontend", "css"],
                "links": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-22T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "complexity": "low",
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-24T10:00:00Z",
                "created_at": "2026-03-20T10:00:00Z",
            },
        ],
        "expected": (
            "'Dashboard polish' was not picked because it lost decisively on the urgency lens. "
            "'Payment API 500 errors' is a critical bug with priority 10/10 and the urgency lens "
            "weighted it heavily. Dashboard polish has no urgency signals — lower priority, "
            "no critical bug classification. The confidence is high due to the large margin."
        ),
        "criteria": (
            "The response explains why 'Dashboard polish' lost, citing the urgency lens "
            "or the critical bug classification of the winner as the key differentiator. "
            "It must reference the shadow ranking, counterfactual reasoning, or the "
            "specific lost_reason/lost_on_lens from the tribunal output. "
            "It should NOT just say 'lower score' — it must explain WHY."
        ),
    },
    {
        "name": "Tribunal_Confidence_Low",
        "input": (
            "What does the tribunal recommend and how confident is it? "
            "Are the top two items close enough that I should consider both?"
        ),
        "items": [
            {
                "id": "TASK_A",
                "title": "Add caching layer",
                "priority_weight": 6,
                "status": "ready",
                "category": "feature",
                "tags": ["backend", "performance"],
                "links": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-22T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "complexity": "medium",
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-26T10:00:00Z",
                "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "TASK_B",
                "title": "Add logging middleware",
                "priority_weight": 6,
                "status": "ready",
                "category": "feature",
                "tags": ["backend", "observability"],
                "links": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-22T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0,
                "complexity": "medium",
                "reopen_count": 0,
                "skip_count": 0,
                "threads": [],
                "updated_at": "2026-03-26T10:00:00Z",
                "created_at": "2026-03-20T10:00:00Z",
            },
        ],
        "expected": (
            "The tribunal's confidence should be low because both items have nearly identical "
            "scores — same priority, same status, same complexity, no blocking relationships. "
            "The margin between them is minimal. The user should consider both options and "
            "decide based on personal preference or strategic alignment."
        ),
        "criteria": (
            "The response mentions low or medium confidence, or that the margin is small. "
            "It acknowledges that both items are close and the pick is not definitive. "
            "It must NOT present the pick as a clear, high-confidence winner. "
            "It should suggest the user may want to choose based on context not captured "
            "in the scoring data."
        ),
    },
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "scenario", TRIBUNAL_SCENARIOS, ids=[s["name"] for s in TRIBUNAL_SCENARIOS]
)
def test_tribunal_justification(scenario):
    # 1. SETUP
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)

    # 2. SEED
    requests.put(
        BACKLOG_URL,
        json={"version": version, "items": scenario["items"]},
    ).raise_for_status()

    # 3. EXECUTE — call the tribunal-aware skill
    actual = run_flow_tribunal(scenario["input"])

    # 4. EVALUATE
    test_case = LLMTestCase(
        input=scenario["input"],
        actual_output=actual,
        expected_output=scenario["expected"],
    )

    correctness = GEval(
        name="Tribunal Justification Quality",
        criteria=scenario["criteria"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
        model=judge_model,
    )

    assert_test(test_case, [relevancy_metric, correctness])


# ---------------------------------------------------------------------------
# Phase 3: Multi-Agent Coordination — Direct API Tests
# These test /api/graph and /api/pulse response structure directly (no LLM).
# ---------------------------------------------------------------------------

GRAPH_URL  = "http://localhost:8089/api/graph"
PULSE_URL  = "http://localhost:8089/api/pulse"


def _seed_items(items, config=None):
    """Seed the backlog with the given items (and optional config) and return the new version."""
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    data = resp.json()
    version = data.get("version", 0)
    payload = {"version": version, "items": items}
    if config is not None:
        payload["config"] = config
    requests.put(BACKLOG_URL, json=payload).raise_for_status()
    return version + 1


def test_graph_cascade_correctness():
    """A→B→C chain: cascade_count for A must be 2, B must be 1, C must be 0."""
    items = [
        {
            "id": "CA", "title": "Root blocker", "status": "ready",
            "links": [{"type": "blocks", "item_id": "CB", "reason": "blocks B"}],
            "lane_history": [{"lane": "backlog", "at": "2026-03-01T00:00:00Z", "by": "u"},
                             {"lane": "refined", "at": "2026-03-02T00:00:00Z", "by": "u"}],
            "gate_from": 0, "threads": [], "priority_weight": 5, "tags": [],
            "reopen_count": 0, "skip_count": 0,
            "created_at": "2026-03-01T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
        },
        {
            "id": "CB", "title": "Middle item", "status": "backlog",
            "links": [{"type": "blocks", "item_id": "CC", "reason": "blocks C"}],
            "lane_history": [], "gate_from": 0, "threads": [], "priority_weight": 4, "tags": [],
            "reopen_count": 0, "skip_count": 0,
            "created_at": "2026-03-01T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
        },
        {
            "id": "CC", "title": "Leaf item", "status": "backlog",
            "links": [], "lane_history": [], "gate_from": 0, "threads": [],
            "priority_weight": 3, "tags": [], "reopen_count": 0, "skip_count": 0,
            "created_at": "2026-03-01T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
        },
    ]
    _seed_items(items)

    resp = requests.get(GRAPH_URL)
    assert resp.status_code == 200
    graph = resp.json()

    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    assert nodes_by_id["CA"]["cascade_count"] == 2, "Root must cascade to 2 items"
    assert nodes_by_id["CB"]["cascade_count"] == 1, "Middle must cascade to 1 item"
    assert nodes_by_id["CC"]["cascade_count"] == 0, "Leaf must cascade to 0 items"
    assert "CA" in graph["critical_path"], "Root blocker must be on critical path"
    assert nodes_by_id["CA"]["is_critical_path"] is True


def test_graph_conflict_detection():
    """Two in-progress items with the same tag on different agents → one conflict."""
    items = [
        {
            "id": "CF1", "title": "Auth refactor", "status": "in-progress",
            "assigned_to": "agent-alpha", "tags": ["auth", "backend"],
            "links": [], "lane_history": [], "gate_from": 0, "threads": [],
            "priority_weight": 7, "reopen_count": 0, "skip_count": 0,
            "created_at": "2026-03-01T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
        },
        {
            "id": "CF2", "title": "Auth middleware", "status": "in-progress",
            "assigned_to": "agent-beta", "tags": ["auth", "security"],
            "links": [], "lane_history": [], "gate_from": 0, "threads": [],
            "priority_weight": 6, "reopen_count": 0, "skip_count": 0,
            "created_at": "2026-03-01T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
        },
    ]
    _seed_items(items)

    resp = requests.get(GRAPH_URL)
    assert resp.status_code == 200
    graph = resp.json()

    assert len(graph["conflicts"]) >= 1, "Expected at least one conflict for shared 'auth' tag"
    conflict = graph["conflicts"][0]
    assert conflict["type"] == "tag_overlap"
    assert "auth" in conflict["shared_tags"]
    assert set(conflict["items"]) == {"CF1", "CF2"}


def test_graph_no_conflict_same_agent():
    """Two in-progress items with the same tag on the SAME agent → no conflict."""
    items = [
        {
            "id": "NC1", "title": "Task A", "status": "in-progress",
            "assigned_to": "agent-alpha", "tags": ["auth"],
            "links": [], "lane_history": [], "gate_from": 0, "threads": [],
            "priority_weight": 5, "reopen_count": 0, "skip_count": 0,
            "created_at": "2026-03-01T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
        },
        {
            "id": "NC2", "title": "Task B", "status": "in-progress",
            "assigned_to": "agent-alpha", "tags": ["auth"],
            "links": [], "lane_history": [], "gate_from": 0, "threads": [],
            "priority_weight": 4, "reopen_count": 0, "skip_count": 0,
            "created_at": "2026-03-01T00:00:00Z", "updated_at": "2026-03-01T00:00:00Z",
        },
    ]
    _seed_items(items)

    resp = requests.get(GRAPH_URL)
    assert resp.status_code == 200
    graph = resp.json()

    assert graph["conflicts"] == [], "Same-agent items must not produce conflicts"


def test_pulse_payload_completeness():
    """GET /api/pulse must return all required keys."""
    resp = requests.get(PULSE_URL)
    assert resp.status_code == 200
    pulse = resp.json()

    for key in ("agent", "recommendation", "startable_items", "conflicts",
                "rebalancing", "active_agents", "generated_at", "policies"):
        assert key in pulse, f"Pulse missing key: {key}"

    assert isinstance(pulse["startable_items"], list)
    assert isinstance(pulse["active_agents"], list)
    assert isinstance(pulse["conflicts"], list)

    # Validate policies sub-structure
    policies = pulse["policies"]
    assert isinstance(policies, dict), "policies must be a dict"
    for pkey in ("active_count", "recent_fires", "notifications",
                 "influences_on_pick", "stale_warnings"):
        assert pkey in policies, f"policies missing key: {pkey}"
    assert isinstance(policies["active_count"], int)
    assert isinstance(policies["recent_fires"], list)
    assert isinstance(policies["notifications"], list)
    assert isinstance(policies["influences_on_pick"], list)
    assert isinstance(policies["stale_warnings"], list)


def test_pulse_agent_filter():
    """GET /api/pulse?agent=X must return agent field set to X."""
    resp = requests.get(PULSE_URL + "?agent=test-agent")
    assert resp.status_code == 200
    pulse = resp.json()
    assert pulse["agent"] == "test-agent"


# ---------------------------------------------------------------------------
# Phase 3: LLM-based Graph Coordination Scenarios
# ---------------------------------------------------------------------------

GRAPH_SCENARIOS = [
    {
        "name": "Critical_Path_Identification",
        "input": (
            "Which item is on the critical path and why? "
            "How many items would be unblocked if it completes?"
        ),
        "items": [
            {
                "id": "CP1", "title": "Fix DB schema", "status": "ready",
                "priority_weight": 5, "tags": ["database"],
                "links": [
                    {"type": "blocks", "item_id": "CP2", "reason": "needs schema"},
                    {"type": "blocks", "item_id": "CP3", "reason": "needs schema"},
                ],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0, "complexity": "high", "reopen_count": 0, "skip_count": 0,
                "threads": [], "updated_at": "2026-03-26T10:00:00Z",
                "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CP2", "title": "Build user API", "status": "backlog",
                "priority_weight": 4, "tags": ["api"],
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "medium",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-25T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CP3", "title": "Build admin dashboard", "status": "backlog",
                "priority_weight": 3, "tags": ["frontend"],
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "medium",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-25T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
        ],
        "expected": (
            "The critical path item is 'Fix DB schema' with a cascade_count of 2. "
            "Completing it would unblock both 'Build user API' and 'Build admin dashboard'."
        ),
        "criteria": (
            "The response identifies 'Fix DB schema' as the critical path item. "
            "It mentions that completing it unblocks 2 items (or names both downstream items). "
            "It must NOT identify CP2 or CP3 as critical path — they have cascade_count=0. "
            "It must cite a cascade count or unblock count from the graph data."
        ),
    },
    {
        "name": "Conflict_Detection_Response",
        "input": (
            "Are there any coordination conflicts between agents right now? "
            "Which items are involved and what is the risk?"
        ),
        "items": [
            {
                "id": "CON1", "title": "Refactor auth module", "status": "in-progress",
                "assigned_to": "agent-alpha", "tags": ["auth", "backend"],
                "priority_weight": 7,
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "high",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CON2", "title": "Add auth rate limiting", "status": "in-progress",
                "assigned_to": "agent-beta", "tags": ["auth", "security"],
                "priority_weight": 6,
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "medium",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
        ],
        "expected": (
            "Yes, there is a conflict: 'Refactor auth module' (agent-alpha) and "
            "'Add auth rate limiting' (agent-beta) both touch the 'auth' tag, "
            "creating a risk of merge conflicts or redundant changes to the auth layer."
        ),
        "criteria": (
            "The response confirms that a conflict exists. "
            "It names both items or both agents (agent-alpha, agent-beta). "
            "It identifies 'auth' as the shared tag or area. "
            "It must NOT say there are no conflicts."
        ),
    },
    {
        "name": "Pulse_Coordination_Context",
        "input": (
            "What should I work on next? Also tell me if any other agents are active "
            "and whether there are any coordination risks I should know about."
        ),
        "items": [
            {
                "id": "PUL1", "title": "Write API tests", "status": "ready",
                "priority_weight": 8, "tags": ["testing", "api"],
                "links": [], "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0, "complexity": "low",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
        ],
        "expected": (
            "Recommended work: 'Write API tests'. "
            "The pulse provides full coordination context including active agent loads "
            "and conflict status."
        ),
        "criteria": (
            "The response recommends a specific item to work on next. "
            "It references coordination context — either active agents, "
            "conflict status, or startable items from the pulse. "
            "It must NOT simply give a task recommendation "
            "while ignoring the coordination context parts of the question."
        ),
    },
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "scenario", GRAPH_SCENARIOS, ids=[s["name"] for s in GRAPH_SCENARIOS]
)
def test_graph_coordination(scenario):
    """Phase 3: LLM interprets graph + pulse data for coordination questions."""
    # 1. SETUP
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)

    # 2. SEED
    requests.put(
        BACKLOG_URL,
        json={"version": version, "items": scenario["items"]},
    ).raise_for_status()

    # 3. EXECUTE
    actual = run_flow_graph(scenario["input"])

    # 4. EVALUATE
    test_case = LLMTestCase(
        input=scenario["input"],
        actual_output=actual,
        expected_output=scenario["expected"],
    )

    correctness = GEval(
        name="Graph Coordination Quality",
        criteria=scenario["criteria"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
        model=judge_model,
    )

    assert_test(test_case, [relevancy_metric, correctness])


# ---------------------------------------------------------------------------
# Phase 4: Natural Language Rule Engine — Direct API Tests
# ---------------------------------------------------------------------------

POLICIES_URL   = "http://localhost:8089/api/policies"
POLICY_LOG_URL = "http://localhost:8089/api/policies/log"
POLICY_EVAL_URL = "http://localhost:8089/api/policies/evaluate"


def test_policy_list_structure():
    """GET /api/policies must return a policies array with required fields."""
    resp = requests.get(POLICIES_URL)
    assert resp.status_code == 200
    data = resp.json()

    assert "policies" in data
    assert isinstance(data["policies"], list)
    assert len(data["policies"]) > 0, "Default policies must be seeded"

    for policy in data["policies"]:
        for field in ("id", "name", "description", "priority", "active",
                      "created_at", "fire_count", "staleness_warnings"):
            assert field in policy, f"Policy missing field: {field}"
        assert isinstance(policy["active"], bool)
        assert isinstance(policy["priority"], int)
        assert isinstance(policy["fire_count"], int)
        assert isinstance(policy["staleness_warnings"], list)


def test_policy_crud_lifecycle():
    """POST creates, GET verifies, PUT updates, DELETE removes a policy."""
    create_resp = requests.post(POLICIES_URL, json={
        "name": "Test CRUD policy",
        "description": "If any item has skip_count >= 99, reprioritize it.",
        "priority": 3,
        "active": True,
    })
    assert create_resp.status_code == 201
    policy_id = create_resp.json()["policy"]["id"]
    assert policy_id

    ids = [p["id"] for p in requests.get(POLICIES_URL).json()["policies"]]
    assert policy_id in ids, "Newly created policy must appear in list"

    update_resp = requests.put(f"{POLICIES_URL}/{policy_id}", json={
        "name": "Updated CRUD policy",
        "active": False,
    })
    assert update_resp.status_code == 200
    assert update_resp.json()["status"] == "ok"

    updated = next(
        (p for p in requests.get(POLICIES_URL).json()["policies"] if p["id"] == policy_id),
        None,
    )
    assert updated is not None
    assert updated["name"] == "Updated CRUD policy"
    assert updated["active"] is False

    del_resp = requests.delete(f"{POLICIES_URL}/{policy_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "ok"

    final_ids = [p["id"] for p in requests.get(POLICIES_URL).json()["policies"]]
    assert policy_id not in final_ids, "Deleted policy must not appear in list"


def test_policy_log_structure():
    """GET /api/policies/log must return entries with required fields."""
    resp = requests.get(POLICY_LOG_URL)
    assert resp.status_code == 200
    data = resp.json()

    assert "entries" in data
    assert isinstance(data["entries"], list)

    if data["entries"]:
        entry = data["entries"][0]
        for field in ("id", "policy_id", "policy_name", "timestamp", "fired",
                      "reasoning", "actions_proposed", "actions_executed", "notifications"):
            assert field in entry, f"Log entry missing field: {field}"


def test_policy_log_limit_param():
    """GET /api/policies/log?limit=2 must return at most 2 entries."""
    resp = requests.get(f"{POLICY_LOG_URL}?limit=2")
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) <= 2


def test_policy_evaluate_structure():
    """GET /api/policies/evaluate must return required keys."""
    resp = requests.get(POLICY_EVAL_URL)
    assert resp.status_code == 200
    data = resp.json()

    for key in ("fires", "actions_executed", "notifications", "log_ids"):
        assert key in data, f"Evaluate result missing key: {key}"
    assert isinstance(data["fires"], int)
    assert isinstance(data["actions_executed"], int)
    assert isinstance(data["notifications"], list)
    assert isinstance(data["log_ids"], list)


@requires_anthropic
def test_policy_evaluate_fires_for_skipped_item():
    """
    A policy targeting skip_count >= 5 must fire when a matching item is present.
    """
    bl_resp = requests.get(BACKLOG_URL)
    bl_resp.raise_for_status()
    version = bl_resp.json().get("version", 0)

    items = [{
        "id": "SKIP1", "title": "Long-neglected task", "status": "ready",
        "priority_weight": 3, "skip_count": 7, "reopen_count": 0,
        "tags": [], "links": [], "threads": [],
        "lane_history": [
            {"lane": "backlog", "at": "2026-03-01T00:00:00Z", "by": "u"},
            {"lane": "refined", "at": "2026-03-02T00:00:00Z", "by": "u"},
        ],
        "gate_from": 0, "created_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-01T00:00:00Z",
    }]
    requests.put(BACKLOG_URL, json={"version": version, "items": items}).raise_for_status()

    create_resp = requests.post(POLICIES_URL, json={
        "name": "Force neglected: skip >= 5",
        "description": (
            "If an item has been skipped 5 or more times without being started, "
            "reprioritize it to priority_weight 8."
        ),
        "priority": 9,
        "active": True,
    })
    create_resp.raise_for_status()
    policy_id = create_resp.json()["policy"]["id"]

    try:
        eval_resp = requests.get(POLICY_EVAL_URL)
        assert eval_resp.status_code == 200

        log_entries = requests.get(f"{POLICY_LOG_URL}?limit=20").json()["entries"]
        policy_entries = [e for e in log_entries if e.get("policy_id") == policy_id]
        assert policy_entries, "Policy must appear in log after evaluation"

        fired_entries = [e for e in policy_entries if e.get("fired") is True]
        assert fired_entries, (
            "Policy must fire when item has skip_count=7. "
            f"Log entries: {policy_entries}"
        )
    finally:
        requests.delete(f"{POLICIES_URL}/{policy_id}")


# ---------------------------------------------------------------------------
# Phase 4: LLM-based Policy Evaluation Scenarios
# ---------------------------------------------------------------------------

POLICY_SCENARIOS = [
    {
        "name": "Rule_Authoring_Natural_Language",
        "input": (
            "I just added a rule: 'If any item has been skipped 5+ times, bump it "
            "to priority 8 to force a decision.' Did the rule engine evaluate this policy? "
            "What was its reasoning?"
        ),
        "policy_description": (
            "If an item has been skipped 5 or more times without being started, "
            "reprioritize it to priority_weight 8 to force a decision."
        ),
        "items": [
            {
                "id": "NEG1", "title": "Kept getting pushed aside", "status": "ready",
                "priority_weight": 2, "skip_count": 6, "reopen_count": 0,
                "tags": [], "links": [], "threads": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-01T00:00:00Z", "by": "u"},
                    {"lane": "refined", "at": "2026-03-02T00:00:00Z", "by": "u"},
                ],
                "gate_from": 0, "created_at": "2026-03-01T00:00:00Z",
                "updated_at": "2026-03-01T00:00:00Z",
            },
        ],
        "expected": (
            "The rule engine evaluated the policy about skipped items. "
            "It found an item with skip_count >= 5 and fired the policy, "
            "citing the skip_count as the trigger condition in its reasoning."
        ),
        "criteria": (
            "The response confirms the policy was evaluated by the rule engine. "
            "It references the skip_count or the specific item as the trigger. "
            "It must describe the LLM's reasoning from the evaluation log. "
            "It must NOT just describe the policy without citing evaluation results."
        ),
    },
    {
        "name": "Rule_No_Fire_Conditions_Not_Met",
        "input": (
            "The rule engine just ran. None of my items are bugs, none are skipped, "
            "and none are stale. Did any policies fire? Why or why not?"
        ),
        "policy_description": None,
        "items": [
            {
                "id": "CLEAN1", "title": "Fresh normal task", "status": "ready",
                "priority_weight": 5, "skip_count": 0, "reopen_count": 0,
                "category": "feature", "tags": ["frontend"],
                "links": [], "threads": [],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-25T00:00:00Z", "by": "u"},
                    {"lane": "refined", "at": "2026-03-26T00:00:00Z", "by": "u"},
                ],
                "gate_from": 0, "created_at": "2026-03-25T00:00:00Z",
                "updated_at": "2026-03-26T00:00:00Z",
            },
        ],
        "expected": (
            "No policies fired. The items do not meet any trigger conditions — "
            "no critical bugs, no skipped items, no stale items, no overloaded agents."
        ),
        "criteria": (
            "The response states that no policies fired, or that conditions were not met. "
            "It must NOT claim a policy fired when none should have. "
            "It should cite reasoning from the log showing why conditions were not met."
        ),
    },
]


@pytest.mark.timeout(300)
@requires_anthropic
@pytest.mark.parametrize(
    "scenario", POLICY_SCENARIOS, ids=[s["name"] for s in POLICY_SCENARIOS]
)
def test_policy_rule_engine(scenario):
    """Phase 4: LLM interprets natural language policy evaluation results."""
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)

    requests.put(
        BACKLOG_URL,
        json={"version": version, "items": scenario["items"]},
    ).raise_for_status()

    actual = run_flow_policy(
        scenario["input"],
        policy_description=scenario["policy_description"],
    )

    test_case = LLMTestCase(
        input=scenario["input"],
        actual_output=actual,
        expected_output=scenario["expected"],
    )

    correctness = GEval(
        name="Policy Rule Engine Quality",
        criteria=scenario["criteria"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
        model=judge_model,
    )

    assert_test(test_case, [relevancy_metric, correctness])


# ---------------------------------------------------------------------------
# Phase 5: Strategic Lens — Structural + LLM Tests
# These validate that the strategic tribunal lens correctly responds to
# config.strategic.current_focus, high priority_weight, and category alignment.
# ---------------------------------------------------------------------------

RECOMMEND_URL = "http://localhost:8089/api/recommend"


def _make_strategic_config(current_focus):
    """Build a backlog config with strategic focus set. No gate requires — tests
    don't need gate validation and it avoids 422 errors when seeding items."""
    return {
        "scope": "project",
        "project_name": "Flow",
        "statuses": [
            {"id": "backlog", "label": "Backlog"},
            {"id": "refined", "label": "Refined"},
            {"id": "ready", "label": "Ready"},
            {"id": "in-progress", "label": "In Progress"},
            {"id": "code-review", "label": "Code Review"},
            {"id": "done", "label": "Done"},
            {"id": "discarded", "label": "Discarded"},
        ],
        "strategic": {"current_focus": current_focus},
    }


def test_strategic_lens_present_in_tribunal():
    """The tribunal response must include a 'strategic' lens in the lenses array."""
    items = [
        {
            "id": "STR1", "title": "Auth hardening", "status": "ready",
            "priority_weight": 9, "tags": ["auth", "security"],
            "links": [], "lane_history": [
                {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0, "complexity": "medium", "reopen_count": 0,
            "skip_count": 0, "threads": [],
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
        },
    ]
    _seed_items(items, config=_make_strategic_config(["auth"]))

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    tribunal = resp.json()

    lens_names = [l["lens"] for l in tribunal.get("lenses", [])]
    assert "strategic" in lens_names, (
        f"Strategic lens missing from tribunal. Lenses: {lens_names}"
    )


def test_strategic_focus_match_boosts_item():
    """An item with tags matching current_focus must score higher on the strategic lens
    than an item with no matching tags."""
    items = [
        {
            "id": "MATCH", "title": "Fix auth tokens", "status": "ready",
            "priority_weight": 5, "tags": ["auth", "backend"], "category": "feature",
            "links": [], "lane_history": [
                {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0, "complexity": "medium", "reopen_count": 0,
            "skip_count": 0, "threads": [],
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
        },
        {
            "id": "NOMATCH", "title": "Dashboard CSS polish", "status": "ready",
            "priority_weight": 5, "tags": ["frontend", "css"], "category": "feature",
            "links": [], "lane_history": [
                {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0, "complexity": "medium", "reopen_count": 0,
            "skip_count": 0, "threads": [],
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
        },
    ]
    _seed_items(items, config=_make_strategic_config(["auth", "security"]))

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    tribunal = resp.json()

    strategic_lens = next(
        (l for l in tribunal.get("lenses", []) if l["lens"] == "strategic"), None
    )
    assert strategic_lens is not None, "Strategic lens must be present"
    assert strategic_lens["score"] > 0, (
        f"Strategic lens must have positive score when focus matches. Got: {strategic_lens}"
    )
    assert strategic_lens["argued_for"] == "MATCH", (
        f"Strategic lens should argue for MATCH (auth tag matches focus). "
        f"Got: {strategic_lens['argued_for']}"
    )


def test_strategic_high_priority_signal():
    """An item with priority_weight >= 8 must trigger the strategic high-priority signal
    even without current_focus set."""
    items = [
        {
            "id": "HI", "title": "Critical migration", "status": "ready",
            "priority_weight": 9, "tags": ["backend"], "category": "feature",
            "links": [], "lane_history": [
                {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0, "complexity": "medium", "reopen_count": 0,
            "skip_count": 0, "threads": [],
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
        },
        {
            "id": "LO", "title": "Nice-to-have cleanup", "status": "ready",
            "priority_weight": 3, "tags": ["backend"], "category": "feature",
            "links": [], "lane_history": [
                {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0, "complexity": "medium", "reopen_count": 0,
            "skip_count": 0, "threads": [],
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
        },
    ]
    _seed_items(items, config=_make_strategic_config([]))

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    tribunal = resp.json()

    strategic_lens = next(
        (l for l in tribunal.get("lenses", []) if l["lens"] == "strategic"), None
    )
    assert strategic_lens is not None
    assert strategic_lens["score"] > 0, (
        f"Strategic lens must score > 0 for priority_weight=9. Got: {strategic_lens}"
    )
    assert strategic_lens["argument"] is not None, (
        "Strategic lens must have an argument for high priority"
    )
    assert "priority" in strategic_lens["argument"].lower(), (
        f"Strategic argument should reference priority. Got: {strategic_lens['argument']}"
    )


def test_strategic_category_alignment():
    """A bug item should get a strategic boost when current_focus includes 'stability'."""
    items = [
        {
            "id": "BUG1", "title": "Fix crash on login", "status": "ready",
            "priority_weight": 5, "tags": ["auth"], "category": "bug",
            "links": [], "lane_history": [
                {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0, "complexity": "low", "reopen_count": 0,
            "skip_count": 0, "threads": [],
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
        },
        {
            "id": "FEAT1", "title": "Add dark mode", "status": "ready",
            "priority_weight": 5, "tags": ["frontend"], "category": "feature",
            "links": [], "lane_history": [
                {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0, "complexity": "low", "reopen_count": 0,
            "skip_count": 0, "threads": [],
            "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
        },
    ]
    _seed_items(items, config=_make_strategic_config(["stability"]))

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    tribunal = resp.json()

    strategic_lens = next(
        (l for l in tribunal.get("lenses", []) if l["lens"] == "strategic"), None
    )
    assert strategic_lens is not None
    assert strategic_lens["score"] > 0, (
        f"Strategic lens must score > 0 for bug + stability focus. Got: {strategic_lens}"
    )
    assert strategic_lens["argued_for"] == "BUG1", (
        f"Strategic lens should argue for BUG1 (bug aligns with stability focus). "
        f"Got: {strategic_lens['argued_for']}"
    )


# ---------------------------------------------------------------------------
# Strategic Lens — LLM Scenarios (via tribunal)
# ---------------------------------------------------------------------------

STRATEGIC_SCENARIOS = [
    {
        "name": "Strategic_Focus_Drives_Pick",
        "input": (
            "The team's current focus is on auth and security. Two items are ready: "
            "'Fix auth tokens' (tags: auth, backend) and 'Dashboard CSS polish' (tags: frontend, css). "
            "Both have the same priority weight. Which should the tribunal recommend and why?"
        ),
        "items": [
            {
                "id": "SAUTH", "title": "Fix auth tokens", "status": "ready",
                "priority_weight": 5, "tags": ["auth", "backend"], "category": "feature",
                "links": [], "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0, "complexity": "medium", "reopen_count": 0,
                "skip_count": 0, "threads": [],
                "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
            },
            {
                "id": "SCSS", "title": "Dashboard CSS polish", "status": "ready",
                "priority_weight": 5, "tags": ["frontend", "css"], "category": "feature",
                "links": [], "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0, "complexity": "medium", "reopen_count": 0,
                "skip_count": 0, "threads": [],
                "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
            },
        ],
        "config": _make_strategic_config(["auth", "security"]),
        "expected": (
            "The tribunal recommends 'Fix auth tokens' because it aligns with the current "
            "strategic focus on auth and security. The strategic lens gives it a boost due to "
            "tag match. 'Dashboard CSS polish' has no alignment with the declared focus areas."
        ),
        "criteria": (
            "The response recommends 'Fix auth tokens' over 'Dashboard CSS polish'. "
            "The reasoning must reference strategic alignment, focus area, or the auth/security "
            "focus as the differentiator. It must NOT just say 'higher score' without explaining "
            "the strategic lens influence."
        ),
    },
    {
        "name": "Strategic_Bug_Stability_Alignment",
        "input": (
            "The team is focused on stability. There's a bug 'Fix crash on login' "
            "and a feature 'Add dark mode' — same priority, both ready. "
            "Does the bug get a strategic advantage because of the stability focus?"
        ),
        "items": [
            {
                "id": "SBUG", "title": "Fix crash on login", "status": "ready",
                "priority_weight": 5, "tags": ["auth"], "category": "bug",
                "links": [], "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0, "complexity": "low", "reopen_count": 0,
                "skip_count": 0, "threads": [],
                "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
            },
            {
                "id": "SDARK", "title": "Add dark mode", "status": "ready",
                "priority_weight": 5, "tags": ["frontend"], "category": "feature",
                "links": [], "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0, "complexity": "low", "reopen_count": 0,
                "skip_count": 0, "threads": [],
                "created_at": "2026-03-20T10:00:00Z", "updated_at": "2026-03-26T10:00:00Z",
            },
        ],
        "config": _make_strategic_config(["stability"]),
        "expected": (
            "Yes, the bug gets a strategic advantage. With 'stability' as the current focus, "
            "bugs align with stability/quality themes in the strategic lens. 'Fix crash on login' "
            "gets a category alignment boost. 'Add dark mode' as a feature has no stability alignment."
        ),
        "criteria": (
            "The response confirms the bug gets a strategic advantage due to the stability focus. "
            "It must reference category alignment, stability focus, or the strategic lens as the reason. "
            "It must NOT treat both items equally — the bug should be clearly favored."
        ),
    },
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "scenario", STRATEGIC_SCENARIOS, ids=[s["name"] for s in STRATEGIC_SCENARIOS]
)
def test_strategic_tribunal(scenario):
    """Phase 5: LLM interprets strategic lens influence on tribunal recommendations."""
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)

    requests.put(
        BACKLOG_URL,
        json={"version": version, "items": scenario["items"], "config": scenario["config"]},
    ).raise_for_status()

    actual = run_flow_tribunal(scenario["input"])

    test_case = LLMTestCase(
        input=scenario["input"],
        actual_output=actual,
        expected_output=scenario["expected"],
    )

    correctness = GEval(
        name="Strategic Lens Quality",
        criteria=scenario["criteria"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
        model=judge_model,
    )

    assert_test(test_case, [relevancy_metric, correctness])


# ---------------------------------------------------------------------------
# planv3 Task 6: Tribunal Tiebreaker — Structural Tests
# These verify that tie_broken / tiebreaker_reason fields appear in the
# tribunal output and that the deterministic cascade works correctly.
# ---------------------------------------------------------------------------

def _tie_item(item_id, title, complexity, extra=None):
    """Helper: build an item that will tie on tribunal score with its sibling.

    All scoring signals are identical — only `complexity` differs.
    No blocking links, no agents, same priority and lane history.
    """
    base = {
        "id": item_id,
        "title": title,
        "status": "ready",
        "priority_weight": 5,
        "category": "feature",
        "tags": ["backend"],
        "complexity": complexity,
        "links": [],
        "lane_history": [
            {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
            {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
        ],
        "gate_from": 0,
        "reopen_count": 0,
        "skip_count": 0,
        "threads": [],
        "updated_at": "2026-03-26T10:00:00Z",
        "created_at": "2026-03-20T10:00:00Z",
    }
    if extra:
        base.update(extra)
    return base


def test_tribunal_tiebreaker_fields_present():
    """Tribunal picked must always carry tie_broken and tiebreaker_reason keys."""
    items = [
        _tie_item("TIE_A", "Task Alpha", "low"),
        _tie_item("TIE_B", "Task Beta", "medium"),
    ]
    _seed_items(items)

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    picked = resp.json().get("picked", {})

    assert "tie_broken" in picked, "picked must include tie_broken field"
    assert "tiebreaker_reason" in picked, "picked must include tiebreaker_reason field"


def test_tribunal_complexity_tiebreaker():
    """Two tied items: lower complexity (low) must win over medium."""
    items = [
        _tie_item("COMP_MED", "Medium complexity task", "medium"),
        _tie_item("COMP_LOW", "Low complexity task", "low"),
    ]
    _seed_items(items)

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    tribunal = resp.json()
    picked = tribunal["picked"]

    assert picked["item_id"] == "COMP_LOW", (
        f"Lower complexity must win the tie. Got: {picked['item_id']}"
    )
    assert picked["tie_broken"] is True, "Tie must be detected"
    assert picked["tiebreaker_reason"] is not None
    assert "complexity" in (picked["tiebreaker_reason"] or "").lower(), (
        f"Reason must cite complexity. Got: {picked['tiebreaker_reason']}"
    )


def test_tribunal_reopen_tiebreaker():
    """Two tied items: fewer reopens must win."""
    items = [
        _tie_item("REOPEN_MORE", "Unstable task", "medium", {"reopen_count": 2}),
        _tie_item("REOPEN_LESS", "Stable task", "medium", {"reopen_count": 0}),
    ]
    _seed_items(items)

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    tribunal = resp.json()
    picked = tribunal["picked"]

    assert picked["item_id"] == "REOPEN_LESS", (
        f"Fewer reopens must win the tie. Got: {picked['item_id']}"
    )
    assert picked["tie_broken"] is True


def test_tribunal_equal_items_deterministic():
    """Two truly identical items: same winner every time, low confidence, tiebreaker_reason set."""
    items = [
        _tie_item("TWIN_A", "Twin task A", "medium"),
        _tie_item("TWIN_B", "Twin task B", "medium"),
    ]
    _seed_items(items)

    winner_ids = set()
    for _ in range(3):
        resp = requests.get(RECOMMEND_URL)
        assert resp.status_code == 200
        picked = resp.json()["picked"]
        winner_ids.add(picked["item_id"])
        assert picked["confidence"] == "low", (
            f"Tied items must produce low confidence. Got: {picked['confidence']}"
        )
        assert picked["tiebreaker_reason"] is not None

    assert len(winner_ids) == 1, (
        f"Same inputs must always produce the same winner. Got multiple: {winner_ids}"
    )


def test_tribunal_strategic_prevents_tie():
    """Strategic focus on one item's tag must prevent a tie (not a tiebreaker scenario)."""
    items = [
        _tie_item("FOCUS_MATCH", "Auth hardening", "medium", {"tags": ["auth", "backend"]}),
        _tie_item("FOCUS_MISS", "Dashboard polish", "medium", {"tags": ["frontend", "css"]}),
    ]
    config = _make_strategic_config(["auth", "security"])
    _seed_items(items, config=config)

    resp = requests.get(RECOMMEND_URL)
    assert resp.status_code == 200
    picked = resp.json()["picked"]

    assert picked["item_id"] == "FOCUS_MATCH", (
        "Strategic focus must make FOCUS_MATCH win (not a tie)"
    )
    # Strategic lens should prevent a tie — tie_broken should be False
    assert picked["tie_broken"] is False, (
        "Strategic lens should resolve the contest before tiebreaker is needed"
    )


# ---------------------------------------------------------------------------
# planv3 Task 6: Tiebreaker LLM Scenarios
# ---------------------------------------------------------------------------

TIEBREAKER_SCENARIOS = [
    {
        "name": "Tribunal_Tie_Breaking_Complexity",
        "input": (
            "Two tasks have identical priority, status, and tags. "
            "One is low complexity, one is medium. Which does the tribunal recommend, "
            "and why? Was there a tiebreaker?"
        ),
        "items": [
            _tie_item("TB_MED", "Medium effort task", "medium"),
            _tie_item("TB_LOW", "Quick win task", "low"),
        ],
        "config": None,
        "expected": (
            "The tribunal recommends 'Quick win task' because it is lower complexity. "
            "The two items had identical tribunal scores so a tiebreaker was applied: "
            "lower complexity wins as a quick win when all else is equal. "
            "Confidence is low since the scores were tied."
        ),
        "criteria": (
            "The response recommends the low-complexity item ('Quick win task' or TB_LOW). "
            "It must mention a tiebreaker was used, or that complexity resolved the tie. "
            "It must cite low confidence or that the margin was zero / items were tied. "
            "It must NOT present this as a clear, decisive high-confidence recommendation."
        ),
    },
    {
        "name": "Tribunal_Tie_Breaking_Equal",
        "input": (
            "Both items have the exact same priority, complexity, status, and tags. "
            "The tribunal must still pick one. What does it recommend? "
            "How confident is it, and how was the tie broken?"
        ),
        "items": [
            _tie_item("TWIN_X", "Mirror task X", "medium"),
            _tie_item("TWIN_Y", "Mirror task Y", "medium"),
        ],
        "config": None,
        "expected": (
            "The tribunal picks one item but confidence is low because both are identical. "
            "A tiebreaker was applied even though all primary dimensions were equal. "
            "The user should consider both and decide based on context."
        ),
        "criteria": (
            "The response acknowledges that both items are effectively equal or nearly identical. "
            "It must state that confidence is low. "
            "It must NOT say one is clearly better. "
            "It should indicate a tiebreaker was applied or suggest the user decide based on preference."
        ),
    },
    {
        "name": "Tribunal_Tie_Strategic_Alignment",
        "input": (
            "The team is focused on auth. One item has auth tags, the other has frontend tags. "
            "Both have the same priority and complexity. Does strategic alignment prevent a tie, "
            "and which item wins?"
        ),
        "items": [
            _tie_item("SA_AUTH", "Auth token refresh", "medium", {"tags": ["auth", "backend"]}),
            _tie_item("SA_FRONT", "Sidebar redesign", "medium", {"tags": ["frontend", "css"]}),
        ],
        "config": _make_strategic_config(["auth", "security"]),
        "expected": (
            "The tribunal recommends 'Auth token refresh' because it matches the team's "
            "current focus on auth and security. The strategic lens gives it a decisive boost, "
            "so this is NOT a tie — the auth item wins on its own merits."
        ),
        "criteria": (
            "The response recommends the auth item ('Auth token refresh' or SA_AUTH). "
            "It must reference strategic alignment, focus area, or auth/security tags as the reason. "
            "It should indicate this is a genuine win (not just a tiebreaker) due to strategic lens. "
            "It must NOT say there was no difference between the items."
        ),
    },
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "scenario", TIEBREAKER_SCENARIOS, ids=[s["name"] for s in TIEBREAKER_SCENARIOS]
)
def test_tribunal_tiebreaker(scenario):
    """planv3 Task 6: LLM interprets tiebreaker results in tribunal output."""
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)

    payload = {"version": version, "items": scenario["items"]}
    if scenario.get("config"):
        payload["config"] = scenario["config"]
    requests.put(BACKLOG_URL, json=payload).raise_for_status()

    actual = run_flow_tribunal(scenario["input"])

    test_case = LLMTestCase(
        input=scenario["input"],
        actual_output=actual,
        expected_output=scenario["expected"],
    )

    correctness = GEval(
        name="Tiebreaker Justification Quality",
        criteria=scenario["criteria"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
        model=judge_model,
    )

    assert_test(test_case, [relevancy_metric, correctness])


# ---------------------------------------------------------------------------
# planv3 Task 7: ContextRelevancyMetric
# Validates the slicer preserved all facts the LLM needed to answer correctly.
# Score = proportion of expected facts present in the sliced context (0.0–1.0).
# Threshold = 0.9.
# ---------------------------------------------------------------------------

from deepeval.metrics.base_metric import BaseMetric  # noqa: E402


class ContextRelevancyMetric(BaseMetric):
    """Custom metric: verifies the context slice contains the facts needed to answer.

    Internally uses GEval to judge whether each expected factual claim
    (item name, score, agent, conflict, penalty) appears in or is directly
    derivable from the sliced context. Fails if the slicer cut essential data.
    """

    def __init__(self, threshold: float = 0.9, model=None):
        self.threshold = threshold
        self._model = model
        self.score = 0.0
        self.success = False
        self.reason = None

    @property
    def __name__(self):
        return "ContextRelevancyMetric"

    def measure(self, test_case: LLMTestCase, *args, **kwargs) -> float:
        context_parts = getattr(test_case, "context", None) or []
        context_text = "\n".join(context_parts) if context_parts else ""

        if not context_text.strip():
            self.score = 1.0
            self.success = True
            self.reason = "No context provided — metric skipped (vacuous pass)"
            return self.score

        # Build a proxy test case where actual_output = sliced context
        # and expected_output = the expected answer, then ask GEval to judge
        # whether the expected facts are present in the context.
        proxy = LLMTestCase(
            input=test_case.input,
            actual_output=context_text,
            expected_output=test_case.expected_output or "",
        )

        judge = GEval(
            name="ContextFactPresence",
            criteria=(
                "The ACTUAL OUTPUT is the data context that was provided to an AI assistant. "
                "The EXPECTED OUTPUT is the ideal answer the assistant should have produced. "
                "Determine what proportion of factual claims in the expected answer — such as "
                "item titles, IDs, scores, agent names, conflict descriptions, penalty values, "
                "or readiness percentages — are derivable from the actual output context. "
                "Score 1.0 if all key facts are present or reasonably inferable from the context. "
                "Score 0.5 if roughly half the key facts are present. "
                "Score 0.0 if almost none of the key facts appear in the context. "
                "Do not penalise for minor omissions or paraphrasing differences."
            ),
            evaluation_params=[
                LLMTestCaseParams.ACTUAL_OUTPUT,
                LLMTestCaseParams.EXPECTED_OUTPUT,
            ],
            threshold=self.threshold,
            model=self._model,
        )

        judge.measure(proxy)
        self.score = judge.score
        self.success = self.score >= self.threshold
        self.reason = getattr(judge, "reason", None)
        return self.score

    def is_successful(self) -> bool:
        return self.success


_context_relevancy_metric = ContextRelevancyMetric(threshold=0.9, model=judge_model)


# ---------------------------------------------------------------------------
# planv3 Task 7: Scale Eval Scenarios
# Test with large backlogs and deep dependency chains.
# Each scenario also validates ContextRelevancyMetric >= 0.9.
# ---------------------------------------------------------------------------

def _make_scale_items(n: int, seed_priority: int = 5) -> list:
    """Generate n filler backlog items plus one high-priority standout item."""
    items = []
    for i in range(n):
        items.append({
            "id": f"FILLER_{i:03d}",
            "title": f"Routine task {i:03d}",
            "status": "ready",
            "priority_weight": (i % 5) + 1,  # 1-5, never beats the standout
            "category": "feature",
            "tags": ["backend"],
            "complexity": "medium",
            "links": [],
            "lane_history": [
                {"lane": "backlog", "at": "2026-03-01T10:00:00Z", "by": "user"},
                {"lane": "refined", "at": "2026-03-02T10:00:00Z", "by": "user"},
            ],
            "gate_from": 0,
            "reopen_count": 0,
            "skip_count": 0,
            "threads": [],
            "updated_at": "2026-03-10T10:00:00Z",
            "created_at": "2026-03-01T10:00:00Z",
        })
    # The standout: high priority, should always be rank 1
    items.append({
        "id": "SCALE_TOP",
        "title": "Critical data migration",
        "status": "ready",
        "priority_weight": 10,
        "category": "feature",
        "tags": ["database", "backend"],
        "complexity": "high",
        "links": [],
        "lane_history": [
            {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
            {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
        ],
        "gate_from": 0,
        "reopen_count": 0,
        "skip_count": 0,
        "threads": [],
        "updated_at": "2026-03-26T10:00:00Z",
        "created_at": "2026-03-20T10:00:00Z",
    })
    return items


SCALE_SCENARIOS = [
    {
        "name": "Scale_Recommendation_100_Items",
        "input": "What should I work on next? Which item has the highest priority?",
        "setup": lambda: _make_scale_items(100),
        "config": None,
        "expected": (
            "Recommend 'Critical data migration' — it has the highest priority weight (10) "
            "in the backlog. The other 100 filler items have priority 1-5 and do not compete."
        ),
        "criteria": (
            "The response recommends 'Critical data migration' or SCALE_TOP as the top item. "
            "It must NOT recommend a filler item. "
            "It must reference priority or scoring as the reason."
        ),
    },
    {
        "name": "Scale_Critical_Path_Deep_Chain",
        "input": (
            "Which item is on the critical path? "
            "How many items would be unblocked if the root blocker completes?"
        ),
        "setup": lambda: [
            {
                "id": "CHAIN_ROOT", "title": "Root DB migration", "status": "ready",
                "priority_weight": 8, "tags": ["database"],
                "links": [
                    {"type": "blocks", "item_id": "CHAIN_MID1", "reason": "needs schema"},
                    {"type": "blocks", "item_id": "CHAIN_MID2", "reason": "needs schema"},
                ],
                "lane_history": [
                    {"lane": "backlog", "at": "2026-03-20T10:00:00Z", "by": "user"},
                    {"lane": "refined", "at": "2026-03-21T10:00:00Z", "by": "user"},
                ],
                "gate_from": 0, "complexity": "high", "reopen_count": 0,
                "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z",
                "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CHAIN_MID1", "title": "Build user API", "status": "backlog",
                "priority_weight": 6, "tags": ["api"],
                "links": [{"type": "blocks", "item_id": "CHAIN_LEAF", "reason": "API needed"}],
                "lane_history": [], "gate_from": 0, "complexity": "medium",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-22T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CHAIN_MID2", "title": "Build admin dashboard", "status": "backlog",
                "priority_weight": 5, "tags": ["frontend"],
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "medium",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-22T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CHAIN_LEAF", "title": "Write API integration tests", "status": "backlog",
                "priority_weight": 4, "tags": ["testing"],
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "low",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-22T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
        ],
        "config": None,
        "expected": (
            "'Root DB migration' is on the critical path. Completing it would unblock "
            "at least 3 downstream items: Build user API, Build admin dashboard, and "
            "Write API integration tests (via the chain CHAIN_ROOT→CHAIN_MID1→CHAIN_LEAF)."
        ),
        "criteria": (
            "The response identifies 'Root DB migration' or CHAIN_ROOT as the critical path item. "
            "It must mention at least 2 downstream items that would be unblocked. "
            "It must cite a cascade count or unblock chain from the graph data."
        ),
    },
    {
        "name": "Scale_Conflict_Detection_Many_InProgress",
        "input": (
            "Are there any coordination conflicts between agents right now? "
            "Name the agents and items involved."
        ),
        "setup": lambda: [
            {
                "id": "CONF_A1", "title": "Auth service refactor", "status": "in-progress",
                "assigned_to": "agent-alpha", "tags": ["auth", "backend"],
                "priority_weight": 8,
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "high",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CONF_B1", "title": "Auth rate limiting", "status": "in-progress",
                "assigned_to": "agent-beta", "tags": ["auth", "security"],
                "priority_weight": 7,
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "medium",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CONF_A2", "title": "Payment API fix", "status": "in-progress",
                "assigned_to": "agent-alpha", "tags": ["payment", "backend"],
                "priority_weight": 9,
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "medium",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
            {
                "id": "CONF_C1", "title": "DB index optimization", "status": "in-progress",
                "assigned_to": "agent-gamma", "tags": ["database", "backend"],
                "priority_weight": 6,
                "links": [], "lane_history": [], "gate_from": 0, "complexity": "low",
                "reopen_count": 0, "skip_count": 0, "threads": [],
                "updated_at": "2026-03-26T10:00:00Z", "created_at": "2026-03-20T10:00:00Z",
            },
        ],
        "config": None,
        "expected": (
            "Yes, there is a conflict: 'Auth service refactor' (agent-alpha) and "
            "'Auth rate limiting' (agent-beta) share the 'auth' tag, creating a "
            "coordination risk. agent-alpha and agent-beta are working on overlapping auth code."
        ),
        "criteria": (
            "The response confirms that a conflict exists. "
            "It must name agent-alpha and agent-beta (or name the two auth items). "
            "It must identify 'auth' as the shared/conflicting area. "
            "It must NOT say there are no conflicts."
        ),
    },
]


@pytest.mark.timeout(300)
@pytest.mark.parametrize(
    "scenario", SCALE_SCENARIOS, ids=[s["name"] for s in SCALE_SCENARIOS]
)
def test_scale_with_context_relevancy(scenario):
    """planv3 Task 7: Large-backlog scenarios + ContextRelevancyMetric >= 0.9."""
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)

    items = scenario["setup"]()
    payload = {"version": version, "items": items}
    if scenario.get("config"):
        payload["config"] = scenario["config"]
    requests.put(BACKLOG_URL, json=payload).raise_for_status()

    scenario_name = scenario["name"]
    if "Critical_Path" in scenario_name or "Conflict" in scenario_name:
        actual = run_flow_graph(scenario["input"])
    else:
        actual = run_flow(scenario["input"])

    # Capture the sliced context that was sent to the LLM
    ctx = get_last_context()
    context_content = ctx.get("content") or ""
    token_count = ctx.get("estimated_tokens", 0)

    test_case = LLMTestCase(
        input=scenario["input"],
        actual_output=actual,
        expected_output=scenario["expected"],
        context=[context_content] if context_content else [],
    )

    correctness = GEval(
        name="Scale Scenario Correctness",
        criteria=scenario["criteria"],
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.6,
        model=judge_model,
    )

    # Print token usage for documentation per the plan
    print(f"\n[{scenario_name}] context tokens (estimated): {token_count}")

    assert_test(test_case, [relevancy_metric, correctness, _context_relevancy_metric])


# ---------------------------------------------------------------------------
# planv3 Fixture-Based Tests
# Use tribunal_ties_fixture.json and critical_path_fixture.json from stress-tests/.
#
# Fixture loading is done at test runtime — the test code reads the files,
# not the assistant (per CLAUDE.md).  Each fixture carries a _meta block that
# documents the expected slicer / tribunal behaviour.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 (already imported, aliased to avoid shadowing)
from pathlib import Path as _Path  # noqa: E402

_STRESS_DIR = _Path(__file__).parent.parent.parent.parent / "stress-tests"


def _load_fixture(filename: str) -> dict:
    """Load a fixture JSON from stress-tests/. Returns parsed dict."""
    path = _STRESS_DIR / filename
    with open(path) as f:
        return _json.load(f)


def _items_by_id(fixture: dict) -> dict:
    """Return {id: item} index from a fixture's items list."""
    return {i["id"]: i for i in fixture.get("items", [])}


# ---------------------------------------------------------------------------
# Fixture schema normalizer
# The stress-test fixtures use a different schema than the backlog server.
# This normalizer converts fixture items to server-compatible format.
# ---------------------------------------------------------------------------

_PRIORITY_WEIGHT = {"critical": 10, "high": 8, "medium": 5, "low": 2}
_STATUS_MAP = {
    "todo": "ready",
    "open": "backlog",
    "pending": "backlog",
    "blocked": "backlog",   # blocked items are candidates — blocker expressed via links
    "in_progress": "in-progress",
    "in-progress": "in-progress",
    "completed": "done",
    "closed": "done",
    # Force "done" items to "ready" so the server can evaluate them as candidates
    "done": "ready",
}
_MINIMAL_LANE_HISTORY = [
    {"lane": "backlog", "at": "2026-03-01T10:00:00Z", "by": "fixture"},
    {"lane": "refined", "at": "2026-03-02T10:00:00Z", "by": "fixture"},
]


def _normalize_fixture_item(item: dict) -> dict:
    """Convert a fixture item to the server's backlog schema."""
    out = dict(item)

    # Status
    raw_status = out.get("status", "ready")
    out["status"] = _STATUS_MAP.get(raw_status, raw_status)

    # Priority: fixture uses "priority": "critical" / "high" / etc.
    if "priority_weight" not in out:
        raw_priority = out.get("priority", "medium")
        out["priority_weight"] = _PRIORITY_WEIGHT.get(raw_priority, 5)

    # Links: fixture uses "blocks": ["ID1", "ID2"] flat list
    if "links" not in out:
        blocks = out.get("blocks") or []
        out["links"] = [{"type": "blocks", "item_id": bid} for bid in blocks]

    # Reopen / skip count field names
    if "reopen_count" not in out:
        out["reopen_count"] = out.get("reopens", 0) or 0
    if "skip_count" not in out:
        out["skip_count"] = out.get("skips", 0) or 0

    # Required fields the server needs
    out.setdefault("lane_history", _MINIMAL_LANE_HISTORY)
    out.setdefault("gate_from", 0)
    out.setdefault("threads", [])
    out.setdefault("category", "feature")
    out.setdefault("complexity", "medium")

    return out


def _seed_pair(item_a, item_b, config=None):
    """Seed exactly two normalized fixture items into the running server."""
    resp = requests.get(BACKLOG_URL)
    resp.raise_for_status()
    version = resp.json().get("version", 0)
    payload = {
        "version": version,
        "items": [_normalize_fixture_item(item_a), _normalize_fixture_item(item_b)],
    }
    if config:
        payload["config"] = config
    r = requests.put(BACKLOG_URL, json=payload)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# tribunal_ties_fixture.json — structural correctness per tie scenario
# ---------------------------------------------------------------------------

class TestTribunalTiesFixture:
    """Structural tests driven by tribunal_ties_fixture.json.

    Each method seeds exactly the two items from one tie pair, hits
    /api/recommend, and verifies the server-side tiebreaker output.
    """

    @pytest.fixture(autouse=True)
    def _fixture_data(self):
        self.fixture = _load_fixture("tribunal_ties_fixture.json")
        self.items = _items_by_id(self.fixture)
        self.meta = self.fixture.get("_meta", {})

    def test_tie_complexity_low_wins(self):
        """TIE-001 (low complexity) must beat TIE-002 (medium complexity)."""
        _seed_pair(self.items["TIE-001"], self.items["TIE-002"])
        tribunal = requests.get(RECOMMEND_URL).json()
        picked = tribunal["picked"]

        assert picked["item_id"] == "TIE-001", (
            f"Low-complexity item must win. Got: {picked['item_id']}"
        )
        assert picked["tie_broken"] is True
        assert picked["tiebreaker_reason"] is not None
        assert "complexity" in (picked["tiebreaker_reason"] or "").lower()

    def test_tie_readiness_clear_beats_blocked(self):
        """TIE-003 (no blocker) must beat TIE-004 (has blocker, lower readiness)."""
        _seed_pair(self.items["TIE-003"], self.items["TIE-004"])
        tribunal = requests.get(RECOMMEND_URL).json()
        picked = tribunal["picked"]

        assert picked["item_id"] == "TIE-003", (
            f"Unblocked item must win on readiness. Got: {picked['item_id']}"
        )
        assert picked["tie_broken"] is True

    def test_tie_equal_deterministic_low_confidence(self):
        """TIE-005 and TIE-006 are identical — same winner on every call, low confidence."""
        _seed_pair(self.items["TIE-005"], self.items["TIE-006"])

        winner_ids = set()
        for _ in range(3):
            picked = requests.get(RECOMMEND_URL).json()["picked"]
            winner_ids.add(picked["item_id"])
            assert picked["confidence"] == "low", (
                f"Identical items must produce low confidence. Got: {picked['confidence']}"
            )
            assert picked["tiebreaker_reason"] is not None

        assert len(winner_ids) == 1, (
            f"Identical items must always yield the same winner. Got: {winner_ids}"
        )

    def test_tie_strategic_focus_prevents_tie(self):
        """TIE-007 (growth tags) must beat TIE-008 when current_focus = growth."""
        strategic_config = self.meta.get("strategic_config") or _make_strategic_config(
            self.items.get("TIE-007", {}).get("tags", [])
        )
        _seed_pair(self.items["TIE-007"], self.items["TIE-008"], config=strategic_config)
        tribunal = requests.get(RECOMMEND_URL).json()
        picked = tribunal["picked"]

        assert picked["item_id"] == "TIE-007", (
            f"Strategic focus item must win outright. Got: {picked['item_id']}"
        )
        assert picked["tie_broken"] is False, (
            "Strategic lens should resolve the contest — tie_broken must be False"
        )

    def test_tie_reopens_risk_lens_rewards_unstable(self):
        """TIE-010 (2 reopens) beats TIE-009 (0 reopens) via the risk lens.

        The risk lens scores reopened items HIGHER — unstable areas that have
        bounced back are riskier to defer further. This is NOT a tiebreaker
        scenario: TIE-010 wins outright with a higher tribunal_score.
        """
        _seed_pair(self.items["TIE-009"], self.items["TIE-010"])
        tribunal = requests.get(RECOMMEND_URL).json()
        picked = tribunal["picked"]

        assert picked["item_id"] == "TIE-010", (
            f"Risk lens must favour the reopened item (unstable area). Got: {picked['item_id']}"
        )
        assert picked["tie_broken"] is False, (
            "Items must NOT tie — risk lens gives TIE-010 a higher tribunal score"
        )


# ---------------------------------------------------------------------------
# critical_path_fixture.json — context slicer correctness at scale
# ---------------------------------------------------------------------------

# The 7 critical-chain IDs documented in the fixture
_CRITICAL_CHAIN_IDS = [f"CP-{str(i).zfill(3)}" for i in range(1, 8)]  # CP-001 … CP-007


class TestCriticalPathFixture:
    """Slicer correctness tests driven by critical_path_fixture.json (35 items).

    Verifies that a critical_path query:
    - includes all 7 chain nodes in the graph context
    - excludes the 23 noise nodes (context is smaller than total item count)
    """

    @pytest.fixture(autouse=True)
    def _fixture_data(self):
        self.fixture = _load_fixture("critical_path_fixture.json")
        self.all_items = self.fixture.get("items", [])
        self.meta = self.fixture.get("_meta", {})

    def _seed_all(self):
        resp = requests.get(BACKLOG_URL)
        resp.raise_for_status()
        version = resp.json().get("version", 0)
        normalized = [_normalize_fixture_item(i) for i in self.all_items]
        requests.put(
            BACKLOG_URL,
            json={"version": version, "items": normalized},
        ).raise_for_status()

    def test_critical_chain_nodes_in_context(self):
        """All 7 critical chain nodes must appear in the graph context after slicing."""
        self._seed_all()
        run_flow_graph(
            "Which item is on the critical path? How many items does it block transitively?"
        )
        ctx = get_last_context()
        node_ids = set(ctx.get("node_ids", []))

        missing = [cid for cid in _CRITICAL_CHAIN_IDS if cid not in node_ids]
        assert not missing, (
            f"Critical chain nodes missing from sliced context: {missing}. "
            f"Present: {sorted(node_ids)}"
        )

    def test_slicer_trims_noise_from_critical_path_query(self):
        """A critical_path query must produce fewer nodes in context than total items seeded."""
        self._seed_all()
        run_flow_graph(
            "Which item is on the critical path? Show me the full blocking chain."
        )
        ctx = get_last_context()
        node_ids = ctx.get("node_ids", [])
        total_items = len(self.all_items)

        assert len(node_ids) < total_items, (
            f"Slicer must trim noise: context has {len(node_ids)} nodes "
            f"but {total_items} items were seeded."
        )

    def test_slicer_token_budget_respected(self):
        """Context token estimate must stay within MAX_CONTEXT_TOKENS."""
        from context_slicer import MAX_CONTEXT_TOKENS
        self._seed_all()
        run_flow_graph(
            "Which item is on the critical path? Show me the full blocking chain."
        )
        ctx = get_last_context()
        tokens = ctx.get("estimated_tokens", 0)

        assert tokens <= MAX_CONTEXT_TOKENS, (
            f"Context exceeded token budget: {tokens} > {MAX_CONTEXT_TOKENS}"
        )
