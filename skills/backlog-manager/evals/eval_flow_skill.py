import json
import os
import requests

BASE_URL      = "http://localhost:8089"
SCORES_URL    = f"{BASE_URL}/api/scores"
RECOMMEND_URL = f"{BASE_URL}/api/recommend"
GRAPH_URL     = f"{BASE_URL}/api/graph"
PULSE_URL     = f"{BASE_URL}/api/pulse"

# ---------------------------------------------------------------------------
# LLM backend toggle: set EVAL_LLM=openai to use OpenAI, defaults to ollama
# ---------------------------------------------------------------------------
EVAL_LLM = os.environ.get("EVAL_LLM", "ollama").lower()

OLLAMA_URL    = "http://localhost:11434/api/generate"
OLLAMA_MODEL  = "qwen2.5-coder:7b"

_openai_client = None
OPENAI_MODEL   = "gpt-4o-mini"

if EVAL_LLM == "openai":
    from openai import OpenAI
    from dotenv import load_dotenv
    load_dotenv()
    _openai_client = OpenAI()

# ---------------------------------------------------------------------------
# Token tracker — captures prompt + completion tokens from every LLM call
# ---------------------------------------------------------------------------
_token_usage = {
    "generation": {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0},
}


def _track_tokens(data, category="generation"):
    """Track tokens from either Ollama or OpenAI response."""
    _token_usage.setdefault(category, {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})
    if EVAL_LLM == "openai":
        _token_usage[category]["prompt_tokens"] += data.prompt_tokens or 0
        _token_usage[category]["completion_tokens"] += data.completion_tokens or 0
    else:
        _token_usage[category]["prompt_tokens"] += data.get("prompt_eval_count", 0)
        _token_usage[category]["completion_tokens"] += data.get("eval_count", 0)
    _token_usage[category]["calls"] += 1


def _call_llm(prompt: str) -> str:
    """Call the configured LLM backend and track tokens."""
    if EVAL_LLM == "openai":
        resp = _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1024,
        )
        if resp.usage:
            _track_tokens(resp.usage, "generation")
        return resp.choices[0].message.content.strip()
    else:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        resp_json = resp.json()
        _track_tokens(resp_json, "generation")
        return resp_json.get("response", "").strip()


def get_token_usage():
    """Return accumulated token usage across all categories."""
    return _token_usage


