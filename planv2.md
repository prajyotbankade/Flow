# Flow v2: Agent-Ready Work Intelligence — Implementation Plan

> North Star: A human describes intent, the lead agent decomposes, assigns, and runs a team through Flow — with every agent learning from its own mistakes.

## Current State (as of 2026-03-28)

Phases 1-4 from plan.md are complete: tribunal justification, readiness signals, multi-agent coordination, and natural language rule engine. Flow is a fully functional Work Intelligence Engine.

But it's **not agent-usable yet**. The skill reads like a reference manual, not an operating guide. Sub-agents have no persistent identity, no memory of past corrections, and no handoff protocol. The Strategic lens (business value/impact alignment) was designed in Phase 1 but never implemented. The server start instruction is buried at the bottom of SKILL.md.

### What works
- 9-component scoring with dynamic readiness-scaled penalties
- Tribunal with 5 lenses (urgency, leverage, agent_fit, risk, momentum)
- Readiness signals with partial-unblock support
- Multi-agent coordination (pulse, graph, conflicts, rebalancing)
- Natural language policy engine with LLM evaluation
- Full REST API with optimistic versioning
- Web board with graph view, intelligence tab, policy settings

### What's missing
- No operating procedure — agents don't know the step-by-step workflow
- No precedence hierarchy — gates, readiness, tribunal, policies all described at same level
- No decomposition guidance — how to break features into tasks
- No sub-agent handoff protocol
- No persistent agent identity (`agent.md` / persona files)
- No Strategic lens — business value/impact is implicit, not scored
- Server start buried at bottom of SKILL.md
- `/api/agents` doesn't read from a shared agent definition file

---

## Task 1: Research `agent.md` Placement

**Goal:** Determine where Claude recommends keeping agent team definitions so both the lead agent and sub-agents reliably read them.

**Why first:** Every other task depends on knowing where agent definitions live. Server integration (Task 2), SKILL.md references (Task 3), and persona files (Task 6) all need this answer.

### Deliverable
- Research Claude Code documentation for recommended agent definition patterns
- Decide on file location and structure
- Document the decision

---

## Task 2: Update `/api/agents` to Read from `agent.md`

**Goal:** The server reads agent profiles from the same file the lead agent reads. Single source of truth for who's on the team, their skills, and their capacity.

**Why:** Currently agent data lives somewhere in the server config. If `agent.md` is the canonical team definition, the server should consume it — not maintain a separate copy.

### Tasks
- [ ] Parse `agent.md` on server startup and on each `/api/agents` request
- [ ] Map agent.md fields to existing agent profile structure (skills, max_active, preferred_complexity)
- [ ] Preserve backward compatibility — if no agent.md exists, fall back to current behavior
- [ ] Update `/api/recommend` and `/api/pulse` to use agent.md-sourced profiles

---

## Task 3: SKILL.md Additions

**Goal:** Add the operating guide that turns SKILL.md from a reference manual into an actionable playbook. No restructuring — just additions at the top.

**Why:** This is the single biggest blocker to agent usability. The building blocks exist but the step-by-step workflow doesn't.

### Additions

**3a. Operating Procedure (top of file)**
```
## How to operate
1. Start server: python <skill-path>/scripts/backlog_server.py --file backlog.json
2. If server is already running, verify with a health check (GET /api/backlog)
3. Decompose the work into tasks — add via API with dependencies and links
4. Assign based on team (agent.md) using assignment intelligence
5. Delegate to sub-agents with handoff protocol
6. Monitor via /api/pulse, act on _events after every write
```

**3b. Precedence Chain**
```
## Decision Hierarchy
Gates (hard stop) → Readiness (eligibility) → Tribunal (recommendation) → Policies (overrides)
- Gates prevent invalid status moves. Non-negotiable.
- Readiness determines if work is startable (>=70%) or fully ready (>=90%).
- Tribunal recommends what to do next based on 5 lenses.
- Policies can override or nudge — they fire after tribunal and can escalate, reassign, or block.
```

**3c. Decomposition Guidance**
```
## Decomposing a Feature
When the user describes a feature to build:
1. Identify distinct units of work (each should be completable by one agent)
2. Set dependencies — use "blocks" links for sequential work, no link for parallel work
3. Assign complexity: low (< 1 hour), medium (1-4 hours), high (4+ hours)
4. Set tags for skill matching (e.g., backend, frontend, auth, testing)
5. Set priority_weight based on dependency depth — items that unblock the most get highest weight
6. Add all items via API, dependencies first so links resolve correctly
```

**3d. Sub-Agent Handoff Protocol**
```
## Delegating to a Sub-Agent
When spawning a sub-agent for an assigned task, include in the prompt:
1. The task details (title, description, acceptance criteria from backlog)
2. Server URL: http://localhost:8089
3. Their agent name: use GET /api/backlog?agent=<name> to see only your work
4. Status protocol: set in-progress when starting, code-review or done when finishing
5. Blocker protocol: if blocked, open a thread via PUT /api/items/<id> and report back
6. Their persona file path (from agent.md) — agent reads it for identity and past learnings
7. Self-correction instruction: if you make a mistake and get corrected, update your persona file before finishing
```

