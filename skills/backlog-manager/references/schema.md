# backlog.json Schema

## Top Level

```json
{
  "version": "integer — incremented on every write, used for optimistic concurrency control",
  "config": {
    "scope": "project | global",
    "project_name": "string",
    "statuses": [Status, ...],
    "scoring": "ScoringConfig (optional)",
    "agents": "AgentProfiles (optional)",
    "thresholds": "Thresholds (optional)",
    "model_routing": "ModelRouting (optional)"
  },
  "items": [Item, ...]
}
```

- `version`: Auto-incrementing integer. The server bumps this on every write. Clients must send the current version with updates — if it's behind, the write is rejected (HTTP 409) and the client must re-read.
- `scope`: `"project"` stores backlog.json in the project root. `"global"` stores it at `~/.claude/backlog.json`.
- `project_name`: Human-readable project name, displayed in the board header.
- `statuses`: Ordered array of workflow stages. Defines the columns on the board. First status must always be `"backlog"`. If omitted, defaults to: `backlog → refined → ready → in-progress → done → discarded`.
- `items`: Ordered array — index 0 is highest priority.

## Status

```json
{
  "id": "string — kebab-case identifier used in item.status (e.g., 'code-review')",
  "label": "string — display name shown on the board (e.g., 'Code Review')",
  "color": "string (optional) — hex color for the column header (e.g., '#3b82f6')",
  "requires": ["string (optional) — array of status IDs the item must have passed through before entering this lane"]
}
```

- `requires`: Gate rules. If set, an item can only move into this lane if its `lane_history` contains ALL of the required lane IDs. Enforced by the server (HTTP 422 on violation) and the web board (blocked drop zones). Example: `"requires": ["code-review"]` on the `done` lane means nothing can be marked done without passing through code review first.

## ScoringConfig

Weights and thresholds for the Work Intelligence Engine scoring formula. All fields are optional — when absent, hardcoded defaults are used.

```json
{
  "priority_weight_factor": "number (default 1.0) — multiplier for priority_weight in scoring",
  "unblock_weight": "number (default 2.0) — score bonus per item this item blocks",
  "freshness_boost_days": "integer (default 3) — items updated within this many days get a freshness boost",
  "freshness_decay_days": "integer (default 14) — items not updated for this many days get a staleness penalty",
  "freshness_decay_factor": "number (default 0.5) — multiplier for the staleness penalty",
  "complexity_bonus": "object (default {low: 1.5, medium: 0.0, high: -1.0}) — score adjustment by complexity",
  "blocked_penalty": "number (default -3.0) — score penalty when item is blocked by incomplete items",
  "quick_win_bonus": "number (default 1.0) — bonus for unblocked low-complexity items",
  "reopen_penalty_per": "number (default -0.5) — score penalty per reopen",
  "skip_floor_per": "number (default 0.3) — score boost per skip (prevents permanent neglect)",
  "position_weight": "number (default 0.5) — weight of position-derived priority when priority_weight is null",
  "critical_bug_boost": "number (default 5.0) — score bonus for critical bugs (category=bug, priority_weight>=9)"
}
```

## AgentProfiles

Agent capability profiles for assignment intelligence. Keyed by agent name.

```json
{
  "agent-name": {
    "skills": ["string — tags this agent is proficient in (matched against item tags)"],
    "max_active": "integer (default from thresholds.max_active_per_agent) — max concurrent in-progress items",
    "preferred_complexity": ["low | medium | high — complexity levels this agent should prefer"]
  }
}
```

- `skills`: Matched against item `tags` for affinity scoring. Example: `["auth", "backend", "database"]`.
- `max_active`: Overrides the global `thresholds.max_active_per_agent` for this specific agent.
- `preferred_complexity`: Items whose `complexity` matches get an affinity bonus during assignment.

## Thresholds

Global thresholds for the intelligence engine.

```json
{
  "max_active_per_agent": "integer (default 3) — default max concurrent in-progress items per agent",
  "staleness_days_refined": "integer (default 7) — days in refined before flagging as stale",
  "staleness_days_ready": "integer (default 5) — days in ready before flagging as stale",
  "critical_unassigned_hours": "integer (default 4) — hours a critical item can be unassigned before alerting",
  "skip_escalation_count": "integer (default 5) — skip count that triggers a 'kill or bump' surfacing"
}
```

## ModelRouting

Maps item complexity to recommended AI model. Advisory — appears in work briefs.

```json
{
  "low": "string (default 'haiku') — model for low-complexity items",
  "medium": "string (default 'sonnet') — model for medium-complexity items",
  "high": "string (default 'opus') — model for high-complexity items"
}
```

## Item

```json
{
  "id": "string (8-char alphanumeric)",
  "title": "string",
  "description": "string — detailed description, acceptance criteria, context",
  "status": "string — must match one of the status IDs from config.statuses",
  "assigned_to": "string | null — name of the agent or person assigned to this item",
  "complexity": "low | medium | high | null — estimated effort, drives model routing and scoring",
  "priority_weight": "number (1-10) | null — explicit priority override; when null, array position is used",
  "category": "bug | feature | chore | tech-debt | null — item classification; critical bugs trigger reprioritization",
  "tags": ["string — free-form tags for categorization and agent skill matching (e.g., 'auth', 'frontend')"],
  "reopen_count": "integer — auto-incremented when item moves backward from done (default 0)",
  "skip_count": "integer — incremented when item is evaluated for assignment but passed over (default 0)",
  "threads": [Thread, ...],
  "links": [Link, ...],
  "lane_history": [LaneMove, ...],
  "gate_from": "integer — index into lane_history where the current journey starts (default 0)",
  "notes": "string — additional context, completion notes, etc.",
  "created_at": "ISO 8601 timestamp",
  "updated_at": "ISO 8601 timestamp"
}
```

