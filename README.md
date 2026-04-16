# Flow

A Claude Code skill for intelligent project management — an AI-driven work intelligence engine that scores, prioritizes, assigns, and picks up work items through conversation or a visual Kanban board.

Flow turns Claude into a project partner that thinks in backlogs and decides what matters next. Say "park this for later" and it queues the idea with complexity, category, and tags. Say "what's next?" and it generates a scored work brief with reasoning, assignment recommendations, and model routing. It detects when priorities shift, flags stale work, and catches recurring problems before you ask.

## What It Does

- **Work Intelligence Engine** — every item gets a computed score based on priority, blocking relationships, freshness, complexity, and more. The score determines what matters next — not just backlog position
- **Scored work briefs** — instead of "here's the next item", Claude produces structured recommendations with reasoning: why this item, who should work on it, which model to use
- **Reprioritization triggers** — automatic re-scoring when critical bugs are created, blockers are resolved, items are reopened, or blocking relationships change
- **Assignment intelligence** — recommends agents based on skill matching, linked item history, complexity preference, and workload balancing (max active task cap per agent)
- **Model routing** — maps item complexity to recommended AI model (low → haiku, medium → sonnet, high → opus) to avoid spending expensive models on simple tasks
- **Staleness & skip tracking** — flags items stuck too long in refined/ready, and items repeatedly passed over. Prevents backlog rot.
- **Opportunistic redesign detection** — spots patterns (3+ reopens in the same code area) and suggests structural fixes before more patches
- **Conversational backlog management** — add, reorder, refine, and complete items by talking naturally
- **Agent-initiated items** — the agent adds bugs, tech debt, and follow-ups it discovers while working, with complexity, category, tags, and links back to the source task
- **Linked items** — connect related items with typed links (`discovered-during`, `follow-up`, `blocks`, `related`) and a reason, so context isn't lost weeks later
- **Visual Kanban board** — drag-and-drop web UI with score badges, complexity indicators, category labels, staleness markers, and an Intelligence tab per item
- **Configurable workflow lanes** — default agile flow (`backlog → refined → ready → in-progress → code-review → done`) or define your own
- **Lane gate rules** — enforce process (e.g., nothing reaches Done without passing through Code Review)
- **Threaded refinement** — structured Q&A threads on items keep discussions organized
- **Multi-agent support** — filtered views for worker agents, assignment tracking, `@Agent` tagging, configurable agent profiles with skills and capacity
- **Concurrency safe** — optimistic versioning + atomic writes prevent data loss when multiple agents or the board write simultaneously

## Quick Start

There are two ways to install Flow depending on how you want to use it.

### Option A — Standalone install (single user, simplest)

Copy the skill directly into your Claude Code skills directory:

```bash
# Available across all your projects (recommended)
cp -r skills/backlog-manager ~/.claude/skills/

# Available in this project only
cp -r skills/backlog-manager .claude/skills/
```

Claude Code auto-discovers skills in these directories — no restart needed. Invoke the skill as **`/backlog-manager`**.

### Option B — Plugin install (via marketplace)

Once available in the Anthropic marketplace, install with:

```
/plugin install flow
```

After installation, the skill is invoked as **`/flow:backlog-manager`**.

### Test locally (developers)

To try the plugin without installing it:

```bash
claude --plugin-dir .
```

Then invoke as `/flow:backlog-manager` within that session.

### Start using it

Just talk to Claude — no command needed once the skill is active:

```
"Add OAuth2 support to the backlog"
"Show me the backlog"
"What should I work on next?"
"Move #3 above #1"
"Refine #2 — I think we need to scope it down"
```

### Install the CLI package

The skill ships as an installable Python package. Install it once to get the `backlog` CLI and `backlog-server` commands:

```bash
cd skills/backlog-manager    # or ~/.claude/skills/backlog-manager after standalone install
pip install -e .
```

Set your backlog file once (or pass `--file` on every command):

```bash
export BACKLOG_FILE=/path/to/your/backlog.json
```

Now agents and scripts can use the CLI directly — no server required:

