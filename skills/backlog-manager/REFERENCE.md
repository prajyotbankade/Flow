# Backlog Manager — Reference

Full CLI, API, scoring internals, and configuration. Agents consult this on-demand; the operating protocol lives in [SKILL.md](SKILL.md).

---

## CLI Reference

```bash
# File resolution: --file flag > BACKLOG_FILE env var > ./backlog.json (default)

backlog top                           # top 5 items by score — what to work on next (no server needed)
backlog top 10                        # top 10 items by score
backlog top --json                    # machine-readable ranked list

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

backlog init                          # create backlog.json + wire CLAUDE.md (one-shot setup)
backlog board                         # launch web board (port 8089)
backlog board --port 9000             # custom port

backlog handoff reviewer --item 3 --review   # review handoff: pass/reject verdict
backlog handoff backend-dev --item 3         # work handoff: done/blocked/partial

backlog orchestrate                   # supervised mode (default): acts on ready+ items
backlog orchestrate --mode auto       # auto mode: lead agent refines + starts items autonomously
backlog orchestrate --poll 30         # tick interval in seconds (default 10)
backlog orchestrate --once            # single tick and exit
backlog orchestrate --dry-run         # print actions without invoking agents

backlog doctor                        # check project setup (backlog.json + CLAUDE.md)
backlog doctor --fix                  # repair missing CLAUDE.md setup
```

**Exit codes:** `0` success · `1` gate violation / not found / validation error · `2` version conflict (re-read and retry)

Data store: `backlog.json` at project root. Always re-read before writing. Increment `version` on every write.

Status flow: `backlog → refined → ready → in-progress → code-review → done` (`discarded` from any lane, no gates).

Items referenced by position: `#1` = index 0, `#2` = index 1, etc.

---

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

---

## Scoring

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

`base_priority`: `priority_weight × factor`. All items receive `priority_weight = 5` at creation if not explicitly set. `blocked_penalty` scales with blocker readiness — a blocker at 70% applies only 30% of the penalty; a fully-done blocker applies 0. All weights configurable via `config.scoring`.

---

## Readiness Signals

Each item has a **readiness score** (0.0–1.0) derived from status baseline + artifact signals:

| Status baseline | `backlog`=5% | `refined`=20% | `ready`=35% | `in-progress`=50% | `code-review`=70% | `done`=100% |

Signal types (additive):

| Type | Trust | Meaning |
|---|---|---|
| `spec_written` | +10% | Spec captured at ready gate |
| `file_created` | +10% | Code artifact exists |
| `design_approved` | +15% | Design gate passed |
| `test_passed` | +20% | Downstream gate cleared |
| `pr_merged` | +25% | Integration artifact complete |
| `review_approved` | +25% | Peer review gate passed |

**Readiness levels:** `not_ready` (< 70%) · `startable` (70–89%) · `ready` (>= 90%)

**Add a signal:** `POST /api/items/<id>/signal` with `{"type": "pr_merged", "source": "agent-name", "description": "optional note"}`

---

## Tribunal

Evaluates eligible items through 6 weighted lenses. Access via `/api/pulse` (preferred) or `GET /api/recommend[?agent=name&commit=true]`.

| Lens | Weight | Evaluates |
|---|---|---|
| urgency | 1.0 | Time-sensitivity, criticality, staleness |
| leverage | 1.2 | Downstream unblock cascade |
| agent_fit | 0.8 | Skill match, complexity preference, load, history |
| risk | 1.0 | Blocking impact, reopens, skip neglect |
| momentum | 0.6 | Status progression, recent activity |
| strategic | 1.0 | Business value alignment with declared focus areas |

**Strategic lens** — tribunal-only, does not modify raw scores. Configure `config.strategic.current_focus` with tags/categories the team is prioritizing (e.g., `["auth", "security"]`). Items with matching tags, high `priority_weight` (>=8), or category alignment score higher.

Use `?commit=true` on `/api/recommend` to store decisions for outcome tracking.

---

## Orchestrator

`backlog orchestrate` is a persistent process that drives the dev cycle forward.

**Supervised (default):** Orchestrator only acts on `ready`+ items. Human controls when work starts by moving items to `ready`.

**Auto:** A designated lead agent continuously picks the highest-priority `backlog`/`refined` item, assesses whether it's actionable, and either moves it to `ready` or opens a refinement thread (`waiting_on: "user"`) with at most 2 blocking questions.

**Config:**
```json
"orchestrator": {
  "mode": "supervised",
  "require_review": true
}
```

**Lead agent:** Set `"role": "lead"` on exactly one agent in `config.agents`. Required for auto mode. Zero or multiple leads → hard error.

