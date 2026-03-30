import os
import pytest
import requests
from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, GEval
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from eval_flow_skill import run_flow, run_flow_tribunal, run_flow_graph, run_flow_policy, EVAL_LLM

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