```bash
backlog list                  # show board
backlog add "Fix login bug"   # add item
backlog pick alice            # pick top ready item, move to in-progress
backlog move 3 in-progress    # lane transition (gate rules enforced)
backlog done 3                # complete
```

### Launch the visual board

```bash
backlog board                  # uses BACKLOG_FILE, opens at http://localhost:8089
backlog-server --file backlog.json --port 8089   # explicit, same thing
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
├── version           # Optimistic concurrency counter
├── config
│   ├── scope         # "project" or "global" (for monorepos)
│   ├── project_name
│   ├── statuses[]    # Configurable workflow lanes with gate rules
│   ├── scoring       # Intelligence engine weights (all configurable)
│   ├── agents        # Agent profiles: skills, max_active, preferred_complexity
│   ├── thresholds    # Staleness days, critical unassigned hours, skip escalation
│   └── model_routing # Complexity-to-model mapping (low→haiku, medium→sonnet, high→opus)
└── items[]           # Ordered by priority (index 0 = highest)
    ├── id, title, description, status
    ├── assigned_to
    ├── complexity     # low | medium | high — drives scoring + model routing
    ├── priority_weight # 1-10 explicit priority (overrides position)
    ├── category       # bug | feature | chore | tech-debt — critical bugs trigger reprioritization
    ├── tags[]         # Free-form tags for skill matching (e.g., "auth", "frontend")
    ├── reopen_count   # Auto-incremented when moved back from done
    ├── skip_count     # Tracks how many times item was passed over
    ├── readiness_signals[]  # Artifact evidence (spec_written, pr_merged, etc.)
    ├── threads[]      # Refinement Q&A
    ├── links[]        # Connections to related items (type + reason)
    ├── lane_history[] + gate_from  # Audit trail + rule engine
    └── created_at, updated_at
```

### Lane Gate Rules

Lanes can require items to have passed through specific prior lanes before entering. This is enforced at three layers: the web board (blocked drop zones), the server API (HTTP 422), and the agent instructions.

```json
{ "id": "done", "label": "Done", "requires": ["code-review"] }
```

An item can't be marked Done unless it's been through Code Review. Moving backward resets the watermark — the item must re-earn gates on its new journey.

**Spec gate** — a complementary soft gate enforced at the skill level: before any item moves to `ready` (from any prior status), the skill requires a written spec covering acceptance criteria, failure modes, and edge cases. Answers are embedded in the item description as a `## Spec` block and the `spec_written` readiness signal (+10% readiness) is set by the skill. The gate is bypassed if either the signal or a `## Spec` block already exists — so reopened items that return to `ready` don't get re-questioned. Unlike lane gate rules (which are structural), the spec gate is semantic — it ensures the work is understood before it enters the execution queue. Items that skip the spec are a leading cause of review rejects and reopens.

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
│       ├── pyproject.toml        # Package config — installs backlog + backlog-server CLI
│       ├── backlog/              # Core Python package
│       │   ├── core.py           # BacklogStore — all gate/versioning/CRUD logic
│       │   ├── exceptions.py     # GateViolationError, ConflictError, ItemNotFoundError
│       │   ├── cli.py            # Typer CLI (backlog command)
│       │   └── server.py         # HTTP server (backlog-server command)
│       ├── scripts/
│       │   └── backlog_server.py # Legacy server (kept for reference)
│       ├── assets/
│       │   └── backlog-board.html # Kanban board UI
│       ├── references/
│       │   └── schema.md         # JSON schema documentation
│       └── evals/
│           ├── evals.json           # Test cases and assertions
│           ├── eval_flow_skill.py   # deepeval skill harness (calls live API + Ollama)
│           ├── test_flow_live.py    # deepeval benchmark suite (AnswerRelevancy + GEval)
│           ├── test_cli.py          # CLI integration tests (11 scenarios, no server needed)
│           ├── files/               # Fixture data for evals
│           └── results/             # Test run outputs
└── backlog.json              # Your backlog (zero items, ready to use)
```

## Board Features

- Drag-and-drop cards between lanes
- **Intelligence score badges** on every card (color-coded: green >7, yellow 4-7, gray <4)
- **Complexity dots** (green/yellow/red) and **category badges** (BUG, FEAT, CHORE, DEBT)
- **Staleness indicators** — amber border on items past threshold
- **Sort toggle** — switch between position-based and score-based ordering
- Click cards to edit — full modal with all fields including complexity, category, priority weight, and tags
- **Intelligence tab** in modal — score breakdown, recommended agent, recommended model, reopen/skip counts
- Threaded conversations with `@Agent` tagging
- Linked items — view, add, and manage connections between items with type badges and reasons
- Assignment dropdown
- **Agent profiles in settings** — define skills, max active tasks, and preferred complexity per agent
- **Scoring weights in settings** — tune all intelligence engine parameters
- Position numbers, timestamps, unresolved thread counts, link counts
- Auto-refreshes every 5 seconds (scores refresh with each cycle)
- Keyboard shortcuts: `N` to add, `Esc` to close

## Work Intelligence Engine

Flow doesn't just track work — it decides what matters next. Every item gets a computed score:

```
score = base_priority + unblock_value + freshness + complexity_bonus
      + blocked_penalty + quick_win_bonus + reopen_penalty
      + skip_floor + critical_bug_boost
