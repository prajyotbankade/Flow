# Plan v3 — Smart Context Windowing, Tribunal Tiebreaker & Eval Scalability

> **Status**: Planning
> **Predecessor**: planv2.md (Phases 1-4 complete: tribunal, readiness, multi-agent, policies, strategic lens)
> **Trigger**: Eval suite runs 26/26 on OpenAI, 21/26 on Ollama 7b. Current prompts dump full API responses into LLM context. At 500+ backlog items, this blows context windows, wastes tokens, and kills latency. Tribunal also has no tie-breaking logic — identical scores produce arbitrary winners.

---

## Problem 1: Context Bloat

Every `run_flow_*` function sends the **entire** API response to the LLM:
- `run_flow()` — all scored items (500 items = ~50-100k tokens)
- `run_flow_tribunal()` — full tribunal JSON including all lenses for all candidates
- `run_flow_graph()` — all graph nodes, edges, conflicts, rebalancing
- `run_flow_policy()` — all policy evaluations, logs, active policies

This is wasteful because the LLM only needs a **relevant slice** to answer any given question.

## Problem 2: Tribunal Ties

When two items have identical tribunal scores, `sorted()` picks whichever appeared first in the dict — **arbitrary, not justified**. The tribunal reports "low confidence" but has no deliberate tiebreaker. With equal strategic alignment, equal priority, equal complexity — the winner is random.

Current code (backlog_server.py:989):
```python
ranked = sorted(tribunal_scores.items(), key=lambda x: x[1], reverse=True)
```

No secondary sort key. No tiebreaker reasoning in the output.

---

## Principles

1. **The context window should be query-driven, not data-driven.** Send the LLM only what it needs to answer the question.
2. **Slicing boundaries are data-driven, not hardcoded.** If 20 items are critical, include all 20. Don't cap at 10 because a constant says so.
3. **Ties must be broken deliberately and justified.** The tribunal should explain *why* it broke a tie, not silently pick one.

---

## Architecture: Context Slicer

A new module `context_slicer.py` sits between the server APIs and the LLM call. Each `run_flow_*` function calls the slicer instead of dumping raw JSON.

```
Server API → context_slicer.slice(intent, data, agents) → trimmed context → LLM prompt
```

### Intent Categories & Slicing Rules

| Intent | Detected by | Items to include | Boundary logic |
|--------|-------------|------------------|----------------|
| **recommendation** | "what next", "work on", "pick", "recommend" | Top N ranked where N = `active_agents × 2` (min 4) | Expand if items within 10% of Nth item's score |
| **conflict** | "conflict", "overlap", "coordination", "risk" | All `in-progress` items + their assigned agents | No cap — bounded by actual in-progress count |
| **critical_path** | "critical", "blocking", "cascade", "unblock" | All items with `is_critical_path=true` + their blockers/dependents | Follow the full chain, no arbitrary cutoff |
| **counterfactual** | "why not", "why wasn't", "instead", specific item name | The picked item + the named item from shadow_ranking | Just those 2-3 items |
| **readiness** | "readiness", "blocked", "startable", "penalty" | All items with blockers + their blocker items | Bounded by actual blocked count |
| **confidence** | "confident", "close", "margin", "consider both" | All items within 15% score margin of winner | Expands naturally with tight races |
| **policy** | "policy", "rule", "fire", "trigger" | Fired policies + affected items only | Bounded by actual fires |
| **general** | Fallback — no specific intent matched | Top 5 items + any conflicts + critical path summary | Expand if conflicts or critical items exist |

### Multi-Intent Handling

Questions often span intents: "What should I work on next? Also tell me about conflicts."

The slicer detects **all** matching intents, unions the item sets, and deduplicates. The prompt gets a combined context with clear section headers.

### Token Budget as Safety Net

If the data-driven slice is genuinely huge (50 critical items in a massive backlog), the token budget kicks in:
- Progressively **summarize** lower-ranked items (title + score only, drop breakdown)
- Never silently **drop** items — summarize instead
- Log a warning when summarization occurs

---

## Tasks

### Task 0: Context Slicer Module
**File**: `skills/backlog-manager/evals/context_slicer.py`

Build the core slicer:
- `detect_intents(query: str) -> list[str]` — keyword/pattern matching for intent categories
- `slice_scores(items, intents, agent_count) -> list[dict]` — trim scored items with data-driven boundaries
- `slice_tribunal(tribunal, intents, query) -> dict` — trim tribunal output
- `slice_graph(graph, pulse, intents) -> dict` — trim graph + pulse data
- `slice_policies(eval_result, log, policies, intents) -> dict` — trim policy data

Each slicer returns **only the fields the LLM needs** for the detected intents. Boundaries expand based on the data (e.g. all critical items, all conflicts) rather than hardcoded caps.

**Acceptance**: Unit tests — given 500 items and various queries, output includes all relevant items and excludes noise. Token count stays under budget.

### Task 1: Integrate Slicer into run_flow()
**File**: `skills/backlog-manager/evals/eval_flow_skill.py`

