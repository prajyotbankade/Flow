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
- No persistent agent identity (`.claude/agents/` persona files)
- No Strategic lens — business value/impact is implicit, not scored
- Server start buried at bottom of SKILL.md
- `/api/agents` doesn't read from `.claude/agents/` definition files

---

## Task 0: Relocate Eval/Benchmark Files ✅

**Goal:** Move `flow_skill.py`, `test_flow_live.py`, and test result files from the project root into `skills/backlog-manager/evals/` so agents don't confuse benchmark infrastructure with the actual skill.

**Why:** `flow_skill.py` at the project root looks like the skill's main file. It's actually benchmark tooling that wraps API calls through Ollama for cheap eval runs. Keeping it at the root caused the lead agent to misidentify it as the skill itself — a mistake that would repeat for any new agent or conversation.

### Tasks
- [x] Move `flow_skill.py` to `skills/backlog-manager/evals/eval_flow_skill.py` (rename for clarity)
- [x] Move `test_flow_live.py` to `skills/backlog-manager/evals/test_flow_live.py`
- [x] Move `test_results_*.txt`, `test_results_summary.md`, `test_results_full.txt` to `skills/backlog-manager/evals/results/`
- [x] Update any imports or file paths inside the moved files
- [ ] Verify evals still run correctly from new location

---

## Task 1: Research `agent.md` Placement ✅

**Goal:** Determine where Claude recommends keeping agent team definitions so both the lead agent and sub-agents reliably read them.

**Why first:** Every other task depends on knowing where agent definitions live. Server integration (Task 2), SKILL.md references (Task 3), and persona files (Task 6) all need this answer.

### Decision: `.claude/agents/` (unified roster + persona)

Claude Code has a **native sub-agent system** — `.claude/agents/*.md` files with YAML frontmatter. Each file defines a sub-agent the lead can spawn by name.

**Key insight:** Instead of a separate `agent.md` roster + individual persona files, we unify them. Each agent gets one file in `.claude/agents/` where the YAML frontmatter IS the roster data (skills, capacity) and the markdown body IS the persona (identity, learnings).

**Format — `.claude/agents/<name>.md`:**
```markdown
---
name: backend-dev
description: Backend specialist — Python, APIs, databases, auth
skills: [python, api, database, auth]
complexity: [medium, high]
max_active: 2
---

## Persona
I am a backend specialist. I write minimal, correct code.
I prefer raw SQL over ORM for bulk operations.
I ask before making destructive changes.

## Learnings (max 10 items — one line each)
- Always use /api/pulse instead of separate API calls
```

**Why this works:**
- **Claude Code native** — `.claude/agents/` is where Claude Code looks for sub-agents. The lead agent spawns them by name.
- **Single file per agent** — frontmatter = roster data, body = persona. No separate roster file to maintain.
- **Server-parseable** — Python globs `.claude/agents/*.md`, parses YAML frontmatter, serves structured data via `/api/agents`.
- **Team roster is the directory** — `ls .claude/agents/` = your team. Adding an agent = adding a file.

**Impact on other tasks:**
- Task 2: Server reads `.claude/agents/*.md` (not a single `agent.md`)
- Task 3: Handoff protocol points to `.claude/agents/<name>.md`
- Task 6: No separate `agent.md` needed — the directory replaces it. Task 6 focuses on creating the initial agent files and defining the constrained persona template.

---

## Task 2: Update `/api/agents` to Read from `.claude/agents/` ✅

**Goal:** The server reads agent profiles from the same files the lead agent reads. Single source of truth for who's on the team, their skills, and their capacity.

**Why:** Currently agent data lives somewhere in the server config. `.claude/agents/*.md` is the canonical team definition — the server should consume it, not maintain a separate copy.

### Tasks
- [x] Glob `.claude/agents/*.md` and parse YAML frontmatter from each file
- [x] Map frontmatter fields (skills, complexity, max_active) to existing agent profile structure
- [x] Preserve backward compatibility — if `.claude/agents/` doesn't exist or is empty, fall back to current behavior
- [x] Update `/api/recommend` and `/api/pulse` to use `.claude/agents/`-sourced profiles
- [x] Re-read on each `/api/agents` request (no caching — files may change mid-session as agents update learnings)

---

## Task 3: SKILL.md Additions ✅

**Goal:** Add the operating guide that turns SKILL.md from a reference manual into an actionable playbook. No restructuring — just additions at the top.

**Why:** This is the single biggest blocker to agent usability. The building blocks exist but the step-by-step workflow doesn't.

### Additions

**3a. Operating Procedure (top of file)**
```
## How to operate
If the server is not running, start it before doing anything else.

1. Start server: python <skill-path>/scripts/backlog_server.py --file backlog.json
   - If already running, verify with a health check (GET /api/backlog)
2. Decompose the work into tasks — add via API with dependencies and links
3. Assign based on team (.claude/agents/) using assignment intelligence
4. Delegate to sub-agents with handoff protocol
5. Monitor via /api/pulse, act on _events after every write
6. After task completion, review sub-agent persona files — remove duplicates, merge similar learnings, trim anything now obvious from the codebase
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
5. Set priority_weight based on how much value this item unlocks — consider both dependency depth (items that unblock the most work) and business importance. If a Strategic focus is declared, weight items matching it higher
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
6. Their persona file path (.claude/agents/<name>.md) — agent reads it for identity and past learnings
7. Self-correction instruction: if you make a mistake and get corrected, update your persona file before finishing
```

