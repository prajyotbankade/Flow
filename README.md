# Flow

A Claude Code skill for managing project backlogs — add, prioritize, refine, and pick up work items through conversation or a visual Kanban board.

Flow turns Claude into a project partner that thinks in backlogs. Say "park this for later" and it queues the idea. Say "what's next?" and it finds the highest-priority ready item and offers to start. It handles the bookkeeping so you stay in flow.

## What It Does

- **Conversational backlog management** — add, reorder, refine, and complete items by talking naturally
- **Agent-initiated items** — the agent adds bugs, tech debt, and follow-ups it discovers while working, linked back to the source task
- **Linked items** — connect related items with typed links (`discovered-during`, `follow-up`, `blocks`, `related`) and a reason, so context isn't lost weeks later
- **Visual Kanban board** — drag-and-drop web UI that syncs with the same `backlog.json`
- **Configurable workflow lanes** — default agile flow (`backlog → refined → ready → in-progress → code-review → done`) or define your own
- **Lane gate rules** — enforce process (e.g., nothing reaches Done without passing through Code Review)
- **Threaded refinement** — structured Q&A threads on items keep discussions organized
- **Multi-agent support** — filtered views for worker agents, assignment tracking, `@Agent` tagging
- **Concurrency safe** — optimistic versioning + atomic writes prevent data loss when multiple agents or the board write simultaneously

## Quick Start

### Install the skill

Copy the `skills/backlog-manager/` folder into your Claude Code skills directory:

```bash
# Available across all your projects (recommended)
cp -r skills/backlog-manager ~/.claude/skills/

# Available in this project only
cp -r skills/backlog-manager .claude/skills/
```

Claude Code auto-discovers skills in these directories — no restart needed.

### Start using it

Just talk to Claude:

```
"Add OAuth2 support to the backlog"
"Show me the backlog"
"What should I work on next?"
"Move #3 above #1"
"Refine #2 — I think we need to scope it down"
```

### Launch the visual board

```bash
python skills/backlog-manager/scripts/backlog_server.py
```

Opens a Kanban board at `http://localhost:8089` with drag-and-drop cards, threaded conversations, and real-time sync.

## Out of the Box

The repo ships with a starter `backlog.json` — zero items, but fully configured with the default workflow lanes and gate rules. You can see exactly how the board is structured before adding anything:

```
backlog → refined → ready → in-progress → code-review → done
```

Gate rules are pre-configured: items must pass through each stage in order (e.g., nothing reaches Done without Code Review). Customize the lanes anytime by editing the `statuses` array in `backlog.json` — or just start adding items and the defaults will work.

## How It Works

Everything lives in a single `backlog.json` at your project root. Items are stored in an ordered array — position = priority. The file is the source of truth for both the CLI agent and the web board.

```
backlog.json
├── version        # Optimistic concurrency counter
├── config
│   ├── scope      # "project" or "global" (for monorepos)
│   ├── project_name
│   └── statuses[] # Configurable workflow lanes with gate rules
└── items[]        # Ordered by priority (index 0 = highest)
    ├── id, title, description, status
    ├── assigned_to
    ├── threads[]   # Refinement Q&A
    ├── links[]     # Connections to related items (type + reason)
    ├── lane_history[] + gate_from  # Audit trail + rule engine
    └── created_at, updated_at
```

### Lane Gate Rules

Lanes can require items to have passed through specific prior lanes before entering. This is enforced at three layers: the web board (blocked drop zones), the server API (HTTP 422), and the agent instructions.

```json
{ "id": "done", "label": "Done", "requires": ["code-review"] }
```

An item can't be marked Done unless it's been through Code Review. Moving backward resets the watermark — the item must re-earn gates on its new journey.

### Concurrency

Multiple agents or the web board can write simultaneously without data loss:

- Every write increments a `version` field
- Stale writes are rejected with HTTP 409
- All file writes are atomic (temp file + rename)

## Project Structure

```
Flow/
├── .claude-plugin/
│   └── plugin.json           # Plugin manifest for distribution
├── skills/
│   └── backlog-manager/
│       ├── SKILL.md              # Skill instructions (the brain)
│       ├── scripts/
│       │   └── backlog_server.py # REST API + web board server
│       ├── assets/
│       │   └── backlog-board.html # Kanban board UI
│       ├── references/
│       │   └── schema.md         # JSON schema documentation
│       └── evals/
│           ├── evals.json        # Test cases and assertions
│           └── files/            # Fixture data for evals
└── backlog.json              # Your backlog (zero items, ready to use)
```

## Board Features

- Drag-and-drop cards between lanes
- Click cards to edit — full modal with all fields
- Threaded conversations with `@Agent` tagging
- Linked items — view, add, and manage connections between items with type badges and reasons
- Assignment dropdown
- Position numbers, timestamps, unresolved thread counts, link counts
- Auto-refreshes every 5 seconds
- Keyboard shortcuts: `N` to add, `Esc` to close

## Configuration

### Workflow Lanes

Define your own columns in `backlog.json` config:

```json
"statuses": [
  { "id": "backlog",     "label": "Backlog" },
  { "id": "design",      "label": "Design Review" },
  { "id": "ready",       "label": "Ready" },
  { "id": "in-progress", "label": "In Progress" },
  { "id": "qa",          "label": "QA",          "requires": ["in-progress"] },
  { "id": "done",        "label": "Done",        "requires": ["qa"] }
]
```

### Scope

- **`project`** (default) — `backlog.json` lives in the project root
- **`global`** — `backlog.json` lives at `~/.claude/backlog.json`, useful for monorepos or cross-project task lists

## License

MIT
