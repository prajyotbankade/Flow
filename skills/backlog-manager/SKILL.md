---
name: backlog-manager
description: Manage a project backlog — add, prioritize, refine, and pick up work items. Use when the user mentions backlog, tasks, sprints, or work queues. Also triggers proactively after task completion and when bugs or tech debt are discovered during work.
---

# Backlog Manager

## Activation Hints

Invoke this skill when the user:
- Mentions backlog, sprint planning, task queue, work items, story grooming, or agile workflow
- Says things like "add this to the list for later", "what should I work on next", "let me park this idea", "queue this up", "what's in my backlog", "link this to #3", "which agent should take this", or "what's blocking progress"
- Asks about prioritizing, refining, or picking up work

Invoke proactively (without being asked) when:
- You've just finished a task and the user hasn't given you a new one → generate a work brief and offer to pick up the top-scored item
- You discover bugs, tech debt, or follow-ups while working → add them to the backlog and link them to the source task
- An item enters or is already in `code-review` status (after any write, or when scanning the backlog) → for each `code-review` item with no unresolved `review-dispatched` sentinel thread: (1) run `backlog handoff reviewer --item N --review`, (2) immediately run `backlog ingest <result_file>` on the file it prints ("Result saved to …"). Do not wait for the user to ask. The ingest step is what actually advances the item to `done` (pass) or back to `in-progress` (reject) — skipping it leaves the item stuck in `code-review`.

---

## Setup

The skill ships as an installable Python package. Install once per machine:

```bash
cd <skill-path>
pip install -e .
export BACKLOG_FILE=/path/to/backlog.json   # or pass --file on every command
```

> **Backup:** Copy `backlog.json` before upgrading the skill or making bulk edits. There is no automatic migration.

This gives you two entry points:
- **`backlog`** — CLI for agents and terminal users (no server required)
- **`backlog-server`** — HTTP server for the web Kanban board

**Which interface to use:**
- Agents and scripts → `backlog` CLI
- Visual management → `backlog board` (or `backlog-server --file ...`)
- Natural language → Claude skill (this file)

## How to Operate

Agents do not need the server running. Use the CLI directly.

1. **Pick up work:** `backlog pick <your-agent-name>` — moves top ready item to in-progress
2. **Decompose** the work into tasks — add via CLI or API with dependencies and links
3. **Assign** based on team (`.claude/agents/`) using assignment intelligence
4. **Delegate** to sub-agents with handoff protocol (see Delegating to a Sub-Agent below)
5. **Monitor** via `/api/pulse` (if server running) or `backlog list` — act on `_events` after every write. Use `/api/graph` to surface dependency order and critical path before assigning work.
6. **Prune personas** — after task completion, review sub-agent persona files: remove duplicates, merge similar learnings, trim anything now obvious from the codebase

**Start the web board** (human visual use only):
```bash
backlog board                          # uses BACKLOG_FILE, port 8089
backlog-server --file backlog.json     # explicit path, same thing
```

## Decision Hierarchy

```
Gates (hard stop) → Readiness (eligibility) → Tribunal (recommendation) → Policies (overrides)
```

- **Gates** prevent invalid status moves. Non-negotiable.
- **Readiness** determines if work is startable (>=70%) or fully ready (>=90%).
- **Tribunal** recommends what to do next based on 6 lenses (urgency, leverage, agent_fit, risk, momentum, strategic).
- **Policies** can override or nudge — they fire after tribunal and can escalate, reassign, or block.

## Decomposing a Feature

When the user describes a feature to build:

1. Identify distinct units of work (each should be completable by one agent)
2. Set dependencies — use `blocks` links for sequential work, no link for parallel work
3. Assign complexity: `low` (< 1 hour), `medium` (1-4 hours), `high` (4+ hours)
   - Model routing: `low` → haiku · `medium` → sonnet · `high` → opus (advisory — shown in work brief). The spawning agent is responsible for selecting the model when delegating to a sub-agent.
4. Set `tags` for skill matching (e.g., backend, frontend, auth, testing)
5. Set `priority_weight` based on how much value this item unlocks — consider both dependency depth (items that unblock the most work) and business importance. If a Strategic focus is declared, weight items matching it higher
6. Add all items via API, dependencies first so links resolve correctly

