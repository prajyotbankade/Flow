# Changelog

## v0.1.0 — First public release

**Schema version:** 1

### Migration note

If upgrading between versions, back up backlog.json first. Migration guides will appear here when schema breaks.

### What's in this release

- **Work Intelligence Engine** — scored backlog items with priority weighting, complexity tagging, blocking relationships, and staleness detection
- **Scored work briefs** — `backlog top` produces structured recommendations with reasoning, agent assignment, and model routing
- **Reprioritization triggers** — automatic re-scoring when blockers resolve, items reopen, or critical bugs are filed
- **Assignment intelligence** — recommends agents based on skill matching, workload caps, and linked item history
- **Visual Kanban board** — drag-and-drop web UI (`backlog-server`) with score badges, staleness markers, and per-item Intelligence tab
- **Lane gate rules** — configurable gates (e.g., code-review required before done)
- **Multi-agent support** — filtered views, `@Agent` tagging, concurrent write safety via optimistic versioning and atomic writes
- **CLI** — `backlog add`, `backlog top`, `backlog board`, `backlog refine`, `backlog handoff`, `backlog ingest`, `backlog doctor`, and more
- **Claude Code skill** — invoke as `/backlog-manager` (standalone) or `/flow:backlog-manager` (plugin)
- **`backlog init`** — bootstraps a new `backlog.json` with `schema_version: 1`; exits safely if a file already exists
