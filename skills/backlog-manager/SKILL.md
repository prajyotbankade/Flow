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
      + blocked_penalty        (if blocked by incomplete items)
      + quick_win_bonus        (low complexity + not blocked)
      + reopen_count × reopen_penalty_per
      + skip_count × skip_floor_per
      + critical_bug_boost     (category=bug AND priority_weight ≥ 9)
```

`base_priority`: `priority_weight × factor` if set; else `(total−idx)/total × 10 × position_weight`.

Defaults (all configurable via `config.scoring`):

| Key | Default |
|---|---|
| priority_weight_factor | 1.0 |
| unblock_weight | 2.0 |
| freshness_boost_days / decay_days | 3 / 14 |
| complexity_bonus low/medium/high | +1.5 / 0 / −1.0 |
| blocked_penalty | −3.0 |
| quick_win_bonus | +1.0 |
| reopen_penalty_per | −0.5 |
| skip_floor_per | +0.3 |
| critical_bug_boost | +5.0 |

### Work Brief Format

```
NEXT:  #N — Title  [score: X] | Blocks: #A,#B | Age: Xd in ready
       Assign: agent-name (reason, load: X/3) | Model: sonnet
       Why: one line

THEN:  #M — Title  [score: X] | Quick win
       ...

WATCH: stale items · skip-escalated items · 3+ reopens in same tag area
UNASSIGNED CRITICAL: critical bugs unassigned > threshold hours
```

Generate after: task completion, reprioritization trigger fires, user asks "what's next?".

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

## Concurrency Safety

Server rejects writes where client `version` < current (HTTP 409).
**On 409**: re-read → re-apply → retry. Never manually increment version.

## API (when server is running)

```
GET  /api/backlog[?agent=name]   Full or agent-filtered backlog
GET  /api/scores                 Ranked items with score_breakdown
GET  /api/agents                 Agent load info
PUT  /api/backlog                Full write (version-checked)
PUT  /api/items/<id>             Single item update
```

Start server: `python <skill-path>/scripts/backlog_server.py [--port 8089] [--file backlog.json]`