- `complexity`: Optional estimate of item effort. Used by the scoring engine (low complexity = bonus, high = cost) and for model routing recommendations (low → haiku, medium → sonnet, high → opus). When null, treated as medium for scoring purposes.
- `priority_weight`: Explicit priority value on a 1-10 scale. When set, overrides position-based priority in the scoring formula. When null, the scoring engine derives a base priority from the item's array position.
- `category`: Classifies the item type. Items with `category: "bug"` and `priority_weight >= 9` are treated as critical bugs, triggering automatic reprioritization.
- `tags`: Free-form string array for categorization. Used by the assignment engine to match items to agents with relevant skills. Example: `["auth", "backend", "database"]`.
- `reopen_count`: Automatically incremented by the server when an item moves backward from `done` to any other lane. Feeds into the scoring formula as a penalty — repeated reopens signal the complexity estimate was wrong.
- `skip_count`: Incremented by the agent when generating work briefs and passing over an item. Creates a rising score floor that eventually forces attention ("kill it or bump it").

## Thread

A thread is a conversation about a specific topic on a backlog item — typically started by a question during refinement.

```json
{
  "topic": "string — the subject of the thread (usually the original question)",
  "resolved": "boolean — whether the thread is closed",
  "waiting_on": "agent | user | null — who the thread is waiting on for a response",
  "created_at": "ISO 8601 timestamp",
  "thread": [Message, ...]
}
```

## Link

A connection between two backlog items — tracks why they're related and how the relationship was discovered.

```json
{
  "item_id": "string — ID of the linked item",
  "type": "discovered-during | follow-up | blocks | related",
  "reason": "string — one sentence explaining why these items are connected"
}
```

- `item_id`: The 8-char ID of the other item. Links are stored on one side only — both directions can be resolved at read time.
- `type`: The nature of the relationship:
  - `discovered-during` — Found while working on another task (e.g., bug spotted during implementation)
  - `follow-up` — Work that should happen after the linked item is done
  - `blocks` — This item blocks progress on the linked item
  - `related` — General connection (same code area, shared context)
- `reason`: Required. A brief explanation that makes sense from either item's perspective. This is what gives the link value weeks later when the context is forgotten.

## LaneMove

A record of an item moving through a lane. Appended to `lane_history` every time an item's status changes.

```json
{
  "lane": "string — the status ID the item was in before the move",
  "at": "ISO 8601 timestamp — when the move happened",
  "by": "string — who performed the move (e.g., 'user', 'board', agent name)"
}
```

## Message

```json
{
  "role": "agent | user",
  "message": "string",
  "at": "ISO 8601 timestamp"
}
```

## Rules

- Item order in `items` array = priority (index 0 is highest)
- New items append to the end
- Status flow follows the order defined in `config.statuses` (left to right on the board)
- Default flow: `backlog` → `refined` → `ready` → `in-progress` → `code-review` → `done` → `discarded`
- Items can move backward (e.g., back to an earlier status if new threads are opened)
- First status is always `backlog` — the entry point for new items
- `discarded` is a special terminal lane: items can be moved there from **any** lane, bypassing all gate rules. Discarded items can always be restored to any other lane (treated as a backward move — resets `gate_from`).
- An item with unresolved threads (resolved is false) should not be marked `ready`
- Threads with `waiting_on: "agent"` indicate the user has explicitly tagged the agent for a response
- After responding to a tagged thread, set `waiting_on` to `"user"` or `null`
- IDs are 8-character alphanumeric strings (e.g., `a1b2c3d4`)
- All timestamps are ISO 8601 format
- `version` starts at 0 and increments on every write — used by the server for conflict detection
- `assigned_to` is optional — when set, the item appears in that agent's filtered view
- Users can reference items by position number (e.g., `#1` = first item in the array)
- `lane_history` is a full audit trail of every lane an item has visited, in order. Always appended, never truncated.
- `gate_from` is a watermark index into `lane_history`. When an item moves backward, `gate_from` is set to the current length of `lane_history`, marking the start of a new journey. The rule engine only checks entries from `gate_from` onward when evaluating gate requirements. Default is `0`.
- Lane gate rules (`requires`) are enforced at 3 layers: web board (blocked drop zones), server API (HTTP 422), and agent instructions (SKILL.md)
- Moving backward is always allowed (e.g., back to backlog from any lane) — gate rules only apply when moving forward
- `links` is an array of connections to other items. Each link has a `type`, `item_id`, and `reason`. Links are stored on one side only — resolve both directions at read time.
- Every link must have a `reason` — no naked links. The reason should make sense from either item's perspective.
- Valid link types: `discovered-during`, `follow-up`, `blocks`, `related`
- `reopen_count` is automatically incremented by the server when an item's status changes backward from `done`. Never manually set or decrement.
- `skip_count` is incremented by the agent during work brief generation for items that are evaluated but not recommended. Reset to 0 when an item is picked up.
- `complexity`, `priority_weight`, `category`, and `tags` are optional on all items. The scoring engine uses sensible defaults when they are absent (null complexity = medium, null priority_weight = position-derived, null category = no boost, empty tags = no skill matching).
- All `config.scoring`, `config.agents`, `config.thresholds`, and `config.model_routing` sections are optional. When absent, hardcoded defaults apply. Existing backlogs work without any migration.
- Scoring is computed at evaluation time, never persisted as a field on items. The `GET /api/scores` endpoint computes fresh scores on every request.
