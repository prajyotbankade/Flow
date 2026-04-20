# Project Rules

## Git Commits
- NEVER add a Co-Authored-By line to commit messages. No co-author attribution — commits are authored solely by the user.

## Stress Tests
- NEVER read files inside the `stress-tests/` folder. They contain 200–500 item datasets and will waste tokens unnecessarily.
- To understand how stress tests work, refer to the `### To run stress test` section in README.md.

## code-review-graph (MCP Server)

A knowledge graph of the codebase is available via MCP. Default to NOT calling graph tools — only use them for the specific cases below.

**Use graph tools when:**
- `detect_changes_tool` — before code review, to get risk-scored blast radius
- `get_impact_radius_tool` — when editing a file and you need to know cross-file impact
- `semantic_search_nodes_tool` — when you don't know where something lives (unfamiliar area)
- `get_review_context_tool` — when reviewing multi-file changes, for token-efficient context

**Do NOT use graph tools when:**
- You already know the exact file and function to edit
- The task is docs-only, config, backlog management, or git operations
- Single-file edit with obvious scope — just edit it
- You just need to read a known file — use Read directly
- You're running tests or CLI commands

<!-- flow-backlog-setup -->
## Flow Backlog

This project uses the Flow backlog manager skill.

- **What to work on next:** `BACKLOG_FILE=./backlog.json backlog top`
- **Never reason about priorities yourself** — always check the backlog first
- **All backlog commands:** prefix with `BACKLOG_FILE=./backlog.json` or set the env var once:
  `export BACKLOG_FILE=./backlog.json`
- **First time on a session:** run `backlog top` to orient, then pick up the top item
<!-- end flow-backlog-setup -->