## Code Review Protocol

When reviewing an item in `code-review`, a **reject verdict blocks the merge**. Do not pass and log a follow-up ticket — if you can see the bug, it must be fixed before done.

**Review checklist:**

1. **Correctness** — does the implementation match the acceptance criteria exactly?
2. **Idempotency** — any code that runs in a loop, on a recurring tick, or in response to repeated events must be safe to call multiple times without side effects. If it dispatches, writes, or creates something, check it guards against doing that twice.
3. **Error handling** — failures must be graceful. Silent failures that cause the caller to retry infinitely are bugs, not acceptable behavior.
4. **Edge cases** — what happens when the list is empty, the file doesn't exist, the agent isn't configured, or the external call times out?
5. **Write safety** — any write to `backlog.json` must re-read first, use `expected_version`, and go through `apply_lane_transition` for status changes (never raw field writes).

**Severity tiers** — classify every finding before issuing a verdict:
- **blocker** — must fix before merge (correctness bug, security issue, data loss, broken contract)
- **important** — real pain if it ships; gets a follow-up thread but does not block
- **nit** — style/naming/minor inconsistency; mentioned once, never blocks

**Verdict:**
- `pass` — zero blockers. Important issues and nits are noted in the review artifact and follow-up threads.
- `reject:<blocker summary>` — one or more blockers found. Move item back to `in-progress` with a thread listing only the blockers and exactly what must change.

**Review artifact** — write `handoff_results/review_<item_id>_<timestamp>.md` after every review (pass or reject):
```
# Review: <item title>
**Item**: #N  **Verdict**: pass | reject  **Reviewer**: reviewer  **Timestamp**: <ISO>
## Blockers / ## Important / ## Nits / ## Praise   (omit empty sections)
```

A reviewer who passes code with a known bug and logs a follow-up ticket has failed at their job.

---

## Delegating to a Sub-Agent

When spawning a sub-agent for an assigned task, include in the prompt:

1. The task details (title, description, acceptance criteria from backlog)
2. Server URL: `http://localhost:8089`
3. Their agent name: use `GET /api/backlog?agent=<name>` to see only your work
4. Status protocol: set `in-progress` when starting, `code-review` or `done` when finishing
5. Blocker protocol: if blocked, open a thread via `PUT /api/items/<id>` and report back
6. Their persona file path (`.claude/agents/<name>.md`) — agent reads it for identity and past learnings
7. Self-correction instruction: if you make a mistake and get corrected, update your persona file before finishing

---

## CLI Reference

```bash
# File resolution: --file flag > BACKLOG_FILE env var > error
export BACKLOG_FILE=/path/to/backlog.json

backlog list                          # board grouped by lane
backlog list --status ready           # filter by lane
backlog list --assigned-to alice      # filter by assignee
backlog list --json                   # machine-readable output

backlog show 3                        # full detail for #3
backlog show 3 --json                 # machine-readable detail

backlog add "Title"                   # add to backlog
backlog add "Title" --priority high --complexity low --tags "auth,backend"
backlog add "Title" --description "Details" --priority-weight 8 --category bug --assigned-to alice

backlog move 3 in-progress            # lane transition (gate rules enforced)
backlog done 3                        # move to done
backlog assign 3 --to alice
backlog unassign 3

backlog pick alice                    # pick top ready item → in-progress
backlog pick alice --json             # machine-readable picked item
backlog discard 3                     # always allowed from any lane
backlog restore 3                     # always goes back to backlog

backlog edit 3 --title "New title"
backlog edit 3 --description "Updated details" --priority high --complexity low
backlog edit 3 --priority-weight 9 --category bug --tags "auth,backend" --assigned-to alice

backlog init                          # create starter backlog.json
backlog board                         # launch web board (port 8089)
backlog board --port 9000             # custom port

backlog handoff reviewer --item 3 --review   # review handoff: pass/reject verdict
backlog handoff backend-dev --item 3         # work handoff: done/blocked/partial

backlog orchestrate                   # supervised mode (default): acts on ready+ items
backlog orchestrate --mode auto       # auto mode: lead agent refines + starts items autonomously
backlog orchestrate --poll 30         # tick interval in seconds (default 10)
backlog orchestrate --once            # single tick and exit
backlog orchestrate --dry-run         # print actions without invoking agents
```

