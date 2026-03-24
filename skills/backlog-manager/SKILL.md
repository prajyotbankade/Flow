---
name: backlog-manager
description: Manage an intelligent project backlog — score, prioritize, assign, and pick up work items using a Work Intelligence Engine. Use this skill whenever the user mentions backlog, task queue, work items, story grooming, picking up tasks, prioritizing work, linking issues, work briefs, scoring, agent assignment, what to work on next, reprioritization, or wants to organize tasks like a PM/engineer would in an agile workflow. Also triggers when the user says things like "add this to the list for later", "what should I work on next", "generate a work brief", "what's the priority", "let me park this idea", "queue this up", "what's in my backlog", "link this to #3", "which agent should take this", or "what's blocking progress". If you've just finished a task and the user hasn't given you a new one, generate a work brief and offer to pick up the top-scored item. When you discover bugs, tech debt, or follow-ups while working, add them to the backlog with complexity, category, and tags, and link them back to the source task.
---

# Backlog Manager

You are managing an intelligent project backlog — a prioritized list of work items that flow through stages from idea to completion. You don't just track work — you actively decide what matters next using a scoring engine, assign work based on agent capabilities, and surface reprioritization triggers. Think of it like a scrum board with an AI brain: the user (PM/engineer) adds items, and you (the engineer) score, prioritize, refine, and execute them.

## How It Works

The backlog lives in `backlog.json` at the project root. Items are stored in an ordered array and each gets a **computed intelligence score** based on priority weight, blocking relationships, freshness, complexity, and more. The score — not just array position — determines what matters next. Each item moves through these statuses:

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

The repo ships with a starter `backlog.json` at the project root — zero items, but pre-configured with the default workflow lanes and gate rules. If a user clones the repo, they can start adding items immediately without any setup.

If `backlog.json` doesn't exist (e.g., the user deleted it or is starting fresh), create it:

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

**Choosing scope at setup**: If the project directory appears to be a monorepo (e.g., contains `packages/`, `apps/`, or `services/` directories, or multiple distinct sub-projects), ask the user: "Is this a per-project backlog or a single backlog across the whole repo?" and set `scope` accordingly. For single-project repos, default to `"project"` without asking.

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
5. **Set intelligence fields if context is available**: If the user's description implies a category (bug, feature, chore, tech-debt), set `category`. If you can estimate complexity (low/medium/high), set `complexity`. Add relevant `tags` from the description (e.g., "auth", "frontend", "database"). Set `priority_weight` (1-10) if the user indicates urgency. Don't ask for these — infer from context when possible.
6. Confirm what you added with the title, position, and any intelligence fields you set (e.g., "Added as #5 (bug, medium complexity, [auth, backend])")

### Agent-Initiated Items

You don't have to wait for the user to add items. When you discover something concrete while working — a bug, tech debt, a missing edge case, a follow-up task — add it yourself:

1. Create the item with a clear title and a brief description of *what* you found and *where*
2. Set status to `backlog`, append to the bottom — never self-prioritize
3. **Set intelligence fields**: Always set `category` (bug/feature/chore/tech-debt), estimate `complexity` (low/medium/high) based on what you know about the code involved, and add relevant `tags` (e.g., module names, areas like "auth", "database"). Set `priority_weight` (1-10) only for bugs — score based on severity
4. **Link it back** to the item you were working on (see Linking Items below)
5. Mention it naturally: "While working on #3, I found the error handler doesn't cover timeouts — added as #8 (bug, medium complexity, [error-handling]), linked back."

**What to add**: Concrete, actionable things — bugs found during implementation, tech debt spotted in adjacent code, edge cases that need their own task, follow-up work after a feature lands.

**What NOT to add**: Vague suggestions ("maybe we should refactor everything"), speculative ideas, or duplicates of existing items. If you're not sure it's worth adding, mention it to the user instead and let them decide.

### Linking Items

Items can be linked to show how they're connected. Each link has a `type`, the `item_id` of the related item, and a `reason` explaining the connection:

```json
"links": [
  { "item_id": "abc12345", "type": "discovered-during", "reason": "N+1 query found in user search while implementing rate limiter" }
]
```

**Link types:**
- `discovered-during` — Found while working on another task
- `follow-up` — Work that should happen after the linked item is done
- `blocks` — This item blocks progress on the linked item
- `related` — General connection, same code area or shared context

