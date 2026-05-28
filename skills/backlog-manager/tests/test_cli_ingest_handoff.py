"""Integration tests for backlog ingest and handoff CLI commands."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()

# ── Helpers ───────────────────────────────────────────────────────────────────

_ITEM_ID = "abcd1234"
_ITEM_ID_2 = "efgh5678"


def make_backlog(*items_data) -> dict:
    """Create a minimal backlog dict.

    Each element of items_data is either:
    - a str  → title, status defaults to 'backlog'
    - a dict → full item override (must include at least 'title')
    """
    items = []
    for i, spec in enumerate(items_data):
        if isinstance(spec, str):
            spec = {"title": spec}
        item = {
            "id": spec.get("id", f"item{i:04d}ab"),
            "title": spec.get("title", f"Item {i}"),
            "status": spec.get("status", "backlog"),
            "priority": spec.get("priority", "medium"),
            "priority_weight": spec.get("priority_weight", 5),
            "complexity": spec.get("complexity", "medium"),
            "category": spec.get("category", None),
            "tags": spec.get("tags", []),
            "description": spec.get("description", ""),
            "assigned_to": spec.get("assigned_to", None),
            "links": [],
            "threads": [],
            "lane_history": [],
            "execution_history": [],
            "gate_from": 0,
            "reopen_count": 0,
            "skip_count": 0,
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        }
        items.append(item)
    return {
        "version": 1,
        "config": {"scope": "project", "project_name": "test"},
        "items": items,
    }


@pytest.fixture
def backlog_file(tmp_path):
    """Write a backlog with one code-review item and return its path."""
    data = make_backlog(
        {"id": _ITEM_ID, "title": "My Feature", "status": "code-review"},
        {"id": _ITEM_ID_2, "title": "Another Task", "status": "ready"},
    )
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(data, indent=2))
    return str(p)


def write_result_file(tmp_path, payload: dict, filename: str = "result.json") -> str:
    """Write a result JSON file and return its path string."""
    p = tmp_path / filename
    p.write_text(json.dumps(payload, indent=2))
    return str(p)


# ── ingest: happy paths ───────────────────────────────────────────────────────


class TestIngestHappyPath:
    def test_pass_verdict_advances_to_done(self, backlog_file, tmp_path):
        """Pass verdict on a code-review item should advance it to done."""
        result_path = write_result_file(tmp_path, {
            "item_id": _ITEM_ID,
            "verdict": "pass",
            "summary": "Looks good.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 0, result.output
        # Item should now be done
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID)
        assert item["status"] == "done"
        assert "done" in result.output.lower() or "advanced" in result.output.lower()

    def test_reject_verdict_moves_back_to_in_progress(self, backlog_file, tmp_path):
        """Reject verdict on a code-review item should move it back to in-progress."""
        result_path = write_result_file(tmp_path, {
            "item_id": _ITEM_ID,
            "verdict": "reject",
            "summary": "Found a critical issue.",
            "issues": [{"description": "Memory leak in handler", "severity": "blocker"}],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID)
        assert item["status"] == "in-progress"

    def test_markdown_fenced_json_parsed_correctly(self, backlog_file, tmp_path):
        """Markdown-fenced JSON should be parsed and ingested without error."""
        payload = {
            "item_id": _ITEM_ID,
            "verdict": "pass",
            "summary": "All good.",
            "issues": [],
        }
        fenced = f"```json\n{json.dumps(payload, indent=2)}\n```"
        p = tmp_path / "fenced_result.md"
        p.write_text(fenced)
        result = runner.invoke(app, ["ingest", str(p), "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID)
        assert item["status"] == "done"

    def test_json_flag_outputs_valid_json(self, backlog_file, tmp_path):
        """--json flag should produce valid JSON on stdout."""
        result_path = write_result_file(tmp_path, {
            "item_id": _ITEM_ID,
            "verdict": "pass",
            "summary": "All checks passed.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file, "--json"])
        assert result.exit_code == 0, result.output
        # Output should be parseable JSON
        parsed = json.loads(result.output)
        assert "item_id" in parsed
        assert parsed["item_id"] == _ITEM_ID


# ── ingest: failure modes ─────────────────────────────────────────────────────


class TestIngestFailureModes:
    def test_missing_file_exits_1(self, backlog_file):
        """Non-existent result file should produce exit code 1 with human-readable error."""
        result = runner.invoke(app, ["ingest", "/no/such/file.json", "--file", backlog_file])
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    def test_malformed_json_exits_1(self, backlog_file, tmp_path):
        """Non-JSON content in result file should exit 1."""
        p = tmp_path / "bad.json"
        p.write_text("this is not json at all ][[[")
        result = runner.invoke(app, ["ingest", str(p), "--file", backlog_file])
        assert result.exit_code == 1

    def test_missing_item_id_field_exits_1(self, backlog_file, tmp_path):
        """Result JSON missing item_id should exit 1."""
        result_path = write_result_file(tmp_path, {
            "verdict": "pass",
            "summary": "Missing item_id field.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 1

    def test_missing_verdict_and_status_fields_exits_1(self, backlog_file, tmp_path):
        """Result JSON missing both verdict and status should exit 1."""
        result_path = write_result_file(tmp_path, {
            "item_id": _ITEM_ID,
            "summary": "Neither verdict nor status.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 1

    def test_unknown_item_id_exits_1(self, backlog_file, tmp_path):
        """Result with an item_id not in the backlog should exit 1."""
        result_path = write_result_file(tmp_path, {
            "item_id": "notexist",
            "verdict": "pass",
            "summary": "Verdict for unknown item.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 1


# ── handoff: happy paths ──────────────────────────────────────────────────────


class TestHandoffHappyPath:
    def test_dry_run_prints_prompt_and_exits_0(self, backlog_file):
        """--dry-run should print the prompt to stdout and exit 0."""
        result = runner.invoke(
            app,
            ["handoff", "dev", "--file", backlog_file, "--item", "2", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        # Prompt should contain key sections
        assert "Work Brief" in result.output or "Review Brief" in result.output

    def test_review_dry_run_contains_item_title(self, backlog_file):
        """--review --item N --dry-run should emit a prompt containing the item title."""
        result = runner.invoke(
            app,
            [
                "handoff", "dev",
                "--file", backlog_file,
                "--item", "1",
                "--review",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "My Feature" in result.output
        assert "Review Brief" in result.output

    def test_claude_code_entrypoint_prints_prompt_exits_0(self, backlog_file, monkeypatch):
        """When CLAUDE_CODE_ENTRYPOINT is set, handoff should print prompt and exit 0."""
        # Mock shutil.which so the 'claude not found' guard does not fire in CI
        # environments where the claude CLI is not installed.
        monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/claude" if name == "claude" else None)
        result = runner.invoke(
            app,
            ["handoff", "dev", "--file", backlog_file, "--item", "1"],
            env={"CLAUDE_CODE_ENTRYPOINT": "1", "BACKLOG_FILE": backlog_file},
        )
        assert result.exit_code == 0, result.output
        # Should print the review prompt inline instead of spawning subprocess
        assert "My Feature" in result.output or "REVIEW PROMPT" in result.output

    def test_dry_run_does_not_spawn_subprocess(self, backlog_file, monkeypatch):
        """--dry-run must not invoke subprocess.run even if claude is on PATH."""
        called = []

        import subprocess as _subprocess
        original_run = _subprocess.run

        def mock_run(cmd, *args, **kwargs):
            if "claude" in (cmd[0] if isinstance(cmd, list) else cmd):
                called.append(cmd)
                raise AssertionError("subprocess.run called with claude in dry-run mode")
            return original_run(cmd, *args, **kwargs)

        monkeypatch.setattr(_subprocess, "run", mock_run)

        result = runner.invoke(
            app,
            ["handoff", "dev", "--file", backlog_file, "--item", "2", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert called == [], f"subprocess was invoked: {called}"


# ── handoff: failure modes ────────────────────────────────────────────────────


class TestHandoffFailureModes:
    def test_item_not_found_exits_1(self, backlog_file):
        """--item N where N is out of bounds should exit 1."""
        result = runner.invoke(
            app,
            ["handoff", "dev", "--file", backlog_file, "--item", "999"],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    def test_no_ready_items_no_item_flag_exits_1(self, tmp_path):
        """No --item flag and no ready items should exit 1."""
        data = make_backlog(
            {"id": _ITEM_ID, "title": "Blocked Item", "status": "blocked"},
        )
        p = tmp_path / "backlog.json"
        p.write_text(json.dumps(data, indent=2))
        result = runner.invoke(
            app,
            ["handoff", "dev", "--file", str(p)],
        )
        assert result.exit_code == 1


# ── edge cases ────────────────────────────────────────────────────────────────


class TestIngestEdgeCases:
    def test_reject_opens_thread_with_waiting_on_lead(self, backlog_file, tmp_path):
        """Reject ingest should create a thread with waiting_on=lead."""
        result_path = write_result_file(tmp_path, {
            "item_id": _ITEM_ID,
            "verdict": "reject",
            "summary": "Needs more work.",
            "issues": [{"description": "Off-by-one error", "severity": "blocker"}],
        })
        runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID)
        threads = item.get("threads", [])
        assert len(threads) > 0, "Expected at least one thread after reject"
        lead_thread = next((t for t in threads if t.get("waiting_on") == "lead"), None)
        assert lead_thread is not None, "Expected thread with waiting_on=lead"

    def test_reject_thread_body_contains_blocker_description(self, backlog_file, tmp_path):
        """Reject ingest thread body should contain the blocker issue description."""
        result_path = write_result_file(tmp_path, {
            "item_id": _ITEM_ID,
            "verdict": "reject",
            "summary": "Critical flaw found.",
            "issues": [{"description": "Memory leak in handler", "severity": "blocker"}],
        })
        runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID)
        threads = item.get("threads", [])
        bodies = " ".join(t.get("body", "") for t in threads)
        assert "Memory leak in handler" in bodies

    def test_pass_ingest_with_branch_name_shows_in_output(self, backlog_file, tmp_path):
        """Pass ingest with branch_name in JSON should show branch name in output."""
        result_path = write_result_file(tmp_path, {
            "item_id": _ITEM_ID,
            "verdict": "pass",
            "summary": "Done and merged.",
            "issues": [],
            "branch_name": "feat/my-branch",
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID)
        assert item["status"] == "done"
        # branch_name provided in review pass result should appear in CLI output
        assert "feat/my-branch" in result.output


# ── ingest: position-number (#N) lookup ──────────────────────────────────────


class TestIngestPositionLookup:
    """Tests for the #N position-based item_id disambiguation in backlog ingest."""

    def test_hash_position_resolves_to_correct_item(self, backlog_file, tmp_path):
        """item_id '#1' should resolve to the first item (position 1) in the backlog."""
        result_path = write_result_file(tmp_path, {
            "item_id": "#1",
            "verdict": "pass",
            "summary": "Resolved by position.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID)
        assert item["status"] == "done"

    def test_hash_position_second_item(self, backlog_file, tmp_path):
        """item_id '#2' should resolve to the second item in the backlog."""
        # First move item 2 to code-review so it can be ingested
        runner.invoke(app, ["move", "2", "code-review", "--file", backlog_file])
        result_path = write_result_file(tmp_path, {
            "item_id": "#2",
            "verdict": "pass",
            "summary": "Second item resolved by position.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        item = next(i for i in data["items"] if i["id"] == _ITEM_ID_2)
        assert item["status"] == "done"

    def test_bare_digit_string_treated_as_internal_id(self, backlog_file, tmp_path):
        """item_id '1' (no # prefix) must NOT be treated as a position lookup."""
        result_path = write_result_file(tmp_path, {
            "item_id": "1",
            "verdict": "pass",
            "summary": "Should fail — bare digit is not a position lookup.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        # '1' is not a valid internal ID in this backlog, so it should exit 1
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    def test_all_digit_internal_id_still_works(self, tmp_path):
        """An 8-digit all-numeric internal ID must be looked up as-is, not as a position."""
        digit_id = "12345678"
        data = make_backlog(
            {"id": digit_id, "title": "All-digit ID item", "status": "code-review"},
        )
        p = tmp_path / "backlog.json"
        p.write_text(json.dumps(data, indent=2))
        result_path = write_result_file(tmp_path, {
            "item_id": digit_id,
            "verdict": "pass",
            "summary": "Internal all-digit ID lookup.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", str(p)])
        assert result.exit_code == 0, result.output
        updated = json.loads(p.read_text())
        item = next(i for i in updated["items"] if i["id"] == digit_id)
        assert item["status"] == "done"

    def test_hash_position_out_of_range_exits_1(self, backlog_file, tmp_path):
        """item_id '#999' where position 999 does not exist should exit 1."""
        result_path = write_result_file(tmp_path, {
            "item_id": "#999",
            "verdict": "pass",
            "summary": "Out of range position.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file])
        assert result.exit_code == 1
        assert "out of range" in result.output.lower() or "error" in result.output.lower()

    def test_hash_position_json_flag_returns_resolved_id(self, backlog_file, tmp_path):
        """--json output after #N lookup should contain the resolved internal item_id."""
        result_path = write_result_file(tmp_path, {
            "item_id": "#1",
            "verdict": "pass",
            "summary": "Position-based JSON output.",
            "issues": [],
        })
        result = runner.invoke(app, ["ingest", result_path, "--file", backlog_file, "--json"])
        assert result.exit_code == 0, result.output
        parsed = json.loads(result.output)
        # The returned item_id should be the resolved internal ID, not "#1"
        assert parsed["item_id"] == _ITEM_ID
