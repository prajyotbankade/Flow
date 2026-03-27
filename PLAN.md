# Flow: Making It Alive — Implementation Plan

> North Star: Flow should know what needs to happen next before any human or agent thinks to ask.

## Current State (as of 2026-03-27)

Flow is a **Work Intelligence Engine** with a fully implemented scoring engine, blocking system, event detection, agent affinity scoring, and a web board UI. But it's **passive** — agents don't know when dependent work is ready, and the rule engine is hardcoded.

### What's Already Built
- 9-component scoring formula (computed fresh per request, never persisted)
- 4 link types: `blocks`, `discovered-during`, `follow-up`, `related`
- 4 event types: `critical_bug_created`, `blocker_resolved`, `item_reopened`, `blocks_changed`
- Agent affinity scoring (skills, complexity preference, linked-item history, load cap)
- Lane gate rules with 3-layer enforcement (UI, API, agent)
- Append-only lane history + gate watermark
- REST API with optimistic versioning (409 on stale writes)
- Web board with drag-and-drop, score badges, filtering, settings
- Work briefs generated conversationally via SKILL.md
- Threading for refinement Q&A
- Model routing (complexity → haiku/sonnet/opus)

### What's Missing
- No justification for why a task was picked (just a score number)
- Blocking is binary (blocked/not blocked), no nuance
- Agents are isolated filtered views, no awareness of each other
- No push mechanism (everything is pull)
- Rules are hardcoded JSON gates, not intelligent
- No decision tracking or outcome feedback loops

---

## Phase 1: Justification Engine

**Goal:** Every work recommendation comes with a human-readable explanation of WHY it was picked, what was considered, and why alternatives were passed over.

**Why first:** Scoring data already exists. This is an LLM interpretation layer on top of existing `/api/scores` output. Highest leverage, lowest effort. Creates the Decision object that becomes the feedback loop for everything else.

### Core Concepts

**Tribunal Model** — Multiple evaluation lenses each argue their case, final layer adjudicates:

| Lens | Argues For |
|------|------------|
| Urgency | Time-sensitive items, critical bugs, deadlines |
| Leverage | Items that unblock the most downstream work |
| Agent Fit | Items best matched to available agent skill/affinity |
| Risk | Items whose delay creates the most downstream damage |
| Momentum | Items already in motion, partial progress exists |
| Strategic | Items aligned with current declared priority themes |

**Counterfactual Reasoning** — For every pick, Flow maintains a shadow ranking of all considered candidates with the reasoning they lost on. "Why not item B?" is always answerable.

**Decision Object** — Every pick is a first-class object:
```
Decision {
  picked: item_id
  timestamp: ISO 8601
  lenses_evaluated: [{ lens, argued_for, argument, weight }]
  verdict: { winner, reasoning, confidence }
  dissent: [{ lens, argued_for, argument }]
  shadow_ranking: [{ item_id, score, lost_on_lens, lost_reason }]
  outcome: null  // filled when work completes — was this a good pick?
}
```

**Justification Narrative** — Not a score table. A readable explanation:
> **Picked: "Auth token refresh bug"**
> *Primary reason:* Unblocks 3 stalled agents. Resolving it recovers ~6 agent-hours.
> *Why not "Onboarding UI polish" (score: 91)?* No downstream deps, assigned agent would context-switch.
> *Why not "Analytics dashboard" (score: 88)?* Low alignment with current stability focus.
> *Confidence: High.* All lenses agreed.

### Tasks
- [x] Design Decision object schema
- [x] Build tribunal evaluation — 5 lenses (urgency, leverage, agent_fit, risk, momentum) with weighted scoring
- [x] Build counterfactual generator — shadow ranking with "why not" for top 4 runners-up
- [x] New endpoint: `GET /api/recommend[?agent=name&commit=true]` — returns top pick + justification + shadow ranking
- [x] Store Decision objects (`decisions.json`, capped at 100, via `?commit=true`)
- [x] Outcome tracking — auto-records when picked item moves to done (both PUT paths)
- [x] Update SKILL.md work brief format to use justification narratives
- [x] Update web board Intelligence tab to show tribunal verdict, lenses, and counterfactuals
- [x] Update schema.md with Decision object, recommend response, lens docs
- [x] Add evals: tribunal lens citation, counterfactual reasoning, low-confidence detection, agent-specific recommendation

---

## Phase 2: Readiness Signals

**Goal:** Replace binary blocked/unblocked with a confidence-scored readiness spectrum. Items can be "80% ready" and agents can choose to start with known risk.

**Why second:** Builds on Phase 1 — justification engine can now explain readiness context. Without justification, a readiness score is just another number.

### Core Concepts

**Three signal layers (increasing trust):**

| Signal Type | Source | Trust Level |
|-------------|--------|-------------|
| Status change | Human/agent marks done | Low — intent, not proof |
| Artifact produced | PR merged, file written, spec committed | Medium — evidence exists |
| Downstream gate passed | Tests green, review approved, dep validated | High — work is actually ready |

**Readiness Score** — Weighted composite of signal layers. Replaces the flat `blocked_penalty` of -3.0.

**Partial readiness as first-class:** An item at 70% readiness surfaces in work briefs as "startable with risk." Agent can begin exploratory work early and switch to committed work when readiness hits 90%+.

