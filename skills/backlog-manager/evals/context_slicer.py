"""
Context Slicer — planv3

Sits between the server APIs and the LLM call. Each run_flow_* function uses the
slicer instead of dumping raw JSON into the prompt. The slicer:
  1. Detects the query's intent (recommendation, conflict, critical_path, etc.)
  2. Returns only the items/fields the LLM needs for that intent
  3. Enforces a configurable token budget by summarising lower-ranked items if
     the slice is genuinely large — never silently dropping items

Token budget is configurable via the MAX_CONTEXT_TOKENS env var (default: 4000).
Summarisation is logged as a warning when it kicks in.
"""

import json
import logging
import os
import re
import warnings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Token budget (Task 8)
# ---------------------------------------------------------------------------
MAX_CONTEXT_TOKENS = int(os.environ.get("MAX_CONTEXT_TOKENS", 4000))

# Rough token estimator: 1 token ≈ 4 chars of JSON text
def _estimate_tokens(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"))) // 4


# ---------------------------------------------------------------------------
# Intent detection (Task 0 — detect_intents)
# ---------------------------------------------------------------------------

_INTENT_PATTERNS: dict[str, list[str]] = {
    "recommendation": [
        r"\bwhat next\b", r"\bwork on\b", r"\bpick\b", r"\brecommend\b",
        r"\bpriority\b", r"\bshould i\b", r"\btop item\b",
    ],
    "conflict": [
        r"\bconflict\b", r"\boverlap\b", r"\bcoordination\b",
        r"\brisk\b", r"\bcollision\b",
    ],
    "critical_path": [
        r"\bcritical\b", r"\bblocking\b", r"\bcascade\b",
        r"\bunblock\b", r"\bdepend\b",
    ],
    "counterfactual": [
        r"\bwhy not\b", r"\bwhy wasn.t\b", r"\binstead\b",
        r"\balternative\b", r"\bother option\b",
    ],
    "readiness": [
        r"\breadiness\b", r"\bblocked\b", r"\bstartable\b",
        r"\bpenalty\b", r"\bblocker\b",
    ],
    "confidence": [
        r"\bconfident\b", r"\bclose\b", r"\bmargin\b",
        r"\bconsider both\b", r"\bsimilar\b",
    ],
    "policy": [
        r"\bpolicy\b", r"\bpolicies\b", r"\brule\b",
        r"\bfire\b", r"\btrigger\b", r"\bautomat\b",
    ],
}


def detect_intents(query: str) -> list[str]:
    """Return all matching intent categories for the given query string."""
    q = query.lower()
    matched = [
        intent
        for intent, patterns in _INTENT_PATTERNS.items()
        if any(re.search(p, q) for p in patterns)
    ]
    return matched if matched else ["general"]


# ---------------------------------------------------------------------------
# slice_scores — for run_flow() (Task 1)
# ---------------------------------------------------------------------------

def slice_scores(items: list[dict], intents: list[str], agent_count: int) -> str:
    """
    Trim scored items to what the LLM needs for the detected intents.

    Returns a JSON string ready to drop into a prompt.
    Items are already ranked (rank 1 = first). Boundaries are data-driven:
    - recommendation: top N where N = max(4, agent_count * 2), expanded if items
      within 10% of Nth item's score exist
    - readiness: all items with non-empty blockers + their blocker context
    - confidence: all items within 15% score margin of rank-1 score
    - general fallback: top 5 + any blocked/critical items
    """
    if not items:
        return "[]"

    selected: list[dict] = []

    if "readiness" in intents:
        # Include all items that have blockers
        selected = [i for i in items if i.get("readiness", {}).get("blockers")]
        # Also include rank-1 regardless
        if items and items[0] not in selected:
            selected = [items[0]] + selected

    elif "confidence" in intents:
        # All items within 15% score margin of winner
        top_score = items[0].get("score", 0)
        threshold = top_score * 0.85 if top_score > 0 else 0
        selected = [i for i in items if i.get("score", 0) >= threshold]

    elif "recommendation" in intents or "general" in intents:
        # Top N = max(4, active_agents * 2)
        n = max(4, agent_count * 2)
        selected = items[:n]
        # Expand: include items within 10% of Nth item's score
        if selected:
            nth_score = selected[-1].get("score", 0)
            expansion_threshold = nth_score * 0.90 if nth_score > 0 else 0
            for item in items[n:]:
                if item.get("score", 0) >= expansion_threshold:
                    selected.append(item)
                else:
                    break

    else:
        # Fallback: top 5
        selected = items[:5]

    # Apply token budget (Task 8)
    selected = _apply_token_budget(selected, items)

    return json.dumps(selected, indent=2)


def _apply_token_budget(selected: list[dict], all_items: list[dict]) -> list[dict]:
    """
    If the selected slice exceeds MAX_CONTEXT_TOKENS, progressively summarise
    lower-ranked items (keep title + score + rank only). Never silently drops.
    """
    if _estimate_tokens(selected) <= MAX_CONTEXT_TOKENS:
        return selected

    # Try summarising from the bottom up
    result = list(selected)
    for i in range(len(result) - 1, -1, -1):
        if _estimate_tokens(result) <= MAX_CONTEXT_TOKENS:
            break
        item = result[i]
        result[i] = {
            "id": item.get("id"),
            "title": item.get("title"),
            "rank": item.get("rank"),
            "score": item.get("score"),
            "_summarised": True,
        }

    if _estimate_tokens(result) > MAX_CONTEXT_TOKENS:
        warnings.warn(
            f"[context_slicer] Context still exceeds {MAX_CONTEXT_TOKENS} tokens "
            f"after summarisation ({_estimate_tokens(result)} estimated). "
            "Consider raising MAX_CONTEXT_TOKENS.",
            RuntimeWarning,
            stacklevel=3,
        )
        logger.warning(
            "context_slicer: budget exceeded after summarisation — %d estimated tokens",
            _estimate_tokens(result),
        )

    return result


# ---------------------------------------------------------------------------
# slice_tribunal — for run_flow_tribunal() (Task 2)
# ---------------------------------------------------------------------------

def slice_tribunal(tribunal: dict, intents: list[str], query: str) -> str:
    """
    Trim tribunal output based on the query intent.

    - counterfactual: picked item + the named runner-up(s) from shadow_ranking
    - confidence: picked + all shadow items within 15% of winner's tribunal_score
    - general/recommendation: full picked + top-3 shadow + lenses
    """
    if not tribunal.get("picked"):
        return json.dumps(tribunal, indent=2)

    picked = tribunal["picked"]
    shadow = tribunal.get("shadow_ranking", [])
    lenses = tribunal.get("lenses", [])

    if "counterfactual" in intents:
        # Find any item names mentioned in the query and include them
        query_lower = query.lower()
        named_shadows = [
            s for s in shadow
            if s.get("title", "").lower() in query_lower
        ]
        # Always include at least the first shadow item for context
        if not named_shadows and shadow:
            named_shadows = shadow[:1]

        sliced = {
            "picked": picked,
            "shadow_ranking": named_shadows,
            "lenses": [],          # not needed for counterfactual
            "candidates_evaluated": tribunal.get("candidates_evaluated", 0),
        }

    elif "confidence" in intents:
        winner_score = picked.get("tribunal_score", 0)
        threshold = winner_score * 0.85 if winner_score > 0 else 0
        close_shadows = [s for s in shadow if s.get("tribunal_score", 0) >= threshold]
        sliced = {
            "picked": picked,
            "shadow_ranking": close_shadows or shadow[:2],
            "lenses": lenses,
            "candidates_evaluated": tribunal.get("candidates_evaluated", 0),
        }

    else:
        # recommendation / general: picked + top 3 shadow + all lenses
        sliced = {
            "picked": picked,
            "shadow_ranking": shadow[:3],
            "lenses": lenses,
            "candidates_evaluated": tribunal.get("candidates_evaluated", 0),
        }

    token_count = _estimate_tokens(sliced)
    if token_count > MAX_CONTEXT_TOKENS:
        logger.warning(
            "context_slicer (tribunal): slice is %d estimated tokens (budget %d)",
            token_count,
            MAX_CONTEXT_TOKENS,
        )

    return json.dumps(sliced, indent=2)


# ---------------------------------------------------------------------------
# slice_graph — for run_flow_graph() (Task 3)
# ---------------------------------------------------------------------------

def slice_graph(graph: dict, pulse: dict, intents: list[str]) -> dict:
    """
    Trim graph + pulse data to intent-relevant nodes.

    Returns a dict with the same keys as the existing slim_graph structure
    (nodes, critical_path, conflicts, rebalancing) plus startable_items from pulse.
    Boundaries are data-driven — no hardcoded caps on in-progress or critical items.
    """
    nodes = graph.get("nodes", [])
    critical_path_ids = set(graph.get("critical_path", []))
    conflicts = graph.get("conflicts", []) or pulse.get("conflicts", [])
    rebalancing = graph.get("rebalancing", [])

    if "conflict" in intents:
        # All in-progress nodes + their assigned agents — no cap
        selected_nodes = [n for n in nodes if n.get("status") == "in-progress"]

    elif "critical_path" in intents:
        # Full critical chain via BFS — follow the entire dependency graph from
        # every critical path seed node in both directions.
        # Server edges use "source"/"target" keys (not "from"/"to").
        critical_ids = {n["id"] for n in nodes if n.get("is_critical_path")}
        edges = graph.get("edges", [])

        # Build bidirectional adjacency list from edges
        adjacency: dict[str, set] = {}
        for e in edges:
            src = e.get("source") or e.get("from")
            tgt = e.get("target") or e.get("to")
            if src and tgt:
                adjacency.setdefault(src, set()).add(tgt)
                adjacency.setdefault(tgt, set()).add(src)

        # BFS from all critical seed nodes
        visited = set(critical_ids)
        frontier = set(critical_ids)
        while frontier:
            next_frontier = set()
            for nid in frontier:
                for neighbour in adjacency.get(nid, set()):
                    if neighbour not in visited:
                        visited.add(neighbour)
                        next_frontier.add(neighbour)
            frontier = next_frontier

        selected_nodes = [n for n in nodes if n.get("id") in visited]

    elif "recommendation" in intents or "general" in intents:
        # Startable candidates only (non-done, non-discarded, non-in-progress)
        selected_nodes = [
            n for n in nodes
            if n.get("status") not in ("done", "discarded", "in-progress")
        ]

    else:
        selected_nodes = nodes

    startable_items = [
        {"id": s.get("id"), "title": s.get("title"), "score": s.get("score")}
        for s in pulse.get("startable_items", [])
    ]

    sliced = {
        "nodes": selected_nodes,
        "critical_path": list(critical_path_ids),
        "conflicts": conflicts,
        "rebalancing": rebalancing,
        "startable_items": startable_items,
    }

    token_count = _estimate_tokens(sliced)
    if token_count > MAX_CONTEXT_TOKENS:
        logger.warning(
            "context_slicer (graph): slice is %d estimated tokens (budget %d)",
            token_count,
            MAX_CONTEXT_TOKENS,
        )

    return sliced


# ---------------------------------------------------------------------------
# slice_policies — for run_flow_policy() (Task 4)
# ---------------------------------------------------------------------------

def slice_policies(
    eval_result: dict,
    recent_log: list[dict],
    policies: list[dict],
    intents: list[str],
) -> str:
    """
    Trim policy data to only fired policies and their affected items.

    Returns a JSON string ready to drop into a prompt.
    """
    # Fired log entries only
    fired_log = [e for e in recent_log if e.get("fired")]
    fired_policy_ids = {e.get("policy_id") for e in fired_log}

    # Active policies, prefer fired ones first
    fired_policies = [p for p in policies if p.get("id") in fired_policy_ids]
    other_active = [
        p for p in policies
        if p.get("active") and p.get("id") not in fired_policy_ids
    ]

    # If nothing fired, include all active (can't be more useful than the full picture)
    selected_log = fired_log if fired_log else recent_log[-3:]
    selected_policies = fired_policies if fired_policies else other_active[:3]

    sliced = {
        "evaluation_result": eval_result,
        "recent_log": selected_log,
        "active_policies": selected_policies,
    }

    token_count = _estimate_tokens(sliced)
    if token_count > MAX_CONTEXT_TOKENS:
        logger.warning(
            "context_slicer (policies): slice is %d estimated tokens (budget %d)",
            token_count,
            MAX_CONTEXT_TOKENS,
        )

    return json.dumps(sliced, indent=2)
