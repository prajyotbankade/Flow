import json
import requests

BASE_URL      = "http://localhost:8089"
SCORES_URL    = f"{BASE_URL}/api/scores"
RECOMMEND_URL = f"{BASE_URL}/api/recommend"
GRAPH_URL     = f"{BASE_URL}/api/graph"
PULSE_URL     = f"{BASE_URL}/api/pulse"
OLLAMA_URL    = "http://localhost:11434/api/generate"
OLLAMA_MODEL  = "qwen2.5-coder:14b"


def run_flow(query: str) -> str:
    """
    Ask the local Ollama LLM to recommend the top work item.
    The LLM receives the full score_breakdown for every item so it can
    cite exact numeric factors — not guess or template-fill.
    """
    try:
        scores_resp = requests.get(SCORES_URL, timeout=5)
        scores_resp.raise_for_status()
        scored_items = scores_resp.json().get("items", [])

        if not scored_items:
            return "No tasks available for recommendation."

        # Tag each item with its rank for the LLM
        for rank, item in enumerate(scored_items, start=1):
            item["rank"] = rank

        context_block = json.dumps(scored_items, indent=2)

        prompt = f"""You are the Flow Work Intelligence Engine — a precise, senior engineering advisor.

The backlog has been scored and ranked. Each item includes a full score_breakdown
showing exactly which factors contributed to the final score. Non-zero fields are
the active scoring signals. Trust the data — do not invent factors.

Each item also includes a readiness section: its own readiness score (0.0–1.0) plus
blocker readiness scores. blocked_penalty is now proportional to blocker readiness —
full penalty (-3.0) at 0% readiness, zero penalty at 100% readiness. A blocker at
75% readiness produces a blocked_penalty of only -0.75.

SCORED BACKLOG (rank 1 = highest priority):
{context_block}

QUESTION:
{query}

Instructions:
- Respond in 2-3 sentences only.
- Lead with the recommended task title (rank 1).
- Cite the single most important non-zero score_breakdown field and its value.
- If a competitor task scored significantly lower, briefly note why (e.g. blocked_penalty or low readiness)."""

        ollama_resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        ollama_resp.raise_for_status()
        return ollama_resp.json().get("response", "").strip()

    except Exception as e:
        return f"Flow Error: {str(e)}"


def run_flow_tribunal(query: str, agent: str = None) -> str:
    """
    Ask the local Ollama LLM to answer using the tribunal recommendation.
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

The tribunal has evaluated all eligible work items through 5 lenses (urgency, leverage,
agent_fit, risk, momentum) and produced a justified recommendation. The output includes:
- The picked item with reasoning, confidence, and supporting lenses
- A shadow ranking of runners-up with "why not" explanations (counterfactuals)

Trust the tribunal data — cite specific lens arguments and counterfactual reasons.

TRIBUNAL RECOMMENDATION:
{context_block}

QUESTION:
{query}

Instructions:
- Respond in 3-5 sentences.
- Lead with the recommended item and the tribunal's reasoning.
- Cite specific lens arguments (urgency, leverage, etc.) and their weights.
- If asked about a specific alternative, cite its shadow ranking entry and lost_reason.
- Mention the confidence level and what drove it."""

        ollama_resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        ollama_resp.raise_for_status()
        return ollama_resp.json().get("response", "").strip()

    except Exception as e:
        return f"Flow Error: {str(e)}"


def run_flow_graph(query: str, agent: str = None) -> str:
    """
    Ask the local Ollama LLM to answer using the dependency graph and pulse data.
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

        context_block = json.dumps({"graph": graph, "pulse": pulse}, indent=2)

        prompt = f"""You are the Flow Work Intelligence Engine — a precise, senior engineering advisor.

You have access to the full dependency graph and coordination pulse. Key data:
- nodes[].is_critical_path: true for items whose delay cascades to the most downstream work
- nodes[].cascade_count: how many items are transitively unblocked when this completes
- critical_path: ordered list of item IDs by cascade impact (highest first)
- conflicts: in-progress items sharing tags across different agents — potential coordination gaps
- rebalancing: suggestions when one agent is overloaded and another is idle with matching skills
- pulse.active_agents: who is working on what right now and their load percentage
- pulse.startable_items: items at ≥70% readiness that are not yet in-progress

Trust the graph data — cite specific cascade counts, conflict descriptions, and agent loads.

GRAPH + PULSE DATA:
{context_block}

QUESTION:
{query}

Instructions:
- Respond in 3-5 sentences.
- Cite specific cascade_count values, conflict descriptions, or agent loads from the data.
- If conflicts exist, surface them proactively.
- If items are on the critical path, explain the downstream impact.
- If rebalancing is needed, name the agents and describe the suggested transfer."""

        ollama_resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=60,
        )
        ollama_resp.raise_for_status()
        return ollama_resp.json().get("response", "").strip()

    except Exception as e:
        return f"Flow Error: {str(e)}"
