# Backlog Manager

An AI-native project management skill for Claude Code. It replaces static task lists with a work intelligence engine that scores items, enforces workflow gates, routes work to the right agent, and tells you exactly what to do next — in one command.

If you've ever had Claude spin on the wrong thing, rediscover a problem it already fixed, or forget what was in progress when the conversation ended, this is the fix.

---

## What Is This?

A plain task list doesn't know that the auth bug blocks three other items. It doesn't know that the OAuth feature has been sitting in "ready" for nine days and is starting to rot. It doesn't know that your frontend agent is already at capacity.

Backlog Manager does. Every item gets a computed score based on priority, blocking relationships, complexity, freshness, and skip history. Claude reads that score and produces a ranked work brief with reasoning — which item, why now, which agent, which model. The score changes automatically when critical bugs are filed, blockers clear, or items are repeatedly passed over.

It's the difference between "here's your task list" and "here's what to ship next and why."

---

## Core Concepts

**Lanes** — Items move through a configurable workflow: `backlog → refined → ready → in-progress → code-review → done`. You define the lanes; the system enforces them.

**Scoring / Tribunal** — The intelligence engine scores every item across six lenses: urgency, leverage (how many items does this unblock?), agent fit, risk, momentum, and strategic value. Items that unblock others score highest. Critical bugs get a +5.0 boost that dominates the ranking. Stale items decay. Quick wins get a momentum bonus.

**Gates** — Lane transitions are enforced at three layers: the board UI, the server API, and the agent instructions. Nothing reaches Done without passing Code Review. Items must earn their way to Ready through a spec gate (acceptance criteria + edge cases). Gates are non-negotiable — they exist because skipping them is the leading cause of review rejects and reopens.

---

## Quick Start

**Requirements:** Python 3.11+, pip

```bash
# 1. Clone the repo (or copy the skill directory)
git clone https://github.com/prajyotbankade/Flow.git
cd Flow

# 2. Install the CLI
cd skills/backlog-manager
pip install -e .

# 3. Initialize your project backlog
cd /your/project
backlog init
```

That's it. `backlog init` creates `backlog.json` at your project root and wires up `CLAUDE.md` so every agent on the project knows the backlog exists.

```bash
# See what to work on next
backlog top

# Claim the top item and move it to in-progress
backlog pick yourname

# When done, move it through the gate
backlog move 3 code-review
```

---

## CLI

The `backlog` command is the primary interface for agents and scripts. No server required.

| Command | What it does |
|---------|--------------|
| `backlog top` | Ranked list of what to work on next — the starting point for every session |
| `backlog pick <name>` | Claim the top ready item and move it to in-progress |
| `backlog move N <lane>` | Transition an item to a new lane (gate rules enforced) |
| `backlog add "Title"` | Add an item to the backlog |
| `backlog show N` | Full detail for item #N |
| `backlog list` | Board view grouped by lane |
| `backlog handoff reviewer --item N --review` | Trigger the code review gate — generates a structured handoff file |
| `backlog ingest <result_file>` | Process a review result — advances to done (pass) or in-progress (reject) |
| `backlog link N --type blocks --target M` | Connect related items with typed links |
| `backlog ref N --system jira --id PROJ-123 [--url ...]` | Pair an item with an external ticket (JIRA/GitHub/etc.); `--list` / `unref` to view/remove |
| `backlog orchestrate` | Persistent process that drives the dev cycle automatically |
| `backlog doctor` | Check and repair project setup |
| `backlog install-hook` | Install or refresh the pre-commit integrity-gate hook |

For the full command reference including flags, exit codes, API endpoints, scoring weights, and configuration options, see [REFERENCE.md](REFERENCE.md).

---

## Board

```bash
backlog board        # opens at http://localhost:8089
backlog board --port 9000
backlog board --no-git-check   # suppress the uncommitted-changes hint at startup
```

The board persists every edit to `backlog.json` atomically on disk but never touches git — it does not `git add` or `git commit`. Committing the backlog to history is a deliberate user action; commit `backlog.json` alongside your normal commits whenever you want a checkpoint. If it has uncommitted changes, the board prints a one-line hint at startup (suppress with `--no-git-check` / `BACKLOG_NO_GIT_CHECK`).

The visual management layer. Drag-and-drop Kanban cards with score badges (color-coded by urgency), complexity indicators, category labels, and staleness markers. Click any card to open the full edit modal — fields, threads, linked items, and an Intelligence tab showing the score breakdown, recommended agent, and recommended model.

Designed for human oversight. Agents use the CLI directly — no server required for any agent operation.

---

## Multi-branch / Multi-agent Setup

When multiple agents work on different feature branches, `backlog.json` forks — each branch carries its own committed copy. The fix is one canonical copy that never moves with code branches.

```bash
# Create a worktree pinned to trunk (one-time, per clone)
git worktree add ../<repo>-backlog main

# Point every agent and the board at the canonical copy
export BACKLOG_FILE=../<repo>-backlog/backlog.json
```

**Rule:** backlog bookkeeping always targets the canonical worktree copy — never a feature-branch copy. Code work stays on feature branches as normal.

Run `backlog doctor` to verify your setup. It warns when the committed `backlog.json` on your current branch has diverged from trunk and shows the exact command to fix it.

See [REFERENCE.md](REFERENCE.md) for the full pattern including how to commit history from the canonical worktree.

---

## How Claude Uses This

Install the skill once and Claude activates it automatically when you mention backlog, tasks, sprint planning, or work items. It also triggers proactively:

- After finishing a task — generates a work brief and offers to pick up the next item
- When it discovers bugs or tech debt while working — adds them to the backlog with links back to the source
- When an item enters code-review — runs the review gate immediately without being asked

The skill understands intent, not just commands. "What's next?" produces a scored work brief with reasoning. "Assign #7 to the backend agent" spawns the agent and drives the item through to completion — not just updates a field.

---

## Links

- [SKILL.md](SKILL.md) — Claude's instruction set for this skill. Covers the full operating protocol: code review gate, spec gate, delegation, orchestration, and proactive triggers.
- [REFERENCE.md](REFERENCE.md) — Full CLI reference, API endpoints, scoring internals, configuration schema, and lane gate documentation.