def run_flow(query: str) -> str:
    """
    Ask the LLM to recommend the top work item.
    The LLM receives the full score_breakdown for every item so it can
    cite exact numeric factors — not guess or template-fill.
    """
    try:
        scores_resp = requests.get(SCORES_URL, timeout=5)
        scores_resp.raise_for_status()
        scored_items = scores_resp.json().get("items", [])

        if not scored_items:
            return "No tasks available for recommendation."

        # Pre-process: strip zero fields, add WHY_TEXT to rank 1
        for rank, item in enumerate(scored_items, start=1):
            item["rank"] = rank
            bd = item.get("score_breakdown", {})
            # Strip zero-value fields so LLM only sees active signals
            item["score_breakdown"] = {k: v for k, v in bd.items() if v != 0 and v != 0.0 and v != -0.0}
            # Pre-compute the decisive factor for rank 1
            if rank == 1:
                if bd.get("unblock", 0) > 0:
                    item["WHY_TEXT"] = f"unblock: {bd['unblock']} — this item unblocks downstream work"
                elif bd.get("critical_bug", 0) > 0:
                    item["WHY_TEXT"] = f"critical_bug: {bd['critical_bug']} — bug category scoring boost"
                else:
                    item["WHY_TEXT"] = f"base_priority: {bd.get('base_priority', 0)} — highest base priority"

        context_block = json.dumps(scored_items, indent=2)

        prompt = f"""You are the Flow Work Intelligence Engine — a precise, senior engineering advisor.

The backlog has been scored and ranked. Each item includes a full score_breakdown
showing exactly which factors contributed to the final score. Non-zero fields are
the active scoring signals. Trust the data — do not invent factors.

Each item also includes a readiness section: its own readiness score (0.0–1.0) plus
blocker readiness scores. blocked_penalty is proportional to blocker readiness:
  blocked_penalty = -(1.0 - blocker_readiness) * 3.0
So: blocker at 75% readiness → penalty is -0.75, blocker at 0% → penalty is -3.0.
If the question asks about readiness or blocked_penalty, you MUST state the blocker's
readiness %, the resulting penalty value, and whether the item is startable (≥70% = yes).

SCORED BACKLOG (rank 1 = highest priority):
{context_block}

QUESTION:
{query}

IMPORTANT: Answer the QUESTION directly. Focus your response on what was specifically asked.
If the question is about readiness or blocked_penalty, lead with that analysis.
If the question is about ranking or recommendation, lead with the pick.

Include these sections as relevant to the question:

PICK: [rank 1 item's title and id]
WHY: [copy rank 1 item's WHY_TEXT field exactly as written]
RUNNER-UP: [name the rank 2 item and explain why it lost — cite its blocked_penalty or missing boost]
READINESS: [for EVERY item that has a readiness.blockers array, state: blocker's readiness %, the blocked_penalty value, and whether the item is startable (readiness ≥ 0.70 = startable with risk). If no items have blockers, say "No blocked items."]

Rules:
- PICK is always the rank 1 item. Do NOT pick any other item.
- WHY must be copied from the rank 1 item's WHY_TEXT field — do not change it.
- RUNNER-UP: if it has a non-zero blocked_penalty, state the exact value and blocker's readiness %.
- When a blocker has readiness ≥ 0.70 (70%), the blocked item IS startable with risk — do NOT say it is blocked or not startable.
- Keep each section to 1-2 sentences. No extra commentary."""

        return _call_llm(prompt)

    except Exception as e:
        return f"Flow Error: {str(e)}"


def run_flow_tribunal(query: str, agent: str = None) -> str:
    """
    Ask the LLM to answer using the tribunal recommendation.
    The LLM receives the full tribunal output — picked item with reasoning,
    supporting lenses, confidence, and shadow ranking with counterfactuals.
    """
    try:
        url = RECOMMEND_URL
        if agent:
            url += f"?agent={agent}"
        rec_resp = requests.get(url, timeout=5)
        rec_resp.raise_for_status()
        tribunal = rec_resp.json()

        if not tribunal.get("picked"):
            return "No eligible items for recommendation."

        context_block = json.dumps(tribunal, indent=2)

        prompt = f"""You are the Flow Work Intelligence Engine — a precise, senior engineering advisor.

The tribunal has evaluated all eligible work items through 6 lenses (urgency, leverage,
agent_fit, risk, momentum, strategic) and produced a justified recommendation. The output includes:
- The picked item with reasoning, confidence, and supporting lenses
- A shadow ranking of runners-up with "why not" explanations (counterfactuals)

Trust the tribunal data — cite specific lens arguments and counterfactual reasons.

TRIBUNAL RECOMMENDATION:
{context_block}

QUESTION:
{query}

IMPORTANT: Answer the QUESTION directly. Focus your response on what was specifically asked.

Instructions:
- Respond in 3-5 sentences.
- If the question asks about a SPECIFIC item (e.g. "why wasn't X picked?"), focus entirely on that item — find it in the shadow_ranking and cite its lost_reason, lost_on_lens, and counterfactual reasoning. Do NOT lead with the winner unless the question asks about it.
- If the question is general, lead with the recommended item and the tribunal's reasoning.
- Cite specific lens arguments (urgency, leverage, etc.) and their weights.
- Mention the confidence level and what drove it.
- IMPORTANT: If confidence is "low" or the margin between the top items is small, say so explicitly. Do NOT declare a clear winner — instead state that both items are close and the user should consider context or preference to decide."""

        return _call_llm(prompt)

    except Exception as e:
        return f"Flow Error: {str(e)}"