**Reviewer agent:** Set `"role": "reviewer"` on the designated code review agent. Gets a +10 score bonus in `_select_agent`.

```json
"agents": {
  "lead-dev": { "role": "lead", "skills": ["python", "api"], "max_active": 1 },
  "worker-a":  { "skills": ["python", "api"], "max_active": 2 }
}
```

**Honesty rule:** Agents must stop and ask the human rather than guess when context is insufficient.

---

## Assignment Intelligence

- +2 per tag match (item `tags` ∩ agent `skills`)
- +3 if agent worked on a linked item (`assigned_to` or `lane_history.by`)
- +1 if item `complexity` in agent's `preferred_complexity`
- −5 if agent at/above `max_active`

Highest positive affinity wins. Tie or no positive affinity → ask user.

---

## Agent Team (`.claude/agents/`)

The team roster lives in `.claude/agents/*.md`. The server reads these files on every request — no restart needed.

**Template:**
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

**Persona constraints:** Max 10 learnings, max 60 lines per file.

---

## Lane Gate Rules

Each status can have `requires: [lane_ids]` — item must have passed through those lanes (from `gate_from` onward) before entering. Backward moves always allowed.

- `lane_history`: append-only. Format: `{"lane": "<old_status>", "at": "<ISO UTC>", "by": "<actor>"}`. `by` = agent name, `"user"`, or `"unknown"` (legacy). Bare-string entries normalized to structured dicts on every read.
- `gate_from`: watermark index. On backward move: append current lane, set `gate_from = len(lane_history)`.
- Before any forward move: verify target's `requires` against `lane_history[gate_from:]`. If missing: "Can't move to X — requires Y first."
- Server enforces gates (HTTP 422). Check client-side too.

**Backward-then-forward example:**
An item progresses `backlog → ready → in-progress`, then gets moved back to `ready`. On the backward move, `gate_from` resets. The earlier `in-progress` entry is now before `gate_from` and no longer counts as an earned gate. Attempting to move to `code-review` (which requires `in-progress`) will be blocked — the item must pass through `in-progress` again.

---

## Natural Language Rule Engine

Policies are plain English rules. When a policy has a `conditions` + `action` block, the server evaluates it with pure Python — **no API key required**. Policies without `conditions` fall back to LLM evaluation (requires `ANTHROPIC_API_KEY`).

**Always compile the natural language description into structured `conditions` + `action` fields** — this makes the policy free to evaluate for everyone.

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
| "Add rule: [description]" | `POST /api/policies` |
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

**Evaluation pipeline:** Policies evaluate in priority order (10 = highest) on every write. Conflicting actions on the same item are adjudicated by a second LLM call; the winning action and reasoning are logged to `policy_log.json`. Actions apply in a background thread — the main response is never delayed.

**Policy effectiveness:** Each policy tracks `fire_count` and `last_fired`. Staleness warnings surface in pulse when policies haven't fired in 21+ days or never fired after 14+ days.

**Gate rules vs. policies:** Gate rules enforce lane sequencing synchronously at write time. Policies are contextual intelligence — they respond to live system state. Gates prevent invalid moves; policies surface intelligent actions.

---

## API

```
GET  /api/pulse[?agent=name]                 Single source of truth — recommendation + coordination + policies
GET  /api/backlog[?agent=name]               Full or agent-filtered backlog
GET  /api/scores                             Ranked items with score_breakdown + readiness
GET  /api/recommend[?agent=name&commit=true] Tribunal recommendation (use pulse instead)
GET  /api/decisions                          Stored decision history with outcomes
GET  /api/agents                             Agent load info
GET  /api/graph                              Dependency graph with critical path, conflicts, rebalancing
PUT  /api/backlog                            Full write (version-checked)
PUT  /api/items/<id>                         Single item update
POST /api/items/<id>/signal                  Append a readiness signal to an item
GET  /api/policies                           List all policies with staleness analysis
POST /api/policies                           Create a policy {"name","description","priority","active","conditions"?,"action"?}
PUT  /api/policies/<id>                      Update a policy
DELETE /api/policies/<id>                    Delete a policy
GET  /api/policies/log[?limit=N]             Recent policy fire history (default 50)
GET  /api/policies/evaluate                  Manually trigger policy evaluation
GET  /api/policies/suggestions               LLM-generated rule suggestions based on patterns
```

**`/api/graph` key fields:** `critical_path` (item IDs by cascade impact), `conflicts` (concurrent in-progress items sharing tags), `nodes[].cascade_count` (items transitively unblocked).