### Tasks
- [ ] Add operating procedure to top of SKILL.md
- [ ] Add precedence chain section
- [ ] Add decomposition guidance section
- [ ] Add sub-agent handoff protocol section
- [ ] Move server start command from bottom to step 1 of operating procedure

---

## Task 4: Implement Strategic Lens

**Goal:** Add the 6th tribunal lens that evaluates business value and strategic alignment. Designed in Phase 1 plan but not implemented.

**Why:** Without it, the tribunal optimizes for flow and leverage but business value stays implicit. The scoring system has no way to say "this item matters most to the user/business even though it doesn't unblock anything."

### Design
The Strategic lens evaluates: "Is this item aligned with the current declared priority themes?"

Possible signals:
- `priority_weight >= 8` — human explicitly marked it high priority
- Tags matching a declared "focus area" in config (e.g., `current_focus: ["auth", "security"]`)
- Category alignment (e.g., bugs during a stability focus)

### Tasks
- [ ] Add `strategic` lens weight to tribunal config (default: 1.0)
- [ ] Add `current_focus` config field — list of tags/categories the team is currently prioritizing
- [ ] Implement `evaluate_lens_strategic()` following the pattern of existing 5 lenses
- [ ] Integrate into tribunal evaluation pipeline
- [ ] Update SKILL.md with Strategic lens documentation
- [ ] Add evals: strategic alignment scoring, focus area matching

---

## Task 5: SKILL.md Restructure

**Goal:** Trim redundancy, compress repeated ideas, improve information hierarchy. Separate from Task 3 — done after Tasks 1-4 are stable.

**Why:** The skill is powerful but dense. Overlapping explanations of scoring, readiness, tribunal, and policies make it more likely the model misses or partially follows instructions. The most important rules should be at the top.

### Tasks
- [ ] Audit SKILL.md for repeated concepts across sections
- [ ] Consolidate scoring explanation (currently in multiple places)
- [ ] Move API reference detail lower — keep behavioral intent at the top
- [ ] Ensure operating procedure (Task 3) remains the entry point
- [ ] Verify no functionality is lost after restructure

---

## Task 6: `agent.md` + Persona Files

**Goal:** Define a persistent team with individual agent identities. Each agent has a persona file that carries their skills, behavioral traits, and accumulated learnings. Agents are responsible for updating their own learnings.

**Why:** Solves the memory propagation problem. Sub-agents today are born blank — no history, no personality, no lessons learned. Persona files give each agent a persistent identity that survives across conversations.

### Design

**`agent.md`** — Team roster read by lead agent and server:
```markdown
# Team

## backend-dev
- Skills: python, api, database, auth
- Complexity: medium, high
- Max active: 2
- Persona: agents/backend-dev.md

## frontend-dev
- Skills: react, typescript, css, ui
- Complexity: low, medium
- Max active: 2
- Persona: agents/frontend-dev.md

## qa
- Skills: testing, api, e2e
- Complexity: low, medium
- Max active: 3
- Persona: agents/qa.md
```

**Persona files** — Each agent's identity + self-maintained learnings:
```markdown
# Backend Engineer

## My persona
I am a backend specialist. I write minimal, correct code.
I prefer raw SQL over ORM for bulk operations.
I ask before making destructive changes.

## My learnings
- flow_skill.py is benchmark infra, not the skill itself
- Always use /api/pulse instead of separate API calls for recommendations

## My file
This file: agents/backend-dev.md
When I receive a correction, I update this file before finishing my task.
```

**Self-correction protocol** — Part of every agent's persona:
> When you make a mistake or receive a correction during a task, update your persona file with the learning before reporting back. This is your responsibility — the lead agent won't track this for you.

### Tasks
- [ ] Research and decide `agent.md` location (Task 1 output feeds this)
- [ ] Define `agent.md` schema/format
- [ ] Create initial persona files for default team roles
- [ ] Add self-correction instruction to each persona
- [ ] Integrate persona file path into sub-agent handoff protocol (Task 3d)
- [ ] Document in SKILL.md how to customize the team

---

## Execution Order

```
Task 1 (research agent.md placement)
  └─> Task 2 (server reads agent.md) + Task 3 (SKILL.md additions) + Task 4 (strategic lens)
        └─> Task 6 (agent.md + persona files)
              └─> Task 5 (SKILL.md restructure — after everything stabilizes)
```

Task 1 is the prerequisite. Tasks 2, 3, 4 can run in parallel once placement is decided. Task 6 depends on Tasks 1-3. Task 5 is last — restructure after all new content is added.

---

## Design Principles (carried from plan.md + new)

1. **Justification is the interface** — every decision Flow makes must be explainable in plain English
2. **Partial readiness is first-class** — the world isn't binary; Flow shouldn't be either
3. **Push over pull** — Flow tells agents what's ready, agents don't ask
4. **Rules are dialogue** — disagreeing with a justification -> writing a new rule -> Flow adapts
5. **Decisions are objects** — tracked, reviewable, challengeable, and used for calibration
6. **Trust ratchet** — start with full oversight, earn autonomy through consistent good judgment
7. **Agents own their learnings** — corrections go in the agent's persona file, not the parent's memory
8. **The lead delegates, not micromanages** — pick the right agent, brief them, review the output
9. **Server is step 1** — nothing works without it, treat it as a prerequisite not a footnote