**Exit codes:** `0` success · `1` gate violation / not found / validation error · `2` version conflict (re-read and retry)

---

Data store: `backlog.json` at project root. Always re-read before writing. Increment `version` on every write.

Status flow: `backlog → refined → ready → in-progress → code-review → done` (`discarded` from any lane, no gates).

Items referenced by position: `#1` = index 0, `#2` = index 1, etc.

## Pulse — Primary Agent Interface

Call `GET /api/pulse?agent=<your-name>` as your single source of truth. One call replaces `/api/recommend`, `/api/scores`, `/api/agents`, `/api/policies`, and `/api/policies/log`.

**Response fields:**
- **recommendation**: Tribunal pick with justification, confidence, supporting lenses, shadow ranking of runners-up
- **startable_items**: Items at >=70% readiness available now (max 5)
- **conflicts**: In-progress items in your area (tag overlap with other agents) — check before starting
- **rebalancing**: Suggestions if load is uneven across agents
- **active_agents**: Who is working on what, current load percentage
- **policies**: Policy intelligence:
  - `active_count` — number of active policies
  - `recent_fires` — last 5 policy fires with reasoning and action types
  - `notifications` — recent policy notifications (no state change, just alerts)
  - `influences_on_pick` — policy actions that affected the recommended item
  - `stale_warnings` — policies that may be redundant or outdated

## Work Brief Format

```
NEXT:  #N — Title  [score: X | confidence: high]
       Why: Primary reasoning from tribunal lenses
       Assign: agent-name (skill match: auth, backend) | Model: sonnet
       [Startable with risk — weakest blocker at 74% readiness (PR merged, tests pending)]

       Considered but passed:
       · #M — Title [score: Y] — Why not: not ready (status: backlog)
       · #K — Title [score: Z] — Why not: Blocked — weakest blocker at 40% readiness

STARTABLE: Items 70–89% unblocked (can begin exploratory work with known risk):
       · #P — Title — Blocker at 75% (in-progress, PR merged)

WATCH: stale items · skip-escalated items · 3+ reopens in same tag area
UNASSIGNED CRITICAL: critical bugs unassigned > threshold hours
```

Generate after: task completion, reprioritization trigger fires, user asks "what's next?". Use `/api/pulse` to get the data.

## Reprioritization Triggers

Check `_events` in every write response:

| Event | Action |
|---|---|
| `critical_bug_created` | Re-score all. If top-ranked: announce and offer to start. |
| `blocker_resolved` | Re-score unblocked items. Highlight rank jumps. |
| `item_reopened` | Flag: "Reopened (#N). Complexity may be underestimated." At 3+: suggest redesign. |
| `blocks_changed` | Re-score newly blocked items. |

Staleness (agent-side): flag `refined` > 7d or `ready` > 5d in WATCH.
Skip escalation: increment `skip_count` for items scored but not picked. At 5: escalate. Reset to 0 on pick-up.
Cluster detection: 3+ reopens in same tag area within 14d → flag in WATCH.

---

## Reference

### Operations

**Add** — Append to end, `status: backlog`, generate 8-char random alphanumeric ID (lowercase letters + digits; not UUID-based, never reused). Default `priority_weight = 5` if not set. Infer `category`, `complexity`, `tags`, `priority_weight` from context — don't ask. Confirm: "Added as #N (category, complexity, [tags])."

**List** — Group by status: `#N title [score: X] (assigned, complexity, category) — flags`

**Edit** — Update any field. Note scoring impact if relevant (e.g., setting `complexity: high` drops score and routes to opus).

**Assign** — Set `assigned_to`. Unassigned = available to all agents.

**Reorder** — Reorder `items` array. Reordering ≠ status change.

**Discard** — `status: discarded`. No gate rules. Restore: move to any lane, resets `gate_from`.

**Complete** — `status: done`. Add brief completion note. Offer next item from ready queue.

**Refine** — Open threads for unclear items (max 2 questions at a time). Resolve threads → suggest `refined`. User decides `ready`. Always scan for `waiting_on: "agent"` threads — respond and set to `"user"` or null.

