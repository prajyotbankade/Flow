---
name: backlog-manager
description: Manage a project backlog board — add, prioritize, refine, link, and pick up work items. Use this skill whenever the user mentions backlog, sprint planning, task queue, work items, story grooming, picking up tasks, prioritizing work, linking issues, or wants to organize tasks like a PM/engineer would in an agile workflow. Also triggers when the user says things like "add this to the list for later", "what should I work on next", "let me park this idea", "queue this up", "what's in my backlog", "link this to #3", "which agent should take this", or "what's blocking progress". If you've just finished a task and the user hasn't given you a new one, generate a work brief and offer to pick up the top-scored item. When you discover bugs, tech debt, or follow-ups while working, add them to the backlog and link them to the source task.
---

# Backlog Manager

## How to Operate

If the server is not running, start it before doing anything else.

1. **Start server:** `python <skill-path>/scripts/backlog_server.py --file backlog.json`
   - If already running, verify with a health check: `GET /api/backlog`
2. **Decompose** the work into tasks — add via API with dependencies and links (see Decomposing a Feature below)
3. **Assign** based on team (`.claude/agents/`) using assignment intelligence
4. **Delegate** to sub-agents with handoff protocol (see Delegating to a Sub-Agent below)
5. **Monitor** via `/api/pulse` — act on `_events` after every write
6. **Prune personas** — after task completion, review sub-agent persona files: remove duplicates, merge similar learnings, trim anything now obvious from the codebase

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
   - Model routing: `low` → haiku · `medium` → sonnet · `high` → opus (advisory — shown in work brief)
4. Set `tags` for skill matching (e.g., backend, frontend, auth, testing)
5. Set `priority_weight` based on how much value this item unlocks — consider both dependency depth (items that unblock the most work) and business importance. If a Strategic focus is declared, weight items matching it higher
6. Add all items via API, dependencies first so links resolve correctly

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

**Add** — Append to end, `status: backlog`, generate 8-char ID. Infer `category`, `complexity`, `tags`, `priority_weight` from context — don't ask. Confirm: "Added as #N (category, complexity, [tags])."

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

`base_priority`: `priority_weight × factor` if set; else positional based on array order. `blocked_penalty` scales with blocker readiness — a blocker at 70% applies only 30% of the penalty; a fully-done blocker applies 0. All weights configurable via `config.scoring`.

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

**Persona constraints:** Max 10 learnings, max 40 lines per file. The lead agent prunes personas after task completion (step 6 of How to Operate).

### Lane Gate Rules

Each status can have `requires: [lane_ids]` — item must have passed through those lanes (from `gate_from` onward) before entering. Backward moves always allowed.

- `lane_history`: append-only. Format: `{"lane": "<old_status>", "at": "<ISO UTC>", "by": "<actor>"}`. `by` = `"user"` or `"backlog-manager"`.
- `gate_from`: watermark index. On backward move: append current lane, set `gate_from = len(lane_history)`.
- Before any forward move: verify target's `requires` against `lane_history[gate_from:]`. If missing: "Can't move to X — requires Y first."
- Server enforces gates (HTTP 422). Check client-side too.

### Natural Language Rule Engine

Policies are plain English rules evaluated by LLM reasoning on every write. Requires `ANTHROPIC_API_KEY`.

**Policy commands:**

| Prompt | Action |
|---|---|
| "Add rule: [description]" | `POST /api/policies` with name + description |
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
POST /api/policies                           Create a policy {"name","description","priority","active"}
PUT  /api/policies/<id>                      Update a policy (name, description, priority, active)
DELETE /api/policies/<id>                    Delete a policy
GET  /api/policies/log[?limit=N]             Recent policy fire history (default 50)
GET  /api/policies/evaluate                  Manually trigger policy evaluation
GET  /api/policies/suggestions               LLM-generated rule suggestions based on patterns
```

**`/api/graph` key fields:** `critical_path` (item IDs by cascade impact), `conflicts` (concurrent in-progress items sharing tags), `nodes[].cascade_count` (items transitively unblocked).
