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
    "readiness": "ReadinessConfig (optional)",
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

- `requires`: Gate rules. If set, an item can only move into this lane if its `lane_history` (from the current `gate_from` watermark onward) contains all of the listed lane IDs. Enforced by the server (HTTP 422 on violation) and the web board (blocked drop zones). **Best practice: list only the immediate predecessor** — e.g. `"requires": ["code-review"]` on `done`, not `["refined", "ready", "in-progress", "code-review"]`. Listing only the immediate predecessor means items that moved backward and forward again still satisfy the gate without needing to re-earn every prior lane.

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
  "blocked_penalty": "number (default -3.0) — maximum penalty when blocked at 0% blocker readiness; scales linearly with readiness",
  "quick_win_bonus": "number (default 1.0) — bonus for low-complexity items whose blockers are ≥ ready_threshold",
  "reopen_penalty_per": "number (default -0.5) — score penalty per reopen",
  "skip_floor_per": "number (default 0.3) — score boost per skip (prevents permanent neglect)",
  "position_weight": "number (default 0.5) — weight of position-derived priority when priority_weight is null",
  "critical_bug_boost": "number (default 5.0) — score bonus for critical bugs (category=bug, priority_weight>=9)"
}
```

## ReadinessConfig

Thresholds for the readiness signal engine. Optional — hardcoded defaults apply when absent.

```json
{
  "startable_threshold": "number (default 0.70) — readiness ≥ this value → item is startable with known risk",
  "ready_threshold":     "number (default 0.90) — readiness ≥ this value → item is treated as fully unblocked"
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
  "readiness_signals": [ReadinessSignal, ...],
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

## ReadinessSignal

An observed artifact or gate event that increases confidence a blocker is progressing toward completion. Appended via `POST /api/items/<id>/signal`; never manually removed.

```json
{
  "type": "pr_merged | test_passed | review_approved | file_created | spec_written | design_approved",
  "source": "string — agent name, 'user', or 'auto'",
  "timestamp": "ISO 8601 — set automatically by the server",
  "description": "string (optional) — additional context"
}
```

Signal trust weights used in readiness computation:

| Type | Weight | Trust Level |
|------|--------|-------------|
| `spec_written` | +10% | Medium — intent artifact |
| `file_created` | +10% | Medium — code artifact |
| `design_approved` | +15% | Medium — design gate |
| `test_passed` | +20% | High — downstream gate |
| `pr_merged` | +25% | High — integration artifact |
| `review_approved` | +25% | High — peer review gate |

Status baseline (before signals): `backlog`=5%, `refined`=20%, `ready`=35%, `in-progress`=50%, `code-review`=70%, `done`/`discarded`=100%. Score is capped at 95% until the item is actually done.

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
- `readiness_signals` is optional on items. When absent, readiness is derived from status alone. Append signals via `POST /api/items/<id>/signal` — never write them directly unless migrating data.
- All `config.scoring`, `config.readiness`, `config.agents`, `config.thresholds`, and `config.model_routing` sections are optional. When absent, hardcoded defaults apply. Existing backlogs work without any migration.
- Scoring is computed at evaluation time, never persisted as a field on items. The `GET /api/scores` endpoint computes fresh scores on every request.
- Tribunal recommendations are computed via `GET /api/recommend` — never persisted on items. Decisions are stored separately in `decisions.json`.

## Scores Response (`GET /api/scores`)

Returns all items with computed scores and readiness context.

```json
{
  "items": [
    {
      "id": "string",
      "title": "string",
      "status": "string",
      "score": "number",
      "score_breakdown": {
        "base_priority": "number",
        "unblock": "number",
        "freshness": "number",
        "complexity": "number",
        "blocked_penalty": "number — dynamic, scaled by blocker readiness",
        "quick_win": "number",
        "reopen": "number",
        "skip_floor": "number",
        "critical_bug": "number"
      },
      "readiness": {
        "score": "number (0.0–1.0) — this item's own readiness",
        "level": "not_ready | startable | ready",
        "status_contribution": "number — baseline from status lane",
        "signal_contribution": "number — contribution from observed signals",
        "signals": [ReadinessSignal, ...],
        "blockers": [{"blocker_id": "string", "readiness": "number"}, ...]
      },
      "recommended_agent": "string | null",
      "recommended_model": "string"
    }
  ]
}
```

## Signal Endpoint (`POST /api/items/<id>/signal`)

Append a readiness signal to an item — call this when you observe an artifact or gate event on a blocker item.

**Request body:**
```json
{
  "type": "pr_merged | test_passed | review_approved | file_created | spec_written | design_approved",
  "source": "string — your agent name or 'user'",
  "description": "string (optional)"
}
```

**Response (201):**
```json
{
  "status": "ok",
  "signal": {"type": "...", "source": "...", "timestamp": "..."},
  "version": "integer"
}
```

## Tribunal Recommendation (`GET /api/recommend`)

Returns a justified recommendation with counterfactuals. Optional query params: `?agent=name` (agent-specific fit), `?commit=true` (store decision).

```json
{
  "picked": {
    "item_id": "string — ID of the recommended item",
    "title": "string",
    "status": "string — current status of the item",
    "score": "number — score from the scoring engine",
    "tribunal_score": "number — weighted aggregate across all lenses",
    "reasoning": "string — human-readable justification composed from top lenses",
    "confidence": "high | medium | low — based on margin to runner-up",
    "recommended_agent": "string | null — best-fit agent from agent_fit lens",
    "recommended_model": "string — model routing recommendation",
    "supporting_lenses": [
      {
        "lens": "string — urgency | leverage | agent_fit | risk | momentum",
        "argument": "string — why this lens supports the pick",
        "weight": "number — weighted contribution to tribunal score"
      }
    ],
    "status_note": "string | null — e.g. 'Needs refinement before starting' or 'Startable with risk — weakest blocker at 74% readiness'",
    "readiness": {
      "score": "number — readiness of this item itself (0.0–1.0)",
      "level": "not_ready | startable | ready",
      "status_contribution": "number — baseline from status lane",
      "signal_contribution": "number — additional contribution from observed signals",
      "signals": [ReadinessSignal, ...],
      "blockers": [{"blocker_id": "string", "readiness": "number"}, ...]
    }
  },
  "shadow_ranking": [
    {
      "item_id": "string — runner-up item ID",
      "title": "string",
      "score": "number — scoring engine score",
      "tribunal_score": "number — weighted tribunal score",
      "lost_on_lens": "string — lens with biggest gap vs winner",
      "lost_reason": "string — why this item was not picked (includes readiness % for blocked items)"
    }
  ],
  "lenses": [
    {
      "lens": "string — lens name",
      "argued_for": "string — item_id this lens evaluated",
      "argument": "string | null — lens reasoning (null if no signal)",
      "score": "number — raw lens score",
      "weighted_score": "number — score × lens weight"
    }
  ],
  "candidates_evaluated": "integer — number of eligible items considered",
  "decision_id": "string (optional) — only present when ?commit=true"
}
```

### Tribunal Lenses

| Lens | Weight | Evaluates |
|------|--------|-----------|
| `urgency` | 1.0 | Priority weight, critical bugs, staleness |
| `leverage` | 1.2 | Items blocked by this one, cascade depth |
| `agent_fit` | 0.8 | Skill match, complexity preference, load |
| `risk` | 1.0 | Blocking impact, reopen instability, skip neglect |
| `momentum` | 0.6 | Status progression, recent activity, work invested |

### Confidence Levels

- **high**: Margin to runner-up > 5 tribunal points
- **medium**: Margin 2–5 points
- **low**: Margin < 2 points — consider reviewing alternatives

## Decision (`decisions.json`)

Stored when `/api/recommend?commit=true` is called. Outcomes recorded automatically when picked items move to `done`.

```json
{
  "decisions": [
    {
      "id": "string (8-char alphanumeric)",
      "timestamp": "ISO 8601",
      "agent": "string | null — agent the recommendation was for",
      "picked": "TribunalPicked — same as recommend response",
      "shadow_ranking": "TribunalShadow[] — same as recommend response",
      "lenses": "TribunalLens[] — same as recommend response",
      "candidates_evaluated": "integer",
      "outcome": {
        "completed_at": "ISO 8601 | null",
        "result": "completed"
      }
    }
  ]
}
```

- Capped at 100 decisions (oldest pruned on write)
- `outcome` is `null` until the picked item moves to `done`, at which point the server auto-populates it
- Use `GET /api/decisions` to review decision history and outcome tracking

## Dependency Graph (`GET /api/graph`)

Returns the live dependency graph with readiness, critical path, conflicts, and rebalancing suggestions. Computed fresh on every request.

```json
{
  "nodes": [
    {
      "id": "string",
      "title": "string",
      "status": "string",
      "readiness": "number (0.0–1.0)",
      "readiness_level": "not_ready | startable | ready",
      "assigned_to": "string | null",
      "complexity": "low | medium | high | null",
      "category": "bug | feature | chore | tech-debt | null",
      "tags": ["string"],
      "is_critical_path": "boolean — true if this item is in the top-5 by cascade impact",
      "cascade_count": "integer — number of items transitively unblocked if this completes"
    }
  ],
  "edges": [
    {
      "source": "string — item ID with the link",
      "target": "string — linked item ID",
      "type": "blocks | follow-up | discovered-during | related",
      "reason": "string"
    }
  ],
  "critical_path": ["string — item IDs ordered by cascade impact, highest first (top 10)"],
  "conflicts": [
    {
      "type": "tag_overlap",
      "items": ["string — item IDs in conflict"],
      "item_titles": ["string"],
      "shared_tags": ["string — tags both items share"],
      "description": "string — human-readable conflict explanation"
    }
  ],
  "rebalancing": [
    {
      "type": "rebalance | idle_agent",
      "from_agent": "string (rebalance only)",
      "to_agent": "string (rebalance only)",
      "agent": "string (idle_agent only)",
      "description": "string",
      "transferable_items": ["string — item IDs that could transfer (rebalance only)"]
    }
  ]
}
```

**Critical path**: Items with `is_critical_path: true` have the highest downstream cascade — completing them unblocks the most work. Delay on these items has compounding impact.

**Conflicts**: Detected when two or more in-progress items share tags and are assigned to different agents. Indicates potential merge conflicts or coordination gaps.

**Rebalancing**: Surfaces when an agent is at `max_active` capacity while another agent with matching skills is idle. Suggests transferable items.

## Pulse (`GET /api/pulse[?agent=name]`)

Returns a proactive coordination payload for an agent — bundles recommendation, startable items, conflicts, rebalancing, and active agent activity in a single call. Designed to replace polling: agents call this instead of separately calling `/api/recommend`, `/api/scores`, and `/api/agents`.

```json
{
  "agent": "string | null",
  "recommendation": "TribunalResponse — same as GET /api/recommend",
  "startable_items": [
    {
      "id": "string",
      "title": "string",
      "status": "string",
      "readiness": "number",
      "readiness_level": "ready | startable"
    }
  ],
  "conflicts": ["Conflict — same as GET /api/graph conflicts"],
  "rebalancing": ["Rebalancing — same as GET /api/graph rebalancing"],
  "active_agents": [
    {
      "name": "string",
      "items_in_progress": [{"id": "string", "title": "string"}],
      "current_load": "integer",
      "max_active": "integer",
      "load_pct": "integer (0–100)"
    }
  ],
  "generated_at": "ISO 8601"
}
```

- `startable_items`: Items at ≥70% readiness not yet in-progress. If `?agent=name` is provided, filtered to items where the agent is not overloaded.
- `active_agents`: Full agent activity snapshot — who is working on what and at what load.
- `conflicts`: Proactively surfaced so the agent can avoid or coordinate around contested areas before starting work.

---

## Phase 4: Natural Language Rule Engine

### Policy (`policies.json`)

Policies are natural language rules evaluated by an LLM after every state change. Stored in `policies.json` alongside `backlog.json`.

```json
{
  "policies": [
    {
      "id": "string (8-char alphanumeric)",
      "name": "string — short human-readable name",
      "description": "string — full natural language rule (the LLM evaluates this as a prompt)",
      "priority": "integer (1–10) — higher priority policies evaluated first; wins conflicts",
      "active": "boolean — inactive policies are skipped during evaluation",
      "created_at": "ISO 8601 timestamp",
      "fire_count": "integer — total times this policy has fired",
      "last_fired": "ISO 8601 | null — timestamp of last fire"
    }
  ]
}
```

- Four default policies are seeded on first use: critical bug escalation, neglected item forcing, concurrent complexity warning, stale ready item notification.
- Policies are evaluated in descending priority order (10 first).
- `fire_count` and `last_fired` are updated atomically after each evaluation pass.

### Policy Structured Actions

When an LLM decides a policy fires, it returns one or more structured actions — never prose. Flow executes these deterministically:

| Type | Effect | Required Fields |
|------|--------|-----------------|
| `reprioritize` | Changes `priority_weight` on item | `item_id`, `priority_weight`, `reason` |
| `reassign` | Changes `assigned_to` on item | `item_id`, `agent`, `reason` |
| `escalate` | Sets `priority_weight ≥ 9`, marks as critical | `item_id`, `reason` |
| `block` | Adds a block thread (`waiting_on: user`) | `item_id`, `reason` |
| `notify` | Surfaces a warning message (no state change) | `item_id` (or null), `message`, `severity` |
| `skip_force` | Increments `skip_count` | `item_id`, `reason` |

### Policy Log Entry (`policy_log.json`)

Every policy evaluation (fired or not) is logged. Capped at 200 entries.

```json
{
  "entries": [
    {
      "id": "string (8-char alphanumeric)",
      "policy_id": "string",
      "policy_name": "string",
      "timestamp": "ISO 8601",
      "trigger_event": "string — event that triggered evaluation (e.g. 'critical_bug_created', 'item_update', 'manual')",
      "fired": "boolean",
      "reasoning": "string — LLM's 1-3 sentence explanation of why it fired or did not",
      "actions_proposed": ["PolicyAction — full action objects returned by LLM"],
      "conflict_with": ["string | null — names of conflicting policies, if any"],
      "resolution_reasoning": "string | null — LLM adjudication explanation if conflict occurred",
      "actions_executed": [
        {
          "action": "PolicyAction",
          "status": "executed | notified | skipped | error",
          "detail": "string — what changed"
        }
      ],
      "notifications": ["string — messages from notify actions"]
    }
  ]
}
```

### Policy Endpoints

#### `GET /api/policies`

Returns all policies with computed staleness analysis.

```json
{
  "policies": [
    {
      /* ...Policy fields... */
      "staleness_warnings": ["string — e.g. 'Never fired in 18 days — may be redundant'"]
    }
  ]
}
```

**Staleness rules:**
- `fire_count == 0` and age ≥ 14 days → "Never fired in N days — may be redundant"
- `last_fired` more than 21 days ago → "Hasn't fired in N days — still relevant?"

#### `POST /api/policies`

Create a new policy.

**Request:**
```json
{
  "name": "string (required)",
  "description": "string (required)",
  "priority": "integer (default 5)",
  "active": "boolean (default true)"
}
```

**Response (201):** `{"status": "ok", "policy": {...}}`

#### `PUT /api/policies/<id>`

Update `name`, `description`, `priority`, or `active` on an existing policy.

#### `DELETE /api/policies/<id>`

Remove a policy permanently.

#### `GET /api/policies/log[?limit=N]`

Return recent policy fire history, most recent first. Default limit: 50.

```json
{"entries": [PolicyLogEntry, ...]}
```

#### `GET /api/policies/evaluate`

Manually trigger a full policy evaluation pass right now. Applies any fired actions and returns a summary.

```json
{
  "fires": "integer — number of policies that fired",
  "actions_executed": "integer — number of state-changing actions applied",
  "notifications": ["string — notify messages"],
  "log_ids": ["string — IDs of log entries created"]
}
```

#### `GET /api/policies/suggestions`

Analyse current backlog patterns (high skip counts, unassigned critical bugs, overloaded agents, heavily blocked items) and return LLM-suggested policies.

```json
{
  "suggestions": [
    {
      "name": "string",
      "description": "string",
      "priority": "integer"
    }
  ]
}
```

### Tribunal Recommendation — Policy Influence Enrichment

When `GET /api/recommend` returns a winner that was recently influenced by a policy action (reprioritize or escalate), the `picked` object includes:

```json
{
  "picked": {
    /* ...existing fields... */
    "policy_influences": [
      {
        "policy": "string — policy name",
        "action": "reprioritize | escalate",
        "reason": "string — the action's reason from the policy",
        "at": "ISO 8601 — when the action was applied"
      }
    ]
  }
}
```

### Conflict Resolution

When two policies produce contradictory actions on the same item (e.g., one `escalate`s while another `block`s), a second LLM call adjudicates:

- Contradictory pairs: `(escalate, skip_force)`, `(escalate, block)`, `(reprioritize, skip_force)`
- The winning action is selected by the LLM with a one-sentence reasoning
- Both the conflict and resolution are recorded in `policy_log.json`
- Non-conflicting actions from other items proceed unaffected

### Evaluation Pipeline

After every `PUT /api/backlog` or `PUT /api/items/<id>` write:

1. Active policies loaded from `policies.json` (sorted by priority descending)
2. Context snapshot built: item scores, readiness, agent loads, timestamps
3. Each policy evaluated via `claude-haiku-4-5` — returns `{fires, reasoning, actions}`
4. Conflict detection across all fired actions
5. LLM adjudication for any conflicts (via `claude-haiku-4-5`)
6. Non-conflicting + resolved actions executed against `backlog.json` (new version bump)
7. Fire counts updated in `policies.json`
8. All evaluations (fired or not) logged to `policy_log.json`

The entire pipeline runs in a background thread — the original write response is never delayed. Requires `ANTHROPIC_API_KEY` in the server environment; gracefully skips if absent.