**Block** — Open thread, `waiting_on: "user"`, move back to `backlog`. Notify user.

**Pick Up** — Score all items → work brief → confirm with user → set `in-progress` + `assigned_to`. Use `/api/pulse` for recommendation.

**Agent-initiated items** — Add bugs/tech-debt found while working. Set `category`, `complexity`, `tags`, `priority_weight` (bugs only). Link back to source. Announce: "Found X while on #N — added as #M (bug, medium, [tags])."

### Linking

```json
{ "item_id": "abc12345", "type": "blocks", "reason": "one sentence why" }
```
Types: `blocks`, `discovered-during`, `follow-up`, `related`. `reason` is required on every link.

### Scoring

```
score = base_priority
      + unblock_weight × (# items this blocks)
      + freshness
      + complexity_bonus
      + blocked_penalty        (dynamic: scales with blocker readiness)
      + quick_win_bonus        (low complexity + blockers ≥ ready_threshold)
      + reopen_count × reopen_penalty_per
      + skip_count × skip_floor_per
      + critical_bug_boost     (category=bug AND priority_weight ≥ 9)
```

`base_priority`: `priority_weight × factor`. All items receive `priority_weight = 5` at creation if not explicitly set, ensuring consistent scoring across mixed backlogs. `blocked_penalty` scales with blocker readiness — a blocker at 70% applies only 30% of the penalty; a fully-done blocker applies 0. All weights configurable via `config.scoring`.

### Readiness Signals

Each item has a **readiness score** (0.0–1.0) derived from status baseline + artifact signals:

| Status baseline | `backlog`=5% | `refined`=20% | `ready`=35% | `in-progress`=50% | `code-review`=70% | `done`=100% |

Signal types (additive):

| Type | Trust | Meaning |
|---|---|---|
| `spec_written` | +10% | Design intent documented |
| `file_created` | +10% | Code artifact exists |
| `design_approved` | +15% | Design gate passed |
| `test_passed` | +20% | Downstream gate cleared |
| `pr_merged` | +25% | Integration artifact complete |
| `review_approved` | +25% | Peer review gate passed |

**Readiness levels:** `not_ready` (< 70%) · `startable` (70–89% — can begin with known risk) · `ready` (>= 90%)

**Add a signal:** `POST /api/items/<id>/signal` with `{"type": "pr_merged", "source": "agent-name", "description": "optional note"}`

### Tribunal

Evaluates eligible items through 6 weighted lenses. Access via `/api/pulse` (preferred) or `GET /api/recommend[?agent=name&commit=true]`.

| Lens | Weight | Evaluates |
|---|---|---|
| urgency | 1.0 | Time-sensitivity, criticality, staleness |
| leverage | 1.2 | Downstream unblock cascade |
| agent_fit | 0.8 | Skill match, complexity preference, load, history |
| risk | 1.0 | Blocking impact, reopens, skip neglect |
| momentum | 0.6 | Status progression, recent activity |
| strategic | 1.0 | Business value alignment with declared focus areas |

**Strategic lens** — tribunal-only, does not modify raw scores. Configure `config.strategic.current_focus` with tags/categories the team is prioritizing (e.g., `["auth", "security"]`). Items with matching tags, high `priority_weight` (>=8), or category alignment score higher. Justifications cite the signal.

Use `?commit=true` on `/api/recommend` to store decisions for outcome tracking.

### Orchestrator

`backlog orchestrate` is a persistent process that drives the dev cycle forward. It runs in two modes:

**Supervised (default):** Orchestrator only acts on `ready`+ items. Human controls when work starts by moving items to `ready`. Orchestrator handles execution, review, and result ingestion from there.

**Auto:** A designated lead agent continuously picks the highest-priority `backlog`/`refined` item, assesses whether it's actionable (via Claude), and either moves it to `ready` or opens a refinement thread (`waiting_on: "user"`) with at most 2 blocking questions. Human answers questions; lead agent resumes. Items already waiting for human input are skipped.

**Config:**
```json
"orchestrator": {
  "mode": "supervised",     // "supervised" (default) or "auto"
  "require_review": true    // false disables peer review gate (logged warning at startup)
}
```

