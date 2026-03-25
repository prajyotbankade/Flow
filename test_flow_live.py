import pytest
import requests
from deepeval import assert_test
from deepeval.metrics import AnswerRelevancyMetric, GEval
from deepeval.models import OllamaModel
from deepeval.test_case import LLMTestCase, LLMTestCaseParams

from flow_skill import run_flow

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