**Rules:**
- Every link must have a `reason` — no naked links. One sentence that answers "why are these connected?"
- Links are stored on one side only. When displaying, resolve both directions (e.g., if #8 links to #3, show the link from both items)
- When creating an agent-initiated item, always link back to the item that prompted its creation
- The reason should make sense from either item's perspective

### Listing the Backlog

When the user wants to see the backlog, display it grouped by status. Show position number, title, score, assignment, complexity, and any flags (like unresolved threads or staleness). Something like:

```
# Backlog — ProjectName

## In Progress
  → #1 Fix login redirect bug [score: 9.0] (assigned: worker-1, low, bug)

## Ready
  #2 Fix auth token crash [score: 18.1] (medium, bug) — blocks #4, #5
  #3 Add retry logic [score: 7.5] (low, feature) — quick win

## Refined
  #4 Refactor session management [score: 2.0] (high, chore) — blocked by #2
  #5 Redesign settings page [score: 4.7] — ⚠ stale 15d, skipped 6x

## Backlog
  #6 Add dark mode toggle
  #7 Improve search performance — ⚠ 1 unresolved thread

## Done (recent)
  ✓ Set up CI pipeline
  ✓ Add rate limiting
```

### Moving / Reordering

The user might say "move #5 above #1", "move X to position 2", "prioritize X", or "bump X up". Reorder the items array accordingly. Moving changes priority order but doesn't change status — those are separate concerns.

### Editing Items

The user can update any field: title, description, notes, status, assigned_to, complexity, priority_weight, category, tags. Apply changes and confirm. When updating intelligence fields, mention the scoring impact if relevant (e.g., "Set complexity to high — this will reduce the score slightly but recommend opus for execution").

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

**Keep refinement conversational**: Ask a maximum of **2 questions** at a time. Prioritize the most blocking unknowns first. Once those are answered, follow up with the next batch if needed. This keeps the flow natural — refinement should feel like a quick chat, not a questionnaire.

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

When the user says "pick up the next task", "what should I work on next", or when you need to decide what matters next:

1. **Run the Work Intelligence Engine** (see below) to score all items and generate a work brief
2. Present the work brief with the top-scored items, their reasoning, and recommended assignments
3. Confirm: "Based on scoring, I'd recommend **#N <title>** (score: X.X). Should I start on it?"
4. On confirmation, set status to `in-progress`, set `assigned_to` to your agent name, and begin working

**Do NOT rely only on backlog position.** The scoring engine evaluates items using: whether they unblock other work, readiness, complexity, recency, skip count, and blocker status. Position is just one input.

**When there's nothing to pick up**: If the backlog is empty or has no `ready` items, don't offer to pick something up — there's nothing to pick. Instead, let the user know and suggest they add items: "The backlog is empty — want to add something?" or "There are N items in the backlog but none are marked ready yet. Want to refine one or add something new?" Meet the user where they are instead of dead-ending the conversation.

**Being proactive**: After completing a task, if there are more `ready` items, generate a quick work brief: "I've finished '#N <title>'. Based on scores, next up would be '#M <next title>' (unblocks 2 items, score: X.X) — want me to pick it up?" This keeps the work flowing with informed recommendations.

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

## Work Intelligence Engine

The backlog is not just a list — it has a brain. Every item gets a computed score that determines what matters next. You use this engine whenever someone asks "what should I work on?", after completing a task, or when reprioritization triggers fire.

### Scoring Formula

For each item, compute:

```
score = base_priority
      + unblock_weight × (# items this blocks)
      + freshness_component
      + complexity_component
      + blocked_penalty (if blocked by incomplete items)
      + quick_win_bonus (if low complexity AND not blocked)
      + reopen_count × reopen_penalty
      + skip_count × skip_floor
      + critical_bug_boost (if category=bug AND priority_weight ≥ 9)
```

**Components:**
- **base_priority**: Use `priority_weight × priority_weight_factor` if set. Otherwise derive from array position: `(total_items - index) / total_items × 10 × position_weight`
- **unblock_weight**: Count how many items this item blocks (resolve `blocks` links bidirectionally). Multiply by `config.scoring.unblock_weight` (default 2.0). Items that unblock multiple others score highest.
- **freshness**: Items updated within `freshness_boost_days` (default 3) get a boost. Items not updated for `freshness_decay_days` (default 14) get a decay penalty. Between is neutral.
- **complexity_component**: From `config.scoring.complexity_bonus` — low: +1.5, medium: 0, high: -1.0. Null complexity treated as medium.
- **blocked_penalty**: If any item that blocks this one is not `done` or `discarded`, apply penalty (default -3.0)
- **quick_win_bonus**: Low complexity + not blocked = +1.0 bonus. Maintains momentum.
- **reopen_penalty**: Each reopen (from done back to any lane) penalizes by -0.5. Signals complexity was underestimated.
- **skip_floor**: Each time this item is evaluated but not picked, its score rises by +0.3. Prevents permanent neglect — eventually forces a "kill it or bump it" decision.
- **critical_bug_boost**: +5.0 for items with `category: "bug"` AND `priority_weight >= 9`. Critical bugs dominate the ranking.

All weights are configurable via `config.scoring` in `backlog.json`. See `references/schema.md` for defaults.

### Generating Work Briefs

When asked "what should I work on?", after completing a task, or when a reprioritization trigger fires, produce a **work brief** — not just a sorted list:

```
=== WORK BRIEF ===

NEXT: #3 — Fix auth timeout
  Score: 9.2 | Blocks: #7, #11 | Age: 4d in ready
  Assign: Agent-B (worked on auth in #1, load: 1/3)
  Model: sonnet (medium complexity)
  Why: Unblocks 2 critical items, freshness penalty kicking in

THEN: #4 — Add retry logic
  Score: 7.1 | Quick win (low complexity)
  Assign: Agent-A (skill match: backend, load: 0/3)
  Model: haiku
  Why: Clears simple item, maintains momentum

---
WATCH:
  #12 — Stuck in refined 9d (threshold: 7d). Kill or promote?
  Module /auth — 3 reopens in 14d across #1, #3, #9. Consider redesign.
  #6 — Skipped 5 times. Score floor rising. Needs attention or discard.

UNASSIGNED CRITICAL:
  #15 — Production crash (bug, priority: 10). Unassigned 2h. Assign now.
```

**Rules for work briefs:**
- Show the top 2-3 scored items from `ready` or `in-progress` lanes
- Include score, blocking info, age in current lane, recommended agent, and model
- The **Why** line explains the scoring reasoning in plain language
- WATCH section flags: stale items, skip-escalated items, areas with repeated reopens
- UNASSIGNED CRITICAL flags: critical bugs that lack an assignee past the threshold

**When to generate:**
- User asks "what should I work on?" or "what's next?"
- After completing a task (brief version: NEXT recommendation + remaining count)
- After a reprioritization trigger fires
- When proactively checking in after idle time

### Reprioritization Triggers

After every write to `backlog.json` (via server or direct write), check the response for `_events`. The server detects these events by comparing old and new state:

| Event | Meaning | Your Action |
|-------|---------|-------------|
| `critical_bug_created` | New item with category=bug, priority_weight≥9 | Re-score all items. If this bug is now top-ranked, announce: "Critical bug #N created — it scores highest. Should I start on it?" |
| `blocker_resolved` | An item with `blocks` links moved to done/discarded | Re-score unblocked items. If any jumped significantly in rank, highlight: "#N is now unblocked and scores X.X — ready to pick up." |
| `item_reopened` | An item moved backward from done | The server auto-increments `reopen_count`. Flag it: "#N was reopened (reopen #X). Complexity estimate may need adjustment." |
| `blocks_changed` | An item gained new `blocks` links | Re-score the newly-blocked items. Note the impact in your next work brief. |

**Staleness detection** (agent-side, not event-driven): When generating any work brief, scan all items in `refined` and `ready`. If `updated_at` is older than `staleness_days_refined` (default 7) or `staleness_days_ready` (default 5), flag them in the WATCH section.

### Assignment Intelligence

When recommending who should work on an item, compute affinity for each configured agent (from `config.agents`):

- **+2** per matching tag between item `tags` and agent `skills`
- **+3** if the agent previously worked on a linked item (check `assigned_to` and `lane_history.by` on linked items)
- **+1** if the item's `complexity` is in the agent's `preferred_complexity` list
- **-5** if the agent is at or above their `max_active` task cap

**Rules:**
- Pick the agent with the highest positive affinity
- If no agent has positive affinity, or if top agents are tied, **ask the user** — don't silently assign
- Always show the agent's current load in the work brief: `load: 1/3` (current/max)
- If an item is critical (bug, priority ≥ 9) and has been unassigned longer than `critical_unassigned_hours` (default 4h), escalate in UNASSIGNED CRITICAL

### Model Routing

Based on item `complexity` and `config.model_routing`:
- `low` → haiku (fast, cheap)
- `medium` → sonnet (balanced)
- `high` → opus (most capable)

This is advisory — shown in the work brief as `Model: sonnet (medium complexity)`. It helps the user or orchestrator choose the right model for the task. Don't use expensive models for low-impact/nice-to-have items.

### Staleness & Skip Tracking

**Staleness**: Items stuck in `refined` or `ready` past their threshold days are going stale. Flag them in WATCH with the number of days and suggest: "Kill or promote?"

**Skip tracking**: Every time you generate a work brief and an item is scored but not in the top recommendations, increment its `skip_count` by 1. When `skip_count` reaches `skip_escalation_count` (default 5), escalate it in WATCH: "#N has been skipped 5 times. Score floor is rising. Decide: pick it up, reprioritize, or discard."

When an item is finally picked up (moved to `in-progress`), reset its `skip_count` to 0.

### Feedback Loop

If a scored item is picked up, worked, and then reopened (moved back from `done`), the scoring engine's complexity estimate was likely wrong:

- The server automatically increments `reopen_count`
- Each reopen adds a penalty to the item's score
- When you see a reopened item, note: "This item has been reopened N times — the complexity may be higher than estimated. Consider upgrading complexity from [current] to [suggested]."
- If `reopen_count >= 3`, suggest the user re-evaluate whether this item needs a redesign rather than another fix attempt.

### Opportunistic Redesign Detection

Track patterns across items. If you notice:
- **3+ items** in the same `tag` area have been **reopened** within the last 14 days
- A cluster of `blocks` links all point to items in the same module or tag group

Flag it in the WATCH section: "Module /auth — 3 reopens in 14d across #1, #3, #9. Recurring fixes suggest structural issues. Consider a redesign pass before adding more features."

This is proactive — you detect the pattern, the user decides whether to act on it.

## Visual Board

The skill includes a web-based Kanban board for visual management. To launch it:

```bash
python <skill-path>/scripts/backlog_server.py [--port 8089] [--file backlog.json]
```

This opens a drag-and-drop board in the browser that reads/writes the same `backlog.json`. Mention this to the user if they ask for a visual view or want to reorder items visually.

The board auto-refreshes every 5 seconds, so changes made via CLI or by editing the file directly show up automatically.

**Board features**: cards show position number, intelligence score, complexity dot, category badge, assignment badge, created/updated dates, @tagged count, and unresolved thread count. Items past staleness thresholds get an amber border. Click a card to edit — the modal includes an assignment dropdown, complexity/category/priority/tags fields, threaded conversations, an Intelligence tab (score breakdown, recommended agent/model, reopen/skip counts), and all item fields. Use the Sort toggle in the toolbar to switch between position-based and score-based ordering.

**Keyboard shortcuts in the board**: `N` to add new item, `Esc` to close modals.

## Agent Capabilities

The backlog server supports filtered views for multi-agent setups. Leaders see the full backlog and can assign work. Worker agents only see items assigned to them plus unassigned ready items — this keeps the backlog manageable as it grows.

To get a filtered view:
```
GET /api/backlog?agent=worker-1
```

This returns only items where `assigned_to` matches the agent name, plus any unassigned `ready` items that any agent could pick up.

### Intelligence API Endpoints

The server provides intelligence data for agents and the board:

- `GET /api/scores` — Returns all items with computed scores, score breakdowns, recommended agents, and recommended models. Use this to inform work briefs and task selection.
- `GET /api/agents` — Returns configured agent profiles with current load (in-progress item count). Use this to check agent availability before assigning work.

Write responses (`PUT /api/backlog`, `PUT /api/items/<id>`) include an `_events` array when reprioritization triggers fire. Always check this array and act on events (see Reprioritization Triggers).

## Concurrency Safety

The backlog uses optimistic concurrency control to prevent data loss when multiple agents or the web board write simultaneously:

- Every write increments a `version` field in `backlog.json`
- The server rejects writes where the client's version is behind the current version (HTTP 409)
- File writes are atomic (temp file + rename) to prevent corruption

**If you receive a 409 Conflict error, you MUST follow this exact sequence:**
1. **Re-read** `backlog.json` (or `GET /api/backlog`) to get the latest version and data
2. **Re-apply** your intended change on top of the fresh data — do NOT replay stale data
3. **Retry** the write with the updated version

Never guess or manually increment the version to work around a 409 — always sync from the source first.

When writing via the CLI (Read/Write tools), always re-read `backlog.json` before writing to get the latest version. Increment the `version` field in your write.

## Reading and Writing backlog.json

- Always use the Read tool to read and Write tool to write `backlog.json`
- Format with 2-space indentation for readability (the user might edit the file directly)
- Re-read before writing if the file might have been changed externally (user editing or web board)
- Always increment the `version` field when writing

## Item Schema

See `references/schema.md` for the complete JSON structure of `backlog.json` and its items.
