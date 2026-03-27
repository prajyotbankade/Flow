import json
import requests

BASE_URL     = "http://localhost:8089"
SCORES_URL   = f"{BASE_URL}/api/scores"
RECOMMEND_URL = f"{BASE_URL}/api/recommend"
OLLAMA_URL   = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen2.5-coder:14b"


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

SCORED BACKLOG (rank 1 = highest priority):
{context_block}

QUESTION:
{query}

Instructions:
- Respond in 2-3 sentences only.
- Lead with the recommended task title (rank 1).
- Cite the single most important non-zero score_breakdown field and its value.
- If a competitor task scored significantly lower, briefly note why (e.g. blocked_penalty)."""

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