def run_flow_graph(query: str, agent: str = None) -> str:
    """
    Ask the LLM to answer using the dependency graph and pulse data.
    The LLM receives the graph (nodes, edges, critical_path, conflicts, rebalancing)
    and the pulse (active agents, startable items) for a full coordination picture.
    """
    try:
        graph_resp = requests.get(GRAPH_URL, timeout=5)
        graph_resp.raise_for_status()
        graph = graph_resp.json()

        pulse_url = PULSE_URL + (f"?agent={agent}" if agent else "")
        pulse_resp = requests.get(pulse_url, timeout=5)
        pulse_resp.raise_for_status()
        pulse = pulse_resp.json()

        # Slim down context to only the fields the LLM needs
        slim_graph = {
            "nodes": [
                {
                    "id": n.get("id"),
                    "title": n.get("title"),
                    "status": n.get("status"),
                    "score": n.get("score"),
                    "is_critical_path": n.get("is_critical_path"),
                    "cascade_count": n.get("cascade_count"),
                    "assigned_to": n.get("assigned_to"),
                    "tags": n.get("tags"),
                }
                for n in graph.get("nodes", [])
            ],
            "critical_path": graph.get("critical_path", []),
            "conflicts": graph.get("conflicts", []),
            "rebalancing": graph.get("rebalancing", []),
        }
        # Derive active agents from graph nodes (most reliable — uses actual item assignments)
        seen = {}
        for n in graph.get("nodes", []):
            agent_name = n.get("assigned_to")
            if agent_name and n.get("status") == "in-progress" and agent_name not in seen:
                seen[agent_name] = {
                    "agent": agent_name,
                    "current_item": n.get("title"),
                    "tags": n.get("tags", []),
                }
        active_agents = list(seen.values())

        startable_items = [
            {"id": s.get("id"), "title": s.get("title"), "score": s.get("score")}
            for s in pulse.get("startable_items", [])
        ]
        conflicts = slim_graph.get("conflicts", []) or pulse.get("conflicts", [])
        critical_nodes = [n for n in slim_graph["nodes"] if n.get("is_critical_path")]

        # Pre-format sections as plain text so the model doesn't need to parse JSON
        if active_agents:
            agents_text = "\n".join(
                f"  - {a.get('agent', 'unknown')} is working on \"{a.get('current_item', '?')}\" (tags: {', '.join(a.get('tags', []))})"
                for a in active_agents
            )
        else:
            agents_text = "  No agents are currently active."

        if conflicts:
            conflicts_text = "\n".join(
                f"  - CONFLICT: {c.get('description', 'unknown conflict')}"
                for c in conflicts
            )
        else:
            conflicts_text = "  No conflicts detected."

        if startable_items:
            startable_text = "\n".join(
                f"  - \"{s.get('title', '?')}\" (id: {s.get('id', '?')}, score: {s.get('score', '?')})"
                for s in startable_items
            )
        else:
            # Fallback: list non-done graph nodes as candidates
            candidates = [n for n in slim_graph["nodes"] if n.get("status") not in ("done", "discarded", "in-progress")]
            if candidates:
                startable_text = "\n".join(
                    f"  - \"{n.get('title', '?')}\" (id: {n.get('id', '?')}, status: {n.get('status', '?')}, score: {n.get('score', '?')})"
                    for n in candidates
                )
            else:
                startable_text = "  No startable items available."

        if critical_nodes:
            critical_text = "\n".join(
                f"  - \"{n.get('title', '?')}\" — cascade_count: {n.get('cascade_count', 0)}"
                for n in critical_nodes
            )
        else:
            critical_text = "  No items on the critical path."

        prompt = f"""You are the Flow Work Intelligence Engine.

ACTIVE AGENTS:
{agents_text}

CONFLICTS:
{conflicts_text}

STARTABLE ITEMS:
{startable_text}

CRITICAL PATH:
{critical_text}

QUESTION:
{query}

IMPORTANT: Answer the QUESTION directly. Focus your response on what was specifically asked.
If the question is about conflicts, lead with and focus on the CONFLICTS data.
If the question is about what to work on, lead with RECOMMENDATION.
If the question covers multiple topics, address each one.

Available sections (include only those relevant to the question):

RECOMMENDATION: Pick a task from STARTABLE ITEMS, or the highest-score node if none are startable.
AGENTS: State each active agent and their current task. If none, say "No other agents are currently active."
CONFLICTS: If a CONFLICT line exists above, there IS a conflict — state it clearly, name both items, both agents, and the shared tags. If none, say "No coordination conflicts detected."
CRITICAL PATH: If any item is listed above, name it and its cascade_count. If none, say "No items are on the critical path."

Rules:
- ONLY include sections that directly answer the question. Omit all others.
- Keep each section to 1-2 sentences.
- Cite numeric values (cascade_count, tags, scores)."""

        return _call_llm(prompt)

    except Exception as e:
        return f"Flow Error: {str(e)}"