```

Items that unblock multiple others score highest. Quick wins get a momentum bonus. Stale items get penalized. Critical bugs get a +5.0 boost that dominates the ranking.

### Work Briefs

Instead of "here's the next item", Claude produces structured work briefs:

```
=== WORK BRIEF ===

NEXT: #3 — Fix auth timeout
  Score: 9.2 | Blocks: #7, #11 | Age: 4d in ready
  Assign: Agent-B (worked on auth in #1, load: 1/3)
  Model: sonnet (medium complexity)
  Why: Unblocks 2 critical items, freshness penalty kicking in

THEN: #4 — Add retry logic
  Score: 7.1 | Quick win (low complexity)
  Why: Clears simple item, maintains momentum

WATCH:
  #12 — Stuck in refined 9d. Kill or promote?
  Module /auth — 3 reopens in 14d. Consider redesign.
```

### Reprioritization Triggers

The server automatically detects events and returns `_events` in write responses:
- **`critical_bug_created`** — new bug with priority ≥ 9
- **`blocker_resolved`** — item with `blocks` links moved to done
- **`item_reopened`** — item moved backward from done (auto-increments `reopen_count`)
- **`blocks_changed`** — item gained new blocking relationships

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/backlog` | Full backlog (or `?agent=name` for filtered view) |
| `GET /api/scores` | All items with computed scores, breakdowns, and recommendations |
| `GET /api/agents` | Agent profiles with current load |
| `PUT /api/backlog` | Full write (version-checked, returns `_events`) |
| `PUT /api/items/<id>` | Single item update (version-checked, returns `_events`) |

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

### Agent Profiles

Define agent capabilities in `config.agents` for intelligent assignment:

```json
"agents": {
  "lead-dev": {
    "role": "lead",
    "skills": ["python", "api"],
    "max_active": 1
  },
  "worker-auth": {
    "skills": ["auth", "backend", "security"],
    "max_active": 3,
    "preferred_complexity": ["medium", "high"]
  },
  "worker-ui": {
    "skills": ["frontend", "css", "ux"],
    "max_active": 2,
    "preferred_complexity": ["low", "medium"]
  }
}
```

`"role": "lead"` designates the agent that drives the full dev cycle in auto mode. Exactly one agent may have this role.

`"role": "reviewer"` designates the dedicated code review agent. The orchestrator prefers it for all review handoffs. Its persona file (`.claude/agents/reviewer.md`) carries persistent learnings about what to catch.

Also configurable from the board's Settings modal.

### Orchestrator

`backlog orchestrate` is a persistent process that drives the dev cycle after items are ready:

```bash
backlog orchestrate                # supervised mode — acts on ready+ items only
backlog orchestrate --mode auto    # auto mode — lead agent picks, refines, and starts work
backlog orchestrate --once         # single tick and exit (useful for testing)
backlog orchestrate --dry-run      # print planned actions without invoking agents
```

**Supervised mode (default):** Human moves items to `ready`. Orchestrator picks up from there — assigns agents, drives through all lanes including review, processes results.

**Auto mode:** Lead agent continuously picks the highest-priority unstarted item, assesses whether it's actionable, and either moves it to `ready` (orchestrator picks it up immediately) or asks the human targeted questions via a thread. Loops until stopped (`Ctrl+C`).

**Orchestrator config:**

```json
"orchestrator": {
  "mode": "supervised",
  "require_review": true
}
```

`require_review: true` (default) — every item must be reviewed by a different agent before reaching done, even if no code-review lane is configured. Set to `false` with caution — a warning is printed at startup.

### Scoring Weights

All scoring parameters are tunable in `config.scoring`:

```json
"scoring": {
  "unblock_weight": 2.0,
  "blocked_penalty": -3.0,
  "quick_win_bonus": 1.0,
  "critical_bug_boost": 5.0,
  "freshness_decay_days": 14,
  "skip_floor_per": 0.3
}
```

See `references/schema.md` for the full list of configurable weights and thresholds.

### Eval environment setup (one-time)

DeepEval 3.9+ requires Python 3.10+. The system Python on macOS is 3.9 and will fail with a
`TypeError: unsupported operand type(s) for |` error. Use the Homebrew Python 3.12 venv instead:

```bash
# Create the venv once
/opt/homebrew/bin/python3.12 -m venv skills/backlog-manager/evals/.venv
source skills/backlog-manager/evals/.venv/bin/activate
pip install -r skills/backlog-manager/evals/requirements.txt
```

After that, always activate the venv before running evals:

```bash
source skills/backlog-manager/evals/.venv/bin/activate
```

### To run stress test
```bash
lsof -ti :8089 | xargs kill -9
find skills/backlog-manager/scripts -name "*.pyc" -delete
find skills/backlog-manager/scripts -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

python3 skills/backlog-manager/scripts/backlog_server.py --file stress-tests/backlog_stress_2000.json &

until curl -sf http://localhost:8089/api/backlog > /dev/null; do sleep 0.5; done

echo "--- Scores (2000) ---"
time curl -s http://localhost:8089/api/scores | wc -l

echo "--- Recommend (2000) ---"
time curl -s http://localhost:8089/api/recommend | wc -l

echo "--- Graph (2000) ---"
time curl -s http://localhost:8089/api/graph | wc -l

echo "--- Pulse (2000) ---"
time curl -s http://localhost:8089/api/pulse | wc -l

```

### To run all evals (full suite)
```bash
source skills/backlog-manager/evals/.venv/bin/activate
lsof -ti :8089 | xargs kill -9
find skills/backlog-manager/scripts -name "*.pyc" -delete
find skills/backlog-manager/scripts -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

python3 skills/backlog-manager/scripts/backlog_server.py &

until curl -sf http://localhost:8089/api/backlog > /dev/null; do sleep 0.5; done

cd skills/backlog-manager/evals
EVAL_LLM=openai python3 -m pytest test_flow_live.py -v 2>&1 | tee results/test_results_$(date +%Y%m%d_%H%M%S).txt
```

### To run tribunal ties fixture tests
```bash
source skills/backlog-manager/evals/.venv/bin/activate
lsof -ti :8089 | xargs kill -9
find skills/backlog-manager/scripts -name "*.pyc" -delete
find skills/backlog-manager/scripts -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

python3 skills/backlog-manager/scripts/backlog_server.py &

until curl -sf http://localhost:8089/api/backlog > /dev/null; do sleep 0.5; done

cd skills/backlog-manager/evals
python3 -m pytest test_flow_live.py::TestTribunalTiesFixture -v
```

### To run critical path fixture tests
```bash
source skills/backlog-manager/evals/.venv/bin/activate
lsof -ti :8089 | xargs kill -9
find skills/backlog-manager/scripts -name "*.pyc" -delete
find skills/backlog-manager/scripts -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null; true

python3 skills/backlog-manager/scripts/backlog_server.py &

until curl -sf http://localhost:8089/api/backlog > /dev/null; do sleep 0.5; done

cd skills/backlog-manager/evals
python3 -m pytest test_flow_live.py::TestCriticalPathFixture -v
```



## License

MIT
