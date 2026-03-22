---
name: backlog-manager
description: Manage a project backlog board — add, prioritize, refine, and pick up work items. Use this skill whenever the user mentions backlog, sprint planning, task queue, work items, story grooming, picking up tasks, prioritizing work, or wants to organize tasks like a PM/engineer would in an agile workflow. Also triggers when the user says things like "add this to the list for later", "what should I work on next", "let me park this idea", "queue this up", or "what's in my backlog". If you've just finished a task and the user hasn't given you a new one, check the backlog for ready items and offer to pick one up.
---

# Backlog Manager

You are managing a project backlog — a prioritized list of work items that flow through stages from idea to completion. Think of it like a scrum board: the user (PM/engineer) adds and prioritizes items, and you (the engineer) refine and execute them.

## How It Works

The backlog lives in `backlog.json` at the project root. Items are stored in an ordered array — **position in the array is the priority** (first item = highest priority). Each item moves through these statuses:

```
backlog → refined → ready → in-progress → code-review → done
                                                          ↓
                                                      discarded (from any lane)
```

- **backlog**: Just added, might be vague or missing details
- **refined**: Discussed with the user, questions answered, enough detail to act on
- **ready**: User has confirmed it's good to pick up
- **in-progress**: Currently being worked on
- **code-review**: Work is complete and awaiting review
- **done**: Reviewed and completed
- **discarded**: Removed from active work — can always be restored back to any lane

## Setup

If `backlog.json` doesn't exist when the user wants to use the backlog, create it:

```json
{
  "version": 0,
  "config": {
    "scope": "project",
    "project_name": "<infer from directory name or ask>"
  },
  "items": []
}
```

The `scope` field supports two modes:
- `"project"` (default): `backlog.json` lives in the project root
- `"global"`: `backlog.json` lives at `~/.claude/backlog.json` — useful for monorepos or cross-project work

## Configurable Workflow Lanes

Every team works differently. The board's columns (lanes) are fully configurable via `config.statuses`. If omitted, the default flow is used:

```
backlog → refined → ready → in-progress → code-review → done → discarded
```

To customize, add a `statuses` array to the config. Each entry has an `id` (used in item data), a `label` (displayed on the board), and an optional `color` (hex). Example (the default):

```json
"config": {
  "project_name": "My Project",
  "statuses": [
    { "id": "backlog",     "label": "Backlog" },
    { "id": "refined",     "label": "Refined" },
    { "id": "ready",       "label": "Ready" },
    { "id": "in-progress", "label": "In Progress" },
    { "id": "code-review", "label": "Code Review", "color": "#ec4899" },
    { "id": "done",        "label": "Done" },
    { "id": "discarded",   "label": "Discarded",   "color": "#cbd5e1" }
  ]
}
```

**Rules:**
- The first status must always be `backlog` — this is the entry point for new items
- The last status is the terminal/done state
- Everything in between is up to the user — add, remove, rename, reorder
- Colors are auto-assigned from a palette if not specified
- Item `status` values must match one of the configured status IDs

When the user asks to set up their board or configure lanes, help them build this array. The agent doesn't need to understand the semantics of each lane — just that items flow left to right, first column is intake, last column is done.

## Lane Gate Rules (Rule Engine)