Replace:
```python
context_block = json.dumps(scored_items, indent=2)
```
With:
```python
context_block = slice_scores(scored_items, detect_intents(query), len(active_agents))
```

Keep the current prompt structure. Only the context shrinks.

**Acceptance**: Existing evals still pass. Token count per call drops proportionally to backlog size.

### Task 2: Integrate Slicer into run_flow_tribunal()
Trim tribunal output based on whether the question is about the pick, a counterfactual, or confidence.

**Acceptance**: Tribunal evals pass. Counterfactual queries include only the 2-3 relevant items.

### Task 3: Integrate Slicer into run_flow_graph()
Graph already uses pre-formatted text. Update to use slicer for node selection:
- Conflict queries → all in-progress nodes (no cap)
- Critical path queries → full critical chain + blockers
- Recommendation queries → startable candidates only

**Acceptance**: Graph evals pass. Large backlogs produce only relevant nodes in context.

### Task 4: Integrate Slicer into run_flow_policy()
Trim to only fired policies and their affected items.

**Acceptance**: Policy evals pass (when ANTHROPIC_API_KEY is set).

### Task 5: Tribunal Tiebreaker Logic
**File**: `skills/backlog-manager/scripts/backlog_server.py`

Add deliberate tiebreaker cascade in `evaluate_tribunal()`:

When two or more candidates have identical `tribunal_score`, break tie by:
1. **Lower complexity** — prefer quick wins when scores are equal
2. **Fewer reopens** — prefer items that haven't bounced back before
3. **Higher readiness** — prefer items closer to startable
4. **More recent activity** — prefer items with momentum (newer `updated_at`)
5. **Fewer skips** — prefer items that haven't been passed over

Add to tribunal output:
- `tie_broken: true/false` — flag when a tiebreaker was used
- `tiebreaker_reason: str` — e.g. "Tie broken: lower complexity (low vs medium)"

Update confidence logic: ties always produce `"low"` confidence with an explicit note.

**Acceptance**: Deterministic — same inputs always produce same winner. Tiebreaker reason appears in tribunal output.

### Task 6: Tiebreaker Eval Scenarios
**File**: `skills/backlog-manager/evals/test_flow_live.py`

Add eval scenarios:

- **Tribunal_Tie_Breaking_Complexity** — two items with identical priority, status, tags. One is `low` complexity, one is `medium`. Verify low-complexity wins and tiebreaker is cited.
- **Tribunal_Tie_Breaking_Readiness** — two items, same everything except one has higher blocker readiness. Verify readier item wins.
- **Tribunal_Tie_Breaking_Equal** — two truly identical items. Verify the tribunal still picks one deterministically and says "low confidence" with tiebreaker explanation.
- **Tribunal_Tie_Strategic_Alignment** — two items with equal scores but different strategic tags. When `current_focus` matches one, verify it wins on strategic lens (not a tie at all — tests that strategic lens prevents the tie).

**Acceptance**: All tiebreaker scenarios pass on OpenAI. Tribunal output includes `tie_broken` and `tiebreaker_reason` fields.

### Task 7: Scale Eval Scenarios
Add eval scenarios that test with large backlogs:
- Seed 100+ items, ask a recommendation question — verify response is correct AND context was trimmed
- Seed items with deep dependency chains — verify critical path query includes the full chain but not unrelated items
- Seed many in-progress items with overlapping tags — verify conflict detection scales

**Acceptance**: New scenarios pass on OpenAI. Token usage is documented per scenario.

### Task 8: Token Budget & Guardrails
Add a configurable token budget to the slicer:
- `MAX_CONTEXT_TOKENS` configurable via env var (default: 4000)
- If sliced context exceeds budget, progressively **summarize** lower-priority items (title + score only)
- Never silently drop items
- Log a warning when summarization occurs

**Acceptance**: No LLM call exceeds the token budget regardless of backlog size.

---

## Non-Goals (for this plan)

- **Changing the server APIs** — the server already returns everything we need. The slicer is client-side.
- **Caching** — worth doing later but orthogonal to context windowing.
- **Parallel test execution** — `pytest-xdist` is a separate optimization.
- **Changing the prompt structure** — planv3 only changes what data goes INTO the prompt, not how the prompt is written.

---

## Success Criteria

1. All 26+ eval tests pass on OpenAI after integration (no regression)
2. A 500-item backlog produces the same recommendations as today (correctness)
3. Token usage per generation call stays under budget regardless of backlog size
4. Each `run_flow_*` function's context is demonstrably query-relevant (no noise)
5. Tribunal ties are broken deterministically with justification in the output
6. Tiebreaker eval scenarios pass consistently

---

## Migration Path to Real Skill

The context slicer is designed to be reusable. When the eval harness becomes the real skill:
- `context_slicer.py` moves to the skill's core
- `_call_llm()` becomes the skill's LLM interface
- The same slicing logic keeps token costs predictable in production
- Tiebreaker logic is already in the server — no migration needed
