---
name: backlog-manager
description: Manage a project backlog — add, prioritize, refine, and pick up work items. Use when the user mentions backlog, tasks, sprints, or work queues. Also triggers proactively after task completion and when bugs or tech debt are discovered during work.
---

# Backlog Manager

## Activation Hints

Invoke this skill when the user:
- Mentions backlog, sprint planning, task queue, work items, story grooming, or agile workflow
- Says things like "add this to the list for later", "what should I work on next", "let me park this idea", "queue this up", "what's in my backlog", "link this to #3", "which agent should take this", or "what's blocking progress"
- Asks about prioritizing, refining, or picking up work
- Says "assign #N to <agent>" — this means spawn the agent and drive the item to completion, not just update a field
- References an item by number (#N) in any action context (assign, move, review, merge, check status)

## Intent vs. Literal Input

Never take backlog commands purely literally. Resolve intent first:
- **"assign #N to X"** → set `assigned_to`, then spawn agent X to actually do the work and drive through review → merge → done
- **Obvious typos in agent names** (e.g. "sugagent", "subagnet") → silently correct to the intended name and proceed. Do not confirm the typo back to the user.
- **"is it merged?" / "is X working on it?"** → check status AND assess whether the right action has actually been taken (agent spawned, PR open, etc.) — not just report the raw field value
- **"let me know when it's ready"** → treat as implicit delegation: drive the work, don't just watch

Invoke proactively (without being asked) when:
- You've just finished a task and the user hasn't given you a new one → generate a work brief and offer to pick up the top-scored item
- You discover bugs, tech debt, or follow-ups while working → add them to the backlog and link them to the source task
- An item enters or is already in `code-review` status (after any write, or when scanning the backlog) → for each `code-review` item with no unresolved `review-dispatched` sentinel thread: (1) run `backlog handoff reviewer --item N --review`, (2) immediately run `backlog ingest <result_file>` on the file it prints ("Result saved to …"). Do not wait for the user to ask. The ingest step is what actually advances the item to `done` (pass) or back to `in-progress` (reject) — skipping it leaves the item stuck in `code-review`.

> **Code review shortcut:** When an item is in `code-review`, the only valid path is `backlog handoff reviewer --item N --review` → `backlog ingest <result_file>`. Do not spawn a reviewer agent directly and call `backlog done` — that bypasses the gate.

---

## Quick Start

This is all you need 80% of the time:

1. `backlog top` — see what's next
2. `backlog pick <your-name>` — claim the top ready item → moves it to `in-progress`
3. Do the work
4. `backlog move N code-review` — when done
5. `backlog handoff reviewer --item N --review` → `backlog ingest <result_file>` — closes the review gate
6. Blocked? Open a thread on the item, set `waiting_on: "user"`, notify the user

Full protocols for code review, spec gate, delegation, and orchestration are below.

---

## Setup

The skill ships as an installable Python package. Install once per machine:

```bash
cd <skill-path>
pip install -e .
```

Then run once per project to create `backlog.json` and wire up `CLAUDE.md`:

```bash
backlog init
```

That's it. No env vars, no extra steps. `backlog` defaults to `./backlog.json` in your project root.

> **Backup:** Copy `backlog.json` before upgrading the skill or making bulk edits. There is no automatic migration.

**Which interface to use:**
- Agents and scripts → `backlog` CLI
- Visual management → `backlog board`
- Natural language → Claude skill (this file)

---

## How to Operate

Agents do not need the server running. Use the CLI directly.

1. **Pick up work:** `backlog pick <your-agent-name>` — moves top ready item to in-progress
2. **Decompose** the work into tasks — add via CLI or API with dependencies and links
3. **Assign** based on team (`.claude/agents/`) using assignment intelligence
4. **Delegate** to sub-agents with handoff protocol (see Delegating to a Sub-Agent below)
5. **Monitor** via `backlog top` (no server) or `/api/pulse` (if server running) — act on `_events` after every write. Use `/api/graph` to surface dependency order and critical path before assigning work.
6. **Prune personas** — after task completion, review sub-agent persona files: remove duplicates, merge similar learnings, trim anything now obvious from the codebase

**Start the web board** (human visual use only):
```bash
backlog board       # port 8089
```

## Decision Hierarchy

```
Gates (hard stop) → Readiness (eligibility) → Tribunal (recommendation) → Policies (overrides)
```

- **Gates** prevent invalid status moves. Non-negotiable.
- **Readiness** determines if work is startable (>=70%) or fully ready (>=90%).
- **Tribunal** recommends what to do next based on 6 lenses (urgency, leverage, agent_fit, risk, momentum, strategic).
- **Policies** can override or nudge — they fire after tribunal and can escalate, reassign, or block.

## Decomposing a Feature

When the user describes a feature to build:

1. Identify distinct units of work (each should be completable by one agent)
2. Set dependencies — use `blocks` links for sequential work, no link for parallel work
3. Assign complexity: `low` (< 1 hour), `medium` (1-4 hours), `high` (4+ hours)
   - Model routing: `low` → haiku · `medium` → sonnet · `high` → opus (advisory — shown in work brief). The spawning agent is responsible for selecting the model when delegating to a sub-agent.
4. Set `tags` for skill matching (e.g., backend, frontend, auth, testing)
5. Set `priority_weight` based on how much value this item unlocks — consider both dependency depth (items that unblock the most work) and business importance. If a Strategic focus is declared, weight items matching it higher
6. Add all items via API, dependencies first so links resolve correctly

## Code Review Protocol

**NEVER spawn a reviewer agent directly.** The correct flow is always:
1. `backlog handoff reviewer --item N --review` — generates a structured handoff file
2. `backlog ingest <result_file>` — advances the item to `done` (pass) or back to `in-progress` (reject)

Skipping step 1 and spawning an Agent tool call directly produces an unstructured markdown file that `backlog ingest` cannot parse. The item will appear done but the gate was never properly closed. This is a protocol violation — not an acceptable shortcut.

**When running inside Claude Code** (nested `claude` sessions are blocked): `backlog handoff reviewer --item N --review` will print the review prompt to stdout instead of launching a subprocess. When you see `--- REVIEW PROMPT ---` in the output, execute the review inline using that prompt, write the artifact to `handoff_results/review_<item_id>_<timestamp>.json` in the exact format specified below, then run `backlog ingest <result_file>`.

When reviewing an item in `code-review`, a **reject verdict blocks the merge**. Do not pass and log a follow-up ticket — if you can see the bug, it must be fixed before done.

**Step 1 — Socratic questions (before reading the checklist):**

Read the item's acceptance criteria and `## Spec` block, then generate the questions a skeptical senior engineer would ask about *this specific implementation* — derived from the actual code and spec, not a generic template. Answer each question inline as you review. These questions are the ceiling; the static checklist below is the floor.

If the item has no acceptance criteria or `## Spec` block (e.g. user created and moved to ready manually without going through the spec gate), stop and ask the user for the acceptance criteria before proceeding. Do not skip the Socratic step silently.

Example questions for a concurrent write operation:
- *What happens if two agents pick this item simultaneously?*
- *Is the version check atomic with the write?*
- *What does the caller see if the write fails halfway through?*

**Step 2 — Review checklist:**

1. **Correctness** — does the implementation match the acceptance criteria exactly?
2. **Idempotency** — any code that runs in a loop, on a recurring tick, or in response to repeated events must be safe to call multiple times without side effects. If it dispatches, writes, or creates something, check it guards against doing that twice.
3. **Error handling** — failures must be graceful. Silent failures that cause the caller to retry infinitely are bugs, not acceptable behavior.
4. **Edge cases** — what happens when the list is empty, the file doesn't exist, the agent isn't configured, or the external call times out?
5. **Write safety** — any write to `backlog.json` must re-read first, use `expected_version`, and go through `apply_lane_transition` for status changes (never raw field writes).
6. **Test/eval coverage** — every new CLI command or behavior change must have a corresponding eval or integration test. Missing coverage is a **blocker**. Check `evals/evals.json` for skill behavior tests and `evals/` for CLI integration tests. A passing implementation with no test is not shippable.

**Severity tiers** — classify every finding before issuing a verdict:
- **blocker** — must fix before merge (correctness bug, security issue, data loss, broken contract)
- **important** — real pain if it ships; gets a follow-up thread but does not block
- **nit** — style/naming/minor inconsistency; mentioned once, never blocks

**Verdict:**
- `pass` — zero blockers. Important issues and nits are noted in the review artifact and follow-up threads.
- `reject:<blocker summary>` — one or more blockers found. Move item back to `in-progress` with a thread listing only the blockers and exactly what must change.

**Review artifact** — write `handoff_results/review_<item_id>_<timestamp>.json` after every review (pass or reject):
```json
{
  "item_id": "<item_id>",
  "verdict": "pass | reject",
  "summary": "...",
  "issues": [
    {
      "description": "...",
      "severity": "blocker | warning"
    }
  ]
}
```

A reviewer who passes code with a known bug and logs a follow-up ticket has failed at their job.

---

## Git Workflow

All code changes must go through a branch → PR → merge flow. **Never push directly to `main`.**

**Required steps for any code-producing task (subagent):**
1. Create a feature branch: `git checkout -b <item-id>-<short-description>`
2. Do the work and commit on that branch
2a. Run the full test suite locally before pushing:
    - JS/TS: `npm test` or `npx jest --ci`
    - Python: `pytest tests/`
    - If the test command is unknown: skip with a warning note in the PR description
    - If tests fail: fix the code before pushing — do not open a red PR
    - If no test suite exists yet: skip and note it in the PR description
    This step is mandatory. CI is a second confirmation, not the first signal.
3. Push the branch: `git push -u origin <branch>`
4. Open a PR targeting `main` using this body format:
   ```
   gh pr create --title "<concise title>" --body "$(cat <<'EOF'
   ## What changed
   <1-3 bullets describing the change>

   ## Why
   <motivation — backlog item #N, bug fix, etc.>

   ## Test plan
   <how to verify: what tests cover this, what to check manually if any>

   Closes backlog item #N
   EOF
   )"
   ```
5. Report the PR URL to the lead agent — do not merge, do not wait for CI

**Lead agent — PR monitoring and merge protocol:**
1. After a subagent reports a PR URL, check if CI is configured: `gh pr checks <PR-number>`
2. If no CI checks are present (project has no CI yet): skip to step 3 and note "no CI configured"
3. If CI is still running: poll until it completes (re-run `gh pr checks` after a short wait)
4. If CI fails: report the failure to the user with the failing step, move the backlog item back to `in-progress`, and ask the subagent to fix
5. **If CI is green (or no CI): run the code review gate — do NOT ask the user about merging yet:**
   - `backlog move N code-review`
   - `backlog handoff reviewer --item N --review`
   - `backlog ingest <result_file>`
   - If rejected: move back to `in-progress`, send fixes to the subagent, restart from step 1
   - If passed: proceed to step 6
6. Report to the user — "PR #N passed review. Ready to merge. Confirm?" — and wait
7. **Only merge after the user explicitly confirms** — `gh pr merge <PR-number> --squash --delete-branch`
8. After merge: the `backlog ingest` on a passing review already moved the item to `done` — verify with `backlog show N`

**Why:** The user is the final gate before anything lands on `main`. The lead agent does the legwork (CI monitoring, status reporting) but never merges autonomously. Squash merge keeps `main` history clean; `--delete-branch` prevents stale branch accumulation.

**Exception:** Pure doc or config changes with zero code risk may go direct to `main` after explicit user approval — but when in doubt, use a PR.

---

## CI Setup (run once per project)

This is **mandatory**, not advisory. Agents must complete this before writing any tests. Invisible test results are not shippable — CI with junit reporting is part of the definition of done for every code task.

### Step 1 — Detect CI state

At the start of any code task, check two things:

1. Does `.github/workflows/` exist in the project root?
2. If a workflow exists, does it include `EnricoMi/publish-unit-test-result-action`?

Run these checks before writing any production code or tests:

```bash
ls .github/workflows/ 2>/dev/null || echo "NO_CI"
grep -r "publish-unit-test-result-action" .github/workflows/ 2>/dev/null || echo "NO_JUNIT_REPORTER"
```

### Step 2 — Detect language

Look for these files to determine the test runner:

| File present | Language | Test runner |
|---|---|---|
| `package.json` | JS/TS | Jest |
| `requirements.txt` | Python | pytest |
| `pyproject.toml` | Python | pytest |
| None of the above | Unknown | Ask the user which language/runner is in use, or skip CI setup with a warning note explaining why it was skipped. |

### Step 3 — Create or patch the workflow

**If `.github/workflows/` does not exist** — create `test.yml` before writing any tests.

For JS/TS projects:

```yaml
name: Test
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
      - run: npm ci
      - run: npx jest --ci --reporters=default --reporters=jest-junit
        env:
          JEST_JUNIT_OUTPUT_FILE: test-results.xml
      - uses: EnricoMi/publish-unit-test-result-action@v2
        if: always()
        with:
          files: test-results.xml
```

Also add `jest-junit` as a devDependency if it is missing:

```bash
grep -q '"jest-junit"' package.json || npm install --save-dev jest-junit || { echo "jest-junit install failed"; exit 1; }
```

For Python projects detected via `requirements.txt`:

```yaml
name: Test
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -r requirements.txt pytest
      - run: pytest tests/ --junit-xml=test-results.xml
      - uses: EnricoMi/publish-unit-test-result-action@v2
        if: always()
        with:
          files: test-results.xml
```

For Python projects detected via `pyproject.toml`:

```yaml
name: Test
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: pip install -e . pytest
      - run: pytest tests/ --junit-xml=test-results.xml
      - uses: EnricoMi/publish-unit-test-result-action@v2
        if: always()
        with:
          files: test-results.xml
```

**If `.github/workflows/` exists but lacks `EnricoMi/publish-unit-test-result-action`** — patch the existing workflow to add the junit reporter step and the publish action. Do not replace the whole file; append only what is missing. Specifically: (1) add `--junit-xml=test-results.xml` (pytest) or `--reporters=jest-junit` with `JEST_JUNIT_OUTPUT_FILE: test-results.xml` (Jest) to the test run step, and (2) append the following step immediately after the test run step:

```yaml
      - uses: EnricoMi/publish-unit-test-result-action@v2
        if: always()
        with:
          files: test-results.xml
```

### Step 4 — Commit the CI file before tests

Commit the workflow file as a standalone commit on the feature branch **before** the test commit:

```bash
git add .github/workflows/test.yml
git commit -m "ci: add test workflow with junit reporting"
```

This ordering matters: if CI is missing and the test commit lands first, the first PR has no coverage signal.

### Definition of done for CI

An item is not `done` until:
- `.github/workflows/` contains a workflow that runs on push and pull_request to main
- The workflow emits a junit XML file
- `EnricoMi/publish-unit-test-result-action@v2` is present with `if: always()` so results appear in the Actions Summary tab even on failure
- The first PR on the project shows CI checks in `gh pr checks <PR>`

If `gh pr checks <PR>` returns no checks after CI was added, the workflow file likely has a syntax error or branch filter mismatch — fix it before marking done.

---

## Delegating to a Sub-Agent

When spawning a sub-agent for an assigned task, include in the prompt:

1. The task details (title, description, acceptance criteria from backlog)
2. Server URL: `http://localhost:8089`
3. Their agent name: use `GET /api/backlog?agent=<name>` to see only your work
4. Status protocol: set `in-progress` when starting, `code-review` or `done` when finishing
5. Blocker protocol: if blocked, open a thread via `PUT /api/items/<id>` and report back
6. Their persona file path (`.claude/agents/<name>.md`) — agent reads it for identity and past learnings
7. Self-correction instruction: if you make a mistake and get corrected, update your persona file before finishing
8. Git workflow: work on a feature branch, push it, open a PR targeting `main` via `gh pr create`, report the PR URL — do not merge directly to `main`

---

## Work Brief Format

```
NEXT:  #N — Title  [score: X | confidence: high]
       Why: Primary reasoning from tribunal lenses
       Assign: agent-name (skill match: auth, backend) | Model: sonnet
       [Startable with risk — weakest blocker at 74% readiness (PR merged, tests pending)]

       Considered but passed:
       · #M — Title [score: Y] — Why not: not ready (status: backlog)
       · #K — Title [score: Z] — Why not: Blocked — weakest blocker at 40% readiness

STARTABLE: Items 70–89% unblocked (can begin exploratory work with known risk):
       · #P — Title — Blocker at 75% (in-progress, PR merged)

WATCH: stale items · skip-escalated items · 3+ reopens in same tag area
UNASSIGNED CRITICAL: critical bugs unassigned > threshold hours
```

Generate after: task completion, reprioritization trigger fires, user asks "what's next?". Use `/api/pulse` to get the data.

## Reprioritization Triggers

Check `_events` in every write response:

| Event | Action |
|---|---|
| `critical_bug_created` | Re-score all. If top-ranked: announce and offer to start. |
| `blocker_resolved` | Re-score unblocked items. Highlight rank jumps. |
| `item_reopened` | Flag: "Reopened (#N). Complexity may be underestimated." At 3+: suggest redesign. |
| `blocks_changed` | Re-score newly blocked items. |

Staleness (agent-side): flag `refined` > 7d or `ready` > 5d in WATCH.
Skip escalation: increment `skip_count` for items scored but not picked. At 5: escalate. Reset to 0 on pick-up.
Cluster detection: 3+ reopens in same tag area within 14d → flag in WATCH.

---

## Operations

> **Lane = status.** Both terms refer to where an item sits in the workflow (`backlog → refined → ready → in-progress → code-review → done`). They are interchangeable in CLI output and docs.

**Add** — Append to end, `status: backlog`, generate 8-char random alphanumeric ID (lowercase letters + digits; not UUID-based, never reused). Default `priority_weight = 5` if not set. Infer `category`, `complexity`, `tags`, `priority_weight` from context — don't ask. Confirm: "Added as #N (category, complexity, [tags])."

**List** — Group by status: `#N title [score: X] (assigned, complexity, category) — flags`

**Edit** — Update any field. Note scoring impact if relevant (e.g., setting `complexity: high` drops score and routes to opus).

**Assign** — Set `assigned_to`. Unassigned = available to all agents.

**Reorder** — Reorder `items` array. Reordering ≠ status change.

**Discard** — `status: discarded`. No gate rules. Restore: move to any lane, resets `gate_from`.

**Complete** — `status: done`. Add brief completion note. Offer next item from ready queue.

**Refine** — Open threads for unclear items (max 2 questions at a time). Resolve threads → suggest `refined`. Always scan for `waiting_on: "agent"` threads — respond and set to `"user"` or null. **Before closing refinement and moving to `ready`, you MUST run the refinement gate below** — propose `refinement_gate: simple` or `refinement_gate: complex`, wait for human confirmation, and (if complex) run the sub-lead-agent readiness review. Do not skip to the spec gate without completing this step.

**Refinement-gate sub-lead-agent review (end of refinement) — REQUIRED before any `→ ready` move:**

This gate is mandatory. Do not move an item to `ready` or run the spec gate without completing it first. After all refinement questions are resolved, do the following:

1. Lead agent proposes `refinement_gate: simple` or `refinement_gate: complex` with a one-line reason:
   - `complex` — touches auth/security/data integrity, spans multiple components, introduces new patterns, has unclear edge cases, or estimate is high
   - `simple` — single-file change, well-understood pattern, clear acceptance criteria, estimate is low or medium
2. Human confirms or overrides the label (`backlog edit N --refinement-gate <simple|complex>`). Sub-lead only triggers **after** the label is finalised.
3. If `refinement_gate: complex`: run a sub-lead-agent readiness review before the spec gate. The checklist:
   - Acceptance criteria are testable, not vague
   - No hidden dependencies on unbuilt or unplanned pieces
   - Estimate is realistic given current codebase state
   - Edge cases and failure modes are called out
   - Scope is clearly bounded with no implicit follow-on work
   - If the review finds issues: open a thread (`waiting_on: "user"`), do **not** move to `ready`. Wait for resolution.
   - If the review passes: proceed to the spec gate as usual.
4. If `refinement_gate: simple`: item moves directly to `ready` via spec gate as usual — no sub-lead review.

In auto mode the orchestrator handles steps 1–4 automatically. In supervised mode the lead agent proposes the `refinement_gate` label to the human, waits for confirmation or override, and then (if complex) runs the sub-lead readiness review inline before proceeding to the spec gate.

**Spec gate (any `→ ready` move):** Before moving any item to `ready` — regardless of its current status — check two things: (1) does a `spec_written` signal exist on the item, or (2) does the item description already contain a `## Spec` block? If either is true, the gate passes — proceed with the `ready` move. If neither exists, surface these three questions and wait for answers before proceeding:
1. *Acceptance criteria* — What does done look like exactly? How will you verify it?
2. *Failure modes* — What happens when this goes wrong? Silent fail, throw, or retry? What does the caller see?
3. *Edge cases* — What are the boundary conditions? (empty inputs, missing files, timeouts, concurrent writes, unexpected state)

Embed the answers as a `## Spec` block appended to the item description, then set the signal: `POST /api/items/<id>/signal {"type": "spec_written", "source": "backlog-manager", "description": "Spec captured at ready gate"}`. Then proceed with the `ready` move.

If the item is reopened and returns to `ready` later, the existing signal or `## Spec` block satisfies the gate — do not re-ask the questions or overwrite the spec.

If the user says "just mark it ready" without answering — acknowledge the risk and ask once more. Do not skip silently. Unspecced items are a leading cause of review rejects and reopens: the reviewer has no ground truth to check against.

**Block** — Open thread, `waiting_on: "user"`, move back to `backlog`. Notify user.

**Pick Up** — Score all items → work brief → confirm with user → set `in-progress` + `assigned_to`. Use `/api/pulse` for recommendation.

**Agent-initiated items** — Add bugs/tech-debt found while working. Set `category`, `complexity`, `tags`, `priority_weight` (bugs only). Link back to source. Announce: "Found X while on #N — added as #M (bug, medium, [tags])."

**Proactive follow-up rule** — When finishing any task, scan for gaps and add them to the backlog immediately — do not ask the user first. This includes:
- Doc gaps created by the work (SKILL.md, README.md, schema.md not updated to reflect new behavior)
- Pre-existing bugs or tech debt surfaced during implementation or review
- Warnings from code review that don't block merge but need future attention
- Any natural consequence of the completed work not yet tracked

Add the item, link it to the source (`follow-up` or `discovered-during`), and mention it in the completion summary. Asking "should I add this?" for obvious follow-up work is unnecessary friction — the user should only be involved for decisions.

**Doctrine-before-feature ordering** — The tribunal scores by `priority_weight` and dependency links. It cannot detect that a low-complexity doctrine or doc item enables better execution of a higher-scored feature unless there is a structural signal. When adding a doc/skill/doctrine item that directly enables an upcoming feature:
1. Add a `blocks` link only if the feature genuinely cannot or should not start without the doctrine in place. Otherwise use `related` — misusing `blocks` inflates leverage scores and corrupts gate rules.
2. Set `priority_weight` to reflect the cascade value, not just the item's own size — a low-effort item that improves every future agent run is worth more than its line count suggests.

## Linking

Use the CLI to add or remove links between items:

```bash
backlog link <source> --type <type> --target <target> --reason "<one sentence why>"
backlog link --list <item>     # show all links for an item
backlog unlink <source> --target <target>
```

Types: `blocks`, `discovered-during`, `follow-up`, `related`. `--reason` is required on every link.

- Use `blocks` only when the dependent item **cannot or should not start** without this one done. Overusing `blocks` inflates leverage scores and corrupts gate rules.
- Use `related` for everything else — parallel work, thematic connections, context links.

```
blocks:  "Auth service must be deployed before login feature can start"
related: "Frontend form is thematically linked to the auth spec but can begin independently"
```

## Concurrency Safety

Server rejects writes where client `version` < current (HTTP 409).
**On 409**: re-read → re-apply → retry. Never manually increment version.

---

> Full CLI reference, API docs, scoring internals, lane gate rules, policy syntax → **[REFERENCE.md](REFERENCE.md)**