Lanes can have `requires` — a list of lane IDs that an item **must have passed through** before it can enter that lane. This prevents skipping steps (e.g., an agent can't move a task straight from In Progress to Done if Code Review is required).

Example config with gate rules:
```json
"statuses": [
  { "id": "backlog",     "label": "Backlog" },
  { "id": "refined",     "label": "Refined" },
  { "id": "ready",       "label": "Ready" },
  { "id": "in-progress", "label": "In Progress" },
  { "id": "code-review", "label": "Code Review", "color": "#ec4899", "requires": ["in-progress"] },
  { "id": "done",        "label": "Done",        "requires": ["code-review"] },
  { "id": "discarded",   "label": "Discarded",   "color": "#cbd5e1" }
]
```

In this setup:
- Moving to "Code Review" requires the item has been through "In Progress"
- Moving to "Done" requires the item has been through "Code Review"
- An agent cannot skip Code Review and move straight to Done
- An item can be moved to "Discarded" from any lane — no gate rules apply

**How it works:**
- Every item has a `lane_history` array — a full audit trail of every lane it has visited. This is always appended, never deleted.
- Every item has a `gate_from` integer (default `0`) — a watermark index into `lane_history`. The rule engine only checks entries from `gate_from` onward.
- When you move an item **forward**, the current lane is appended to `lane_history`.
- When you move an item **backward**, the current lane is appended to `lane_history` AND `gate_from` is set to the new length of `lane_history`. This means the rule engine ignores all prior history — the item must re-earn gates from its new position.
- Example: item with history `[backlog, ready, in-progress, code-review]` moves back to Ready → history becomes `[backlog, ready, in-progress, code-review, done]`, `gate_from: 5`. Now to reach Done again, it must go through In Progress and Code Review — the old passes don't count.
- Moving backward (to an earlier lane) is always allowed — gate rules only apply to forward movement.
- The server enforces gates with HTTP 422 rejection — even if an agent tries to bypass the board.
- The server also enforces the `gate_from` watermark on backward moves, so even direct API calls respect it.

**As an agent, before moving any item:**
1. Read the item's current `lane_history` and `status`
2. Check the target lane's `requires` in `config.statuses`
3. If the item hasn't been through all required lanes, do NOT attempt the move
4. Instead, tell the user what's missing (e.g., "Can't move to Done — needs Code Review first")

## Referencing Items by Number

Users can reference items by their position number using `#N` (e.g., `#1` for the first item, `#3` for the third). Position is 1-based and corresponds to the item's index in the `items` array. When the user says something like "can you look at #2?" or "move #5 above #1", resolve the `#N` to the item at index `N-1` in the array.

## Operations

### Adding Items

When the user says something like "add X to the backlog", "park this for later", or describes a task they want queued up:

1. Create an item with a clear title and whatever description the user provided
2. Set status to `backlog`
3. Append to the END of the items array — new items always go to the bottom
4. Generate an 8-character ID (e.g., `a1b2c3d4`)
5. Confirm what you added with the title and position (e.g., "Added as #5")

### Listing the Backlog

When the user wants to see the backlog, display it grouped by status. Show position number, title, assignment, and any flags (like unresolved threads). Something like:

```
# Backlog — ProjectName

## In Progress
  → #1 Fix login redirect bug (assigned: worker-1)

## Ready
  #2 Implement user auth flow
  #3 Add email notifications

## Refined
  #4 Refactor API error handling

## Backlog
  #5 Add dark mode toggle
  #6 Improve search performance — ⚠ 1 unresolved thread

## Done (recent)
  ✓ Set up CI pipeline
  ✓ Add rate limiting
```

### Moving / Reordering

The user might say "move #5 above #1", "move X to position 2", "prioritize X", or "bump X up". Reorder the items array accordingly. Moving changes priority order but doesn't change status — those are separate concerns.

### Editing Items

The user can update any field: title, description, notes, status, assigned_to. Apply changes and confirm.

### Assignment

Items can be assigned to agents or people via the `assigned_to` field. When a user says "assign #3 to worker-1" or "I'll take #2", set the field accordingly. Unassigned items show as available to any agent. The web board also supports assignment via a dropdown in the edit modal.

### Refinement

This is the collaborative process of making an item actionable. When the user asks you to refine an item (or when you review one that's still vague):

1. Read the item's current details
2. If anything is unclear, start a thread in the item's `threads` array — each thread has a `topic` (like "What's the scope?") and a `thread` array of messages for back-and-forth discussion
3. When the user responds, add their message to the thread. If you need to follow up, add another message — this creates a natural conversation flow
4. Mark threads as `resolved: true` when the topic is settled
5. Once all threads are resolved and you have enough context to execute, suggest moving it to `refined`
6. The user decides when to mark it `ready`

**Responding to tagged threads**: The user can tag you on a thread by setting `waiting_on: "agent"` (via the web board's @Agent button). When you see threads tagged for you, prioritize responding to them. After responding, set `waiting_on` to `"user"` or `null`. When checking the backlog, always scan for threads with `waiting_on: "agent"` — these are the user explicitly asking for your input.

Threads are like mini-conversations within an item. Each one focuses on a specific topic — scope, dependencies, acceptance criteria, etc. This keeps discussions organized and makes it easy to see what's still unresolved.

The goal is that by the time something is `ready`, you can pick it up and work on it without needing to stop and ask more questions.

### Handling Blockers

If you're working on an item (or reviewing it) and discover it has unanswered questions or missing information that prevents progress:

1. Open a thread explaining what's blocking you
2. Set the thread's `waiting_on` to `"user"`
3. Move the item back to `backlog` (or `refined` if it was previously refined but new questions arose)
4. Let the user know: "#3 has a blocker — I've added a thread about [topic]. Moving it back to backlog until we resolve it."

This prevents items from sitting in `in-progress` or `ready` when they can't actually be worked on.

### Picking Up Work

When the user says "pick up the next task" or "what should I work on next":

1. Find the highest-priority item with status `ready` (earliest in the array)
2. Confirm: "The next ready item is **#N <title>**. Should I start on it?"
3. On confirmation, set status to `in-progress`, set `assigned_to` to your agent name, and begin working

**Being proactive**: After completing a task, if there are more `ready` items, mention it naturally: "I've finished '#N <title>'. There are N more ready items — want me to pick up '#M <next title>'?" This keeps the work flowing without the user having to remember to check the board.

### Completing Items

When a task is done:

1. Set status to `done`
2. Add a brief completion note if useful (what was done, any caveats)
3. Update `updated_at`
4. Mention remaining ready items if any

### Discarding Items

When the user wants to remove an item without deleting it (e.g., "discard #3", "drop this idea", "we're not doing this"):

1. Set status to `discarded`
2. **No gate rules apply** — an item can be discarded from any lane at any time
3. Discarded items remain in `backlog.json` for the audit trail but are hidden on the board by default

To restore a discarded item:
1. Move it back to `backlog` (or any other lane) — this is always allowed
2. The `gate_from` watermark resets, so the item must re-earn any gates on its new journey
3. Tell the user: "#N has been restored to backlog."

## Visual Board

The skill includes a web-based Kanban board for visual management. To launch it:

```bash
python <skill-path>/scripts/backlog_server.py [--port 8089] [--file backlog.json]
```

This opens a drag-and-drop board in the browser that reads/writes the same `backlog.json`. Mention this to the user if they ask for a visual view or want to reorder items visually.

The board auto-refreshes every 5 seconds, so changes made via CLI or by editing the file directly show up automatically.

**Board features**: cards show position number, assignment badge, created/updated dates, @tagged count, and unresolved thread count. Click a card to edit — the modal includes an assignment dropdown, threaded conversations, and all item fields.

**Keyboard shortcuts in the board**: `N` to add new item, `Esc` to close modals.

## Agent Capabilities

The backlog server supports filtered views for multi-agent setups. Leaders see the full backlog and can assign work. Worker agents only see items assigned to them plus unassigned ready items — this keeps the backlog manageable as it grows.

To get a filtered view:
```
GET /api/backlog?agent=worker-1
```

This returns only items where `assigned_to` matches the agent name, plus any unassigned `ready` items that any agent could pick up.

## Concurrency Safety

The backlog uses optimistic concurrency control to prevent data loss when multiple agents or the web board write simultaneously:

- Every write increments a `version` field in `backlog.json`
- The server rejects writes where the client's version is behind the current version (HTTP 409)
- On conflict, re-read the file and retry your write
- File writes are atomic (temp file + rename) to prevent corruption

When writing via the CLI (Read/Write tools), always re-read `backlog.json` before writing to get the latest version. Increment the `version` field in your write.

## Reading and Writing backlog.json

- Always use the Read tool to read and Write tool to write `backlog.json`
- Format with 2-space indentation for readability (the user might edit the file directly)
- Re-read before writing if the file might have been changed externally (user editing or web board)
- Always increment the `version` field when writing

## Item Schema

See `references/schema.md` for the complete JSON structure of `backlog.json` and its items.