### Tasks
- [x] Define readiness score computation model (signal weights, thresholds)
- [x] Extend item schema: `readiness_signals[]` — array of observed signals with type, source, timestamp
- [x] Replace flat `blocked_penalty` with dynamic penalty based on blocker's readiness score
- [x] Define artifact signal types (PR merged, file created, test passed, spec written)
- [x] Build signal detection hooks — `POST /api/items/<id>/signal` for agents to record observed artifacts
- [x] Update `/api/scores` to include readiness scores per item
- [x] Integrate with justification engine — readiness context in pick explanations
- [x] Update work brief format: show readiness % and risk level for partially-ready items
- [x] Define readiness thresholds in config (e.g., 70% = startable, 90% = fully ready)
- [x] Add evals: readiness-scaled blocked_penalty, partial unblock startability, signal recording workflow

---

## Phase 3: Multi-Agent Coordination

**Goal:** Agents are aware of each other. Flow pushes work to agents when dependencies resolve. Conflicts detected before they happen, not at merge time.

**Why third:** Builds on Phase 2 (pulses fire on readiness changes) and Phase 1 (every pulse comes with justification).

### Core Concepts

**Assignment Pulse** — When readiness crosses a threshold, Flow proactively pushes a recommendation to the best-fit agent. Agents subscribe to dependency events, not poll.

**Live Dependency Graph** — Not just links between items, but a computed view of the full unblocking cascade. "If A finishes → B and C unlock → which then unlocks D." Visualize the critical path.

**Intent Conflict Detection** — Before work begins:
- Two agents about to touch the same module/tags → warning
- One agent's plan contradicts another agent's current output → intervention
- New critical item would invalidate work already in progress → alert

### Tasks
- [x] Design the pulse mechanism — polling with bundled coordination context (`GET /api/pulse`)
- [x] Build dependency cascade computation — full graph traversal, not just direct links (`compute_critical_path`)
- [x] New endpoint: `GET /api/graph` — returns the live dependency graph with readiness scores
- [x] Build conflict detection — tag/area overlap analysis across in-progress items (`detect_conflicts`)
- [x] Assignment pulse endpoint: `GET /api/pulse?agent=name`
- [x] Pulse payload: item + justification + readiness context + conflict warnings + active agents
- [x] Critical path visualization — which items, if delayed, delay the most downstream work (`is_critical_path`, `cascade_count`)
- [x] Workload rebalancing suggestions — when one agent is overloaded and another is idle (`compute_workload_rebalancing`)
- [x] Update web board: graph view with SVG DAG, agent activity panel, conflict badges on cards, view toggle
- [x] Add evals: dependency cascade correctness, conflict detection, pulse payload content, critical path identification

---

## Phase 4: Natural Language Rule Engine

**Goal:** Replace hardcoded gate rules with natural language policies evaluated by an LLM. Vibe coders write rules in English, Flow enforces with judgment.

**Why last:** Needs all three layers beneath it. Justification (so rules explain their impact), readiness (so rules have rich context), coordination (so rules can trigger actions like reassign/escalate).

### Core Concepts

**Rules as Prompts** — A rule is a natural language statement of intent:
- "If a bug is blocking more than 2 active items and was filed in the last 6 hours, jump it to the top."
- "Never assign two high-complexity items to the same agent simultaneously."
- "If something has been skipped 3 times, force it to the top."

**Contextual Evaluation** — Rules are evaluated against live graph state on every state change. The same rule may behave differently based on time, load, and system state.

**Structured Actions** — LLM doesn't return prose. It returns structured actions: `reassign`, `escalate`, `block`, `unblock`, `notify`, `reprioritize`. Flow executes these deterministically.

**Rule Conflict Resolution** — When two rules contradict, LLM adjudicates based on priority and context. Resolution is logged for transparency.

**Rule Learning** — Every rule application is an outcome data point. Over time Flow surfaces:
- "This rule fires constantly but never changes outcomes — may be redundant."
- "This rule hasn't fired in 3 weeks — still relevant?"

### Tasks
- [ ] Design policy schema — name, description (natural language), priority, active/inactive, created_at
- [ ] Policy storage in config (extend `backlog.json` config or separate `policies.json`)
- [ ] Build evaluation pipeline — on graph state change, feed snapshot + policies to LLM
- [ ] Define structured action types and their execution logic
- [ ] Build rule conflict resolution — detect contradictions, LLM adjudicates, log reasoning
- [ ] Migrate existing gate rules — express current `requires` rules as natural language policies
- [ ] Rule authoring UX — in SKILL.md conversation and/or web board settings
- [ ] Rule effectiveness tracking — fire count, outcome impact, staleness detection
- [ ] Rule suggestion engine — based on patterns Flow observes, suggest new rules
- [ ] Update justification engine — include which rules influenced a decision
- [ ] Add evals: natural language rule firing, conflict resolution, rule authoring via conversation, action execution correctness

---

## The Compound Effect

| Phase | Flow goes from... | ...to |
|-------|-------------------|-------|
| 1. Justification | "Score: 87" | "Pick this because it unblocks 3 items and the best-fit agent is free" |
| 2. Readiness | "Blocked: yes/no" | "80% ready — PR merged, tests pending" |
| 3. Coordination | "Agent pulls next task" | "Flow pushes work to the right agent at the right moment" |
| 4. Rules | "Hardcoded gates" | "User writes policy in English, Flow enforces with judgment" |

After all four phases, a vibe coder describes intent and Flow decomposes, sequences, assigns, monitors, justifies, and adapts. The coder's only job is reviewing decisions they disagree with.

---

## Design Principles

1. **Justification is the interface** — every decision Flow makes must be explainable in plain English
2. **Partial readiness is first-class** — the world isn't binary; Flow shouldn't be either
3. **Push over pull** — Flow tells agents what's ready, agents don't ask
4. **Rules are dialogue** — disagreeing with a justification → writing a new rule → Flow adapts
5. **Decisions are objects** — tracked, reviewable, challengeable, and used for calibration
6. **Trust ratchet** — start with full oversight, earn autonomy through consistent good judgment