def run_flow_policy(query: str, policy_description: str = None) -> str:
    """
    Ask the LLM to reason about policy evaluation results.
    Optionally seeds a temporary policy before evaluating, then interprets the result.
    """
    try:
        policies_url = f"{BASE_URL}/api/policies"
        evaluate_url = f"{BASE_URL}/api/policies/evaluate"
        log_url = f"{BASE_URL}/api/policies/log"

        created_id = None
        if policy_description:
            create_resp = requests.post(
                policies_url,
                json={
                    "name": "test-policy",
                    "description": policy_description,
                    "priority": 9,
                    "active": True,
                },
                timeout=5,
            )
            create_resp.raise_for_status()
            created_id = create_resp.json().get("policy", {}).get("id")

        eval_resp = requests.get(evaluate_url, timeout=30)
        eval_resp.raise_for_status()
        eval_result = eval_resp.json()

        log_resp = requests.get(f"{log_url}?limit=10", timeout=5)
        log_resp.raise_for_status()
        recent_log = log_resp.json().get("entries", [])

        policies_resp = requests.get(policies_url, timeout=5)
        policies_resp.raise_for_status()
        policies = policies_resp.json().get("policies", [])

        if created_id:
            requests.delete(f"{policies_url}/{created_id}", timeout=5)

        context_block = json.dumps({
            "evaluation_result": eval_result,
            "recent_log": recent_log[-5:],
            "active_policies": [p for p in policies if p.get("active")],
        }, indent=2)

        prompt = f"""You are the Flow Work Intelligence Engine — a precise, senior engineering advisor.

The natural language rule engine has just evaluated all active policies against the current
backlog state. Each policy is expressed in plain English and evaluated by an LLM that decides
whether it fires, what actions to take, and why.

Key fields in the evaluation result:
- fires: number of policies that triggered
- actions_executed: number of actions applied to the backlog
- notifications: human-readable notices generated by the rule engine
- log_ids: IDs of log entries recording this evaluation run

In the recent_log, each entry has:
- policy_name: which rule was evaluated
- fired: whether it triggered (true/false)
- reasoning: the LLM's explanation of why it fired or didn't
- actions_proposed: structured actions the policy wanted to take
- actions_executed: actions that were actually applied

POLICY EVALUATION RESULT:
{context_block}

QUESTION:
{query}

Instructions:
- Respond in 3-5 sentences.
- Describe which policies fired and why, citing the LLM reasoning from the log.
- Note any actions that were executed or notifications raised.
- If no policies fired, explain what conditions were not met."""

        return _call_llm(prompt)

    except Exception as e:
        return f"Flow Error: {str(e)}"