**Lead agent:** Set `"role": "lead"` on exactly one agent in `config.agents`. Required for auto mode; validated at startup. Zero or multiple leads → hard error with names listed.

**Reviewer agent:** Set `"role": "reviewer"` on the designated code review agent. When the orchestrator dispatches a review handoff, `_select_agent` gives this agent a +10 score bonus so it wins over generalist agents unless at capacity.

```json
"agents": {
  "lead-dev": { "role": "lead", "skills": ["python", "api"], "max_active": 1 },
  "worker-a":  { "skills": ["python", "api"], "max_active": 2 }
}
```

**Honesty rule (both modes):** Agents must stop and ask the human rather than guess when context is insufficient. Autonomy does not mean guessing.

### Assignment Intelligence

- +2 per tag match (item `tags` ∩ agent `skills`)
- +3 if agent worked on a linked item (`assigned_to` or `lane_history.by`)
- +1 if item `complexity` in agent's `preferred_complexity`
- −5 if agent at/above `max_active`

Highest positive affinity wins. Tie or no positive affinity → ask user.

### Agent Team (`.claude/agents/`)

The team roster lives in `.claude/agents/*.md`. Each file defines one agent with YAML frontmatter (skills, capacity) and a markdown body (persona, learnings, rules). The server reads these files on every request — no restart needed.

**To add an agent:** Create `.claude/agents/<name>.md` with this template:
```markdown
---
name: <agent-name>
description: <one-line role description>
skills: [tag1, tag2, ...]
complexity: [low, medium, high]
max_active: <int>
---

## Persona
<2-3 sentences: who you are, how you work, what you prefer>

## Learnings (max 10 items — one line each, no narrative)

## Rules
- Before adding a learning, check if it's already captured. If it is, skip it.
- When I receive a correction, I update this file before finishing my task.
- If learnings reach 10, I consolidate or drop entries that are now obvious from the codebase.
```

**To remove an agent:** Delete the file. **To modify:** Edit the file directly — changes take effect on next API call.

**Persona constraints:** Max 10 learnings, max 60 lines per file. The lead agent prunes personas after task completion (step 6 of How to Operate).

### Lane Gate Rules

Each status can have `requires: [lane_ids]` — item must have passed through those lanes (from `gate_from` onward) before entering. Backward moves always allowed.

- `lane_history`: append-only. Format: `{"lane": "<old_status>", "at": "<ISO UTC>", "by": "<actor>"}`. `by` = agent name, `"user"`, or `"unknown"` (legacy entries). Bare-string entries from older data are normalized to `{"lane": ..., "at": null, "by": "unknown"}` on every read — callers always see structured dicts.
- `gate_from`: watermark index. On backward move: append current lane, set `gate_from = len(lane_history)`.
- Before any forward move: verify target's `requires` against `lane_history[gate_from:]`. If missing: "Can't move to X — requires Y first."
- Server enforces gates (HTTP 422). Check client-side too.

**Backward-then-forward example:**
An item progresses `backlog → ready → in-progress`, then gets moved back to `ready`.
On the backward move, `gate_from` resets to the current length of `lane_history`.
The earlier `in-progress` entry is now before `gate_from` and is no longer counted as an earned gate.
Attempting to move to `code-review` (which requires `in-progress`) will be blocked — even though `in-progress` appears in `lane_history`.
The item must pass through `in-progress` again before `code-review` becomes reachable.

### Natural Language Rule Engine

Policies are plain English rules. When a policy has a `conditions` + `action` block, the server evaluates it with pure Python — **no API key required**. Policies without `conditions` fall back to LLM evaluation (requires `ANTHROPIC_API_KEY`).

**When creating a policy, always compile the natural language description into structured `conditions` + `action` fields.** This makes the policy free to evaluate for everyone.

**Structured condition format:**
```json
{
  "conditions": {
    "match": "all",
    "rules": [
      { "field": "category",        "op": "eq",   "value": "bug" },
      { "field": "priority_weight", "op": "gte",  "value": 9 },
      { "field": "assigned_to",     "op": "null"              },
      { "field": "hours_since_created", "op": "gte", "value": 4 }
    ]
  },
  "action": { "type": "escalate", "reason": "Critical unassigned bug" }
}
```