### Tasks
- [x] Add operating procedure to top of SKILL.md (server-first line as literal first instruction)
- [x] Add precedence chain section
- [x] Add decomposition guidance section
- [x] Add sub-agent handoff protocol section
- [x] Move server start command from bottom to step 1 of operating procedure
- [x] Add persona pruning as step 6 in operating procedure

---

## Task 4: Implement Strategic Lens ✅

**Goal:** Add the 6th tribunal lens that evaluates business value and strategic alignment. Designed in Phase 1 plan but not implemented.

**Why:** Without it, the tribunal optimizes for flow and leverage but business value stays implicit. The scoring system has no way to say "this item matters most to the user/business even though it doesn't unblock anything."

### Design
The Strategic lens evaluates: "Is this item aligned with the current declared priority themes?"

**Scope:** Strategic is a **tribunal lens only**. It does not modify the raw priority score. This keeps raw scores deterministic and debuggable, while Strategic influence is visible in tribunal justifications (e.g., "recommended because it aligns with current focus: auth"). Strategic weight can be tuned independently without re-calibrating the scoring model.

Possible signals:
- `priority_weight >= 8` — human explicitly marked it high priority
- Tags matching a declared "focus area" in config (e.g., `current_focus: ["auth", "security"]`)
- Category alignment (e.g., bugs during a stability focus)

### Tasks
- [x] Add `strategic` lens weight to tribunal config (default: 1.0)
- [x] Add `current_focus` config field — list of tags/categories the team is currently prioritizing
- [x] Implement `evaluate_lens_strategic()` following the pattern of existing 5 lenses
- [x] Integrate into tribunal evaluation pipeline
- [x] Update SKILL.md with Strategic lens documentation
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
- [ ] Make `/api/pulse` the single source of truth — bundle policy evaluation results into the pulse response so agents make one call, not two
- [ ] Verify no functionality is lost after restructure

---

## Task 6: Agent Persona Files ✅

**Goal:** Create the initial team in `.claude/agents/` with constrained persona files. Each agent has a unified file (YAML frontmatter for roster data, markdown body for persona + learnings). Agents are responsible for updating their own learnings.

**Why:** Solves the memory propagation problem. Sub-agents today are born blank — no history, no personality, no lessons learned. Persona files give each agent a persistent identity that survives across conversations.

### Design

Per the Task 1 decision, each agent lives in `.claude/agents/<name>.md`. No separate roster file — the directory IS the team.

**Template — `.claude/agents/<name>.md`:**
```markdown
---
name: backend-dev
description: Backend specialist — Python, APIs, databases, auth
skills: [python, api, database, auth]
complexity: [medium, high]
max_active: 2
---

## Persona
I am a backend specialist. I write minimal, correct code.
I prefer raw SQL over ORM for bulk operations.
I ask before making destructive changes.

## Learnings (max 10 items — one line each, no narrative)
- Always use /api/pulse instead of separate API calls for recommendations

## Rules
- Before adding a learning, check if it's already captured. If it is, skip it.
- When I receive a correction, I update this file before finishing my task.
- If learnings reach 10, I consolidate or drop entries that are now obvious from the codebase.
```

**Persona constraints:**
- **Max learnings:** 10 one-line items. No narrative, no multi-line explanations. Forces prioritization — only behavior-changing learnings survive.
- **No duplicates:** Before adding a learning, check if it's already captured. If it is, skip it.
- **Total file size:** Keep persona files under 40 lines. If approaching the limit, consolidate or drop learnings that are now obvious from the codebase.
- **Format:** YAML frontmatter (structured, server-parseable) + markdown body (human-readable, agent-parseable).

**Self-correction protocol** — Part of every agent's Rules section:
> When you make a mistake or receive a correction during a task, update this file's Learnings before finishing. This is your responsibility — the lead agent won't track this for you.

**Leader pruning protocol** — Part of the lead agent's operating procedure (Task 3a, step 6):
> After a sub-agent completes a task, the lead agent reviews their persona file: remove duplicates, merge overlapping learnings, and trim anything that's now obvious from the code or docs. This keeps persona files clean and prevents bloat across conversations.

### Tasks
- [x] Create `.claude/agents/` directory
- [x] Create initial agent files for default team roles (backend-dev, frontend-dev, qa) using constrained template
- [x] Ensure each file has: YAML frontmatter (skills, complexity, max_active) + Persona + Learnings + Rules sections
- [x] Cap at 10 learnings, 40 lines total per file
- [x] Add self-correction and dedup rules to each agent's Rules section
- [x] Verify server (Task 2) correctly parses the created files
- [x] Document in SKILL.md how to customize the team (add/remove files in `.claude/agents/`)

---

## Execution Order

```
Task 0 (relocate eval files — removes confusion for all agents) ✅
  └─> Task 1 (research agent placement — decided: .claude/agents/) ✅
        └─> Task 2 (server reads .claude/agents/) ✅ + Task 3 (SKILL.md additions) ✅ + Task 4 (strategic lens) ✅
              └─> Task 6 (create agent persona files in .claude/agents/) ✅
                    └─> Task 5 (SKILL.md restructure — after everything stabilizes)
```

Task 0 ✅ — eval files relocated to `skills/backlog-manager/evals/`. Task 1 ✅ — decided on `.claude/agents/*.md` with YAML frontmatter (unified roster + persona). Tasks 2, 3, 4 can now run in parallel. Task 6 depends on Tasks 1-3. Task 5 is last — restructure after all new content is added.

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
