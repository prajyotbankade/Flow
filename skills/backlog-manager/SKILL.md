---
name: backlog-manager
description: Manage a project backlog board — add, prioritize, refine, link, and pick up work items. Use this skill whenever the user mentions backlog, sprint planning, task queue, work items, story grooming, picking up tasks, prioritizing work, linking issues, or wants to organize tasks like a PM/engineer would in an agile workflow. Also triggers when the user says things like "add this to the list for later", "what should I work on next", "let me park this idea", "queue this up", "what's in my backlog", "link this to #3", "which agent should take this", or "what's blocking progress". If you've just finished a task and the user hasn't given you a new one, generate a work brief and offer to pick up the top-scored item. When you discover bugs, tech debt, or follow-ups while working, add them to the backlog and link them to the source task.
---

# Backlog Manager

Data store: `backlog.json` at project root. Always re-read before writing. Increment `version` on every write. Full schema: `references/schema.md`.

Status flow: `backlog → refined → ready → in-progress → code-review → done` (`discarded` from any lane, no gates).

Items referenced by position: `#1` = index 0, `#2` = index 1, etc.

## Operations

**Add** — Append to end, `status: backlog`, generate 8-char ID. Infer `category`, `complexity`, `tags`, `priority_weight` from context — don't ask. Confirm: "Added as #N (category, complexity, [tags])."

**List** — Group by status: `#N title [score: X] (assigned, complexity, category) — flags`

**Edit** — Update any field. Note scoring impact if relevant (e.g., setting `complexity: high` drops score and routes to opus).

**Assign** — Set `assigned_to`. Unassigned = available to all agents.

**Reorder** — Reorder `items` array. Reordering ≠ status change.

**Discard** — `status: discarded`. No gate rules. Restore: move to any lane, resets `gate_from`.

**Complete** — `status: done`. Add brief completion note. Offer next item from ready queue.

**Refine** — Open threads for unclear items (max 2 questions at a time). Resolve threads → suggest `refined`. User decides `ready`. Always scan for `waiting_on: "agent"` threads — respond and set to `"user"` or null.

**Block** — Open thread, `waiting_on: "user"`, move back to `backlog`. Notify user.

**Pick Up** — Score all items → work brief → confirm with user → set `in-progress` + `assigned_to`. Use `/api/scores` if server is running.

**Agent-initiated items** — Add bugs/tech-debt found while working. Set `category`, `complexity`, `tags`, `priority_weight` (bugs only). Link back to source. Announce: "Found X while on #N — added as #M (bug, medium, [tags])."

## Linking

```json
{ "item_id": "abc12345", "type": "blocks", "reason": "one sentence why" }
```
Types: `blocks`, `discovered-during`, `follow-up`, `related`. `reason` is required on every link.

## Lane Gate Rules

Each status can have `requires: [lane_ids]` — item must have passed through those lanes (from `gate_from` onward) before entering. Backward moves always allowed.

- `lane_history`: append-only. Format: `{"lane": "<old_status>", "at": "<ISO UTC>", "by": "<actor>"}`. `by` = `"user"` or `"backlog-manager"`.
- `gate_from`: watermark index. On backward move: append current lane, set `gate_from = len(lane_history)`.
- Before any forward move: verify target's `requires` against `lane_history[gate_from:]`. If missing: "Can't move to X — requires Y first."
- Server enforces gates (HTTP 422). Check client-side too.

## Work Intelligence Engine

### Scoring Formula

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

`base_priority`: `priority_weight × factor` if set; else `(total−idx)/total × 10 × position_weight`.

`blocked_penalty`: no longer binary. Penalty = `base_penalty × (1 − min_blocker_readiness)`. A blocker at 70% readiness applies only 30% of the base penalty. A fully-done blocker applies 0.

Defaults (all configurable via `config.scoring`):

| Key | Default |
|---|---|
| priority_weight_factor | 1.0 |
| unblock_weight | 2.0 |
| freshness_boost_days / decay_days | 3 / 14 |
| complexity_bonus low/medium/high | +1.5 / 0 / −1.0 |
| blocked_penalty | −3.0 (max, applied at 0% blocker readiness) |
| quick_win_bonus | +1.0 |
| reopen_penalty_per | −0.5 |
| skip_floor_per | +0.3 |
| critical_bug_boost | +5.0 |

### Justification Engine (Tribunal)

Use `GET /api/recommend[?agent=name]` to get a justified recommendation. The tribunal evaluates all eligible items through 5 lenses (urgency, leverage, agent_fit, risk, momentum) and produces:
- **Picked item** with reasoning, confidence, supporting lenses, and readiness context
- **Shadow ranking** of runners-up with "why not" explanations (including blocker readiness %)

When committing to a pick, use `?commit=true` to store the decision for outcome tracking.

### Readiness Signals

Items are no longer binary blocked/unblocked. Each item has a **readiness score** (0.0–1.0) derived from:
1. **Status baseline**: `backlog`=5%, `refined`=20%, `ready`=35%, `in-progress`=50%, `code-review`=70%, `done`/`discarded`=100%
2. **Artifact/gate signals**: observed evidence that work is progressing

Signal types and trust weights:

| Type | Trust | Meaning |
|---|---|---|
| `spec_written` | +10% | Design intent documented |
| `file_created` | +10% | Code artifact exists |
| `design_approved` | +15% | Design gate passed |
| `test_passed` | +20% | Downstream gate cleared |
| `pr_merged` | +25% | Integration artifact complete |
| `review_approved` | +25% | Peer review gate passed |

**Readiness levels** (configurable via `config.readiness`):
- `not_ready`: < 70% — do not start dependent work
- `startable`: 70–89% — can begin with known risk; announce risk in work brief
- `ready`: ≥ 90% — treat as unblocked for planning purposes

**Add a signal** (when you observe an artifact or gate event on a blocker):
```
POST /api/items/<id>/signal
{"type": "pr_merged", "source": "agent-name", "description": "optional note"}
```

### Work Brief Format

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

Generate after: task completion, reprioritization trigger fires, user asks "what's next?". Prefer `/api/recommend` over raw `/api/scores` — it provides justification, not just numbers.

### Reprioritization Triggers

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

### Assignment Intelligence

- +2 per tag match (item `tags` ∩ agent `skills`)
- +3 if agent worked on a linked item (`assigned_to` or `lane_history.by`)
- +1 if item `complexity` in agent's `preferred_complexity`
- −5 if agent at/above `max_active`

Highest positive affinity wins. Tie or no positive affinity → ask user.

### Model Routing

`low` → haiku · `medium` → sonnet · `high` → opus. Advisory — shown in work brief.

### Natural Language Rule Engine (Phase 4)

Flow evaluates active policies after every state change. Policies are written in plain English; Flow uses LLM reasoning to decide if they fire and what structured action to take. Requires `ANTHROPIC_API_KEY`.

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

**Evaluation pipeline:** On every write, policies are evaluated in priority order (10 = highest). The LLM decides if each policy fires; conflicting actions on the same item are adjudicated by a second LLM call. Actions are applied synchronously in a background thread — the main response is never delayed.

**Policy effectiveness:** Each policy tracks `fire_count` and `last_fired`. Policies that never fire after 14 days, or haven't fired in 21 days, surface staleness warnings in the web board settings.

**When to add policies (examples):**
- "If a bug is blocking more than 2 active items, escalate it immediately."
- "Never assign two high-complexity items to the same agent simultaneously."
- "If something has been skipped 5 times, force it to the top."
- "Notify me when any item has been in 'ready' for more than 7 days without being picked up."

**Gate rules vs. policies:** Gate rules (`requires` arrays) are structural — they enforce the sequential flow of lanes and are evaluated synchronously at write time. Policies are contextual intelligence — they respond to live system state and apply judgment. Both layers coexist: gates prevent invalid moves; policies surface intelligent actions.

**Rule conflict resolution:** When two policies produce contradictory actions on the same item (e.g., one escalates while another blocks), a second LLM call adjudicates. The winning action and reasoning are logged to `policy_log.json`.

## Concurrency Safety

Server rejects writes where client `version` < current (HTTP 409).
**On 409**: re-read → re-apply → retry. Never manually increment version.

## API (when server is running)

```
GET  /api/backlog[?agent=name]              Full or agent-filtered backlog
GET  /api/scores                            Ranked items with score_breakdown + readiness
GET  /api/recommend[?agent=name&commit=true]  Tribunal-justified recommendation (+ policy_influences if applicable)
GET  /api/decisions                         Stored decision history with outcomes
GET  /api/agents                            Agent load info
GET  /api/graph                             Dependency graph with critical path, conflicts, rebalancing
GET  /api/pulse[?agent=name]               Proactive push — recommendation + coordination context in one call
PUT  /api/backlog                           Full write (version-checked)
PUT  /api/items/<id>                        Single item update
POST /api/items/<id>/signal                 Append a readiness signal to an item
GET  /api/policies                          List all policies with staleness analysis
POST /api/policies                          Create a policy {"name","description","priority","active"}
PUT  /api/policies/<id>                     Update a policy (name, description, priority, active)
DELETE /api/policies/<id>                   Delete a policy
GET  /api/policies/log[?limit=N]            Recent policy fire history (default 50)
GET  /api/policies/evaluate                 Manually trigger policy evaluation right now
GET  /api/policies/suggestions              LLM-generated rule suggestions based on observed patterns
```

### Using `/api/pulse` (preferred for agent coordination)

Call `GET /api/pulse?agent=<your-name>` instead of separate calls to `/api/recommend`, `/api/scores`, and `/api/agents`. Returns everything in one payload:
- **recommendation**: Tribunal pick with justification
- **startable_items**: Items at ≥70% readiness available now
- **conflicts**: In-progress items in your area (tag overlap with other agents) — check these before starting
- **rebalancing**: Suggestions if load is uneven across agents
- **active_agents**: Who is working on what right now

### Using `/api/graph`

Returns the full dependency graph. Key fields:
- **critical_path**: Item IDs ordered by cascade impact — completing these unblocks the most downstream work
- **conflicts**: Concurrent in-progress items sharing tags across different agents
- **nodes[].is_critical_path**: `true` if this item is in the top-5 by cascade count
- **nodes[].cascade_count**: Number of items transitively unblocked when this completes

Start server: `python <skill-path>/scripts/backlog_server.py [--port 8089] [--file backlog.json]`