**`match`:** `"all"` (AND) or `"any"` (OR)

**Supported fields:** any item field (`status`, `category`, `complexity`, `priority_weight`, `assigned_to`, `skip_count`, `reopen_count`, `blocks_count`, `blocked_by_count`, `score`, `readiness`, `readiness_level`, `tags`) plus computed: `hours_since_created`, `hours_in_status`

**Supported ops:** `eq`, `neq`, `gt`, `gte`, `lt`, `lte`, `null`, `not_null`, `contains`

**`action` type values:** `reprioritize` (include `priority_weight`), `reassign` (include `agent`), `escalate`, `block`, `notify` (include `message` and `severity`: `info|warning|critical`), `skip_force`. Always include `reason`.

**Policy commands:**

| Prompt | Action |
|---|---|
| "Add rule: [description]" | `POST /api/policies` with name, description, conditions, action |
| "List rules" / "Show policies" | `GET /api/policies` |
| "Disable rule [name]" | `PUT /api/policies/<id>` `{"active": false}` |
| "Delete rule [name]" | `DELETE /api/policies/<id>` |
| "Show rule history" | `GET /api/policies/log` |
| "Run rules now" | `GET /api/policies/evaluate` |
| "Suggest rules" | `GET /api/policies/suggestions` |

**Structured action types:**

| Type | Effect |
|---|---|
| `reprioritize` | Change `priority_weight` on item |
| `reassign` | Change `assigned_to` on item |
| `escalate` | Set `priority_weight` ≥ 9, mark as critical |
| `block` | Add a block thread (waiting_on: user) |
| `notify` | Surface a warning (no state change) |
| `skip_force` | Increment `skip_count` — deprioritize temporarily |

**Evaluation pipeline:** Policies evaluate in priority order (10 = highest) on every write. Policies with `conditions` use the pure-Python structured evaluator (no API key needed). Policies without `conditions` use LLM reasoning (`ANTHROPIC_API_KEY` required). Conflicting actions on the same item are adjudicated by a second LLM call; the winning action and reasoning are logged to `policy_log.json`. Actions apply in a background thread — the main response is never delayed.

**Policy effectiveness:** Each policy tracks `fire_count` and `last_fired`. Staleness warnings surface in pulse (`policies.stale_warnings`) and in the web board settings when policies haven't fired in 21+ days or never fired after 14+ days.

**Gate rules vs. policies:** Gate rules (`requires` arrays) enforce lane sequencing synchronously at write time. Policies are contextual intelligence — they respond to live system state and apply judgment. Gates prevent invalid moves; policies surface intelligent actions.

### Concurrency Safety

Server rejects writes where client `version` < current (HTTP 409).
**On 409**: re-read → re-apply → retry. Never manually increment version.

## API

```
GET  /api/pulse[?agent=name]                 Single source of truth — recommendation + coordination + policies
GET  /api/backlog[?agent=name]               Full or agent-filtered backlog
GET  /api/scores                             Ranked items with score_breakdown + readiness
GET  /api/recommend[?agent=name&commit=true] Tribunal recommendation (use pulse instead for coordination)
GET  /api/decisions                          Stored decision history with outcomes
GET  /api/agents                             Agent load info
GET  /api/graph                              Dependency graph with critical path, conflicts, rebalancing
PUT  /api/backlog                            Full write (version-checked)
PUT  /api/items/<id>                         Single item update
POST /api/items/<id>/signal                  Append a readiness signal to an item
GET  /api/policies                           List all policies with staleness analysis
POST /api/policies                           Create a policy {"name","description","priority","active","conditions"?,"action"?}
PUT  /api/policies/<id>                      Update a policy (name, description, priority, active)
DELETE /api/policies/<id>                    Delete a policy
GET  /api/policies/log[?limit=N]             Recent policy fire history (default 50)
GET  /api/policies/evaluate                  Manually trigger policy evaluation
GET  /api/policies/suggestions               LLM-generated rule suggestions based on patterns
```

**`/api/graph` key fields:** `critical_path` (item IDs by cascade impact), `conflicts` (concurrent in-progress items sharing tags), `nodes[].cascade_count` (items transitively unblocked).
