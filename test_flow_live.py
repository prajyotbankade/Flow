import pytest
import requests
from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, GEval
from deepeval.models import OllamaModel
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from flow_skill import run_flow, run_flow_tribunal

BACKLOG_URL = "http://localhost:8089/api/backlog"

local_qwen = OllamaModel(model="qwen2.5-coder:14b")
relevancy_metric = AnswerRelevancyMetric(threshold=0.6, model=local_qwen)

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
            "while 'Blocked Task' is suppressed to -2.0 by a blocked_penalty of -3.0."
        ),
        "criteria": (
            "The response recommends 'Root Task' and references the unblock score_breakdown field "
            "or unblocking multiplier as the reason it outranks 'Blocked Task'. "
            "Bonus if it notes the blocked_penalty applied to 'Blocked Task'."
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
        model=local_qwen,
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
        model=local_qwen,
    )

    assert_test(test_case, [relevancy_metric, correctness])
