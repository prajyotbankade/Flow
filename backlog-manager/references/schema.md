# backlog.json Schema

## Top Level

```json
{
  "version": "integer — incremented on every write, used for optimistic concurrency control",
  "config": {
    "scope": "project | global",
    "project_name": "string",
    "statuses": [Status, ...]
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

## Item

```json
{
  "id": "string (8-char alphanumeric)",
  "title": "string",
  "description": "string — detailed description, acceptance criteria, context",
  "status": "string — must match one of the status IDs from config.statuses",
  "assigned_to": "string | null — name of the agent or person assigned to this item",
  "threads": [Thread, ...],
  "links": [Link, ...],
  "lane_history": [LaneMove, ...],
  "gate_from": "integer — index into lane_history where the current journey starts (default 0)",
  "notes": "string — additional context, completion notes, etc.",
  "created_at": "ISO 8601 timestamp",
  "updated_at": "ISO 8601 timestamp"
}
```

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
