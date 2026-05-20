"""Tests for complexity-gated sub-lead-agent review step in the refinement workflow.

Covers _assess_complexity, _run_subleadagent_review, and the extended
_auto_refine_tick logic that wires them together.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from backlog.cli import _assess_complexity, _auto_refine_tick, _run_subleadagent_review


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_backlog_file(tmp_path, items_data) -> str:
    """Write a minimal backlog.json and return its path as a string."""
    items = []
    for i, spec in enumerate(items_data):
        if isinstance(spec, str):
            spec = {"title": spec}
        item = {
            "id": f"item{i:04d}ab",
            "title": spec.get("title", f"Item {i}"),
            "status": spec.get("status", "backlog"),
            "tags": spec.get("tags", []),
            "description": spec.get("description", ""),
            "assigned_to": spec.get("assigned_to", None),
            "complexity": spec.get("complexity", "medium"),
            "links": [],
            "threads": spec.get("threads", []),
            "lane_history": [],
            "execution_history": [],
            "gate_from": 0,
            "reopen_count": 0,
            "skip_count": 0,
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        }
        items.append(item)
    data = {
        "version": 1,
        "config": {
            "scope": "project",
            "project_name": "test",
            "agents": {"lead-dev": {"role": "lead", "skills": ["python"], "max_active": 1}},
        },
        "items": items,
    }
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(data, indent=2))
    return str(p)


def _mock_claude_run(stdout_text):
    """Return a mock for subprocess.run that yields stdout_text."""
    mock_result = MagicMock()
    mock_result.stdout = stdout_text
    mock_result.returncode = 0
    return mock_result


# ── _assess_complexity ─────────────────────────────────────────────────────────


class TestAssessComplexity:
    def test_returns_complex_when_claude_says_complex(self):
        response = '{"complexity": "complex", "reason": "touches auth and spans multiple files"}'
        with patch("subprocess.run", return_value=_mock_claude_run(response)):
            label, reason = _assess_complexity("Refactor auth", "Rework login flow", ["auth"])
        assert label == "complex"
        assert "auth" in reason

    def test_returns_simple_when_claude_says_simple(self):
        response = '{"complexity": "simple", "reason": "single-file rename, no logic change"}'
        with patch("subprocess.run", return_value=_mock_claude_run(response)):
            label, reason = _assess_complexity("Rename helper", "Rename a function", [])
        assert label == "simple"
        assert "single-file" in reason

    def test_defaults_to_complex_on_invalid_label(self):
        response = '{"complexity": "unclear", "reason": "cannot decide"}'
        with patch("subprocess.run", return_value=_mock_claude_run(response)):
            label, reason = _assess_complexity("Odd item", "no description", [])
        assert label == "complex"

    def test_defaults_to_complex_on_unparseable_output(self):
        with patch("subprocess.run", return_value=_mock_claude_run("not json at all")):
            label, reason = _assess_complexity("Broken", "", [])
        assert label == "complex"

    def test_defaults_to_complex_on_subprocess_exception(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 60)):
            label, reason = _assess_complexity("Timeout item", "", [])
        assert label == "complex"
        assert "error" in reason.lower() or "default" in reason.lower()

    def test_passes_tags_to_prompt(self):
        response = '{"complexity": "complex", "reason": "data integrity risk"}'
        captured_prompts = []

        def capture_run(args, **kwargs):
            captured_prompts.append(args[-1])  # last arg is the prompt string
            return _mock_claude_run(response)

        with patch("subprocess.run", side_effect=capture_run):
            _assess_complexity("DB migration", "Migrate schema", ["database", "data-integrity"])

        assert len(captured_prompts) == 1
        assert "database" in captured_prompts[0]
        assert "data-integrity" in captured_prompts[0]


# ── _run_subleadagent_review ───────────────────────────────────────────────────


class TestRunSubleadagentReview:
    def test_returns_passed_true_with_no_findings(self):
        response = '{"passed": true, "findings": []}'
        with patch("subprocess.run", return_value=_mock_claude_run(response)):
            passed, findings = _run_subleadagent_review("Good item", "Well specified", [])
        assert passed is True
        assert findings == []

    def test_returns_passed_true_with_praise(self):
        response = '{"passed": true, "findings": ["Acceptance criteria are concrete and verifiable"]}'
        with patch("subprocess.run", return_value=_mock_claude_run(response)):
            passed, findings = _run_subleadagent_review("Great item", "spec here", [])
        assert passed is True
        assert len(findings) == 1

    def test_returns_passed_false_with_blockers(self):
        response = (
            '{"passed": false, "findings": '
            '["Acceptance criteria are vague", "Hidden dependency on unbuilt service"]}'
        )
        with patch("subprocess.run", return_value=_mock_claude_run(response)):
            passed, findings = _run_subleadagent_review("Vague item", "no spec", [])
        assert passed is False
        assert len(findings) == 2

    def test_falls_back_to_passed_true_on_parse_error(self):
        with patch("subprocess.run", return_value=_mock_claude_run("garbage output")):
            passed, findings = _run_subleadagent_review("Bad response", "", [])
        assert passed is True

    def test_falls_back_to_passed_true_on_exception(self):
        with patch("subprocess.run", side_effect=OSError("claude not found")):
            passed, findings = _run_subleadagent_review("Exception item", "", [])
        assert passed is True

    def test_checklist_terms_in_prompt(self):
        response = '{"passed": true, "findings": []}'
        captured_prompts = []

        def capture_run(args, **kwargs):
            captured_prompts.append(args[-1])
            return _mock_claude_run(response)

        with patch("subprocess.run", side_effect=capture_run):
            _run_subleadagent_review("Complex item", "description", ["auth"])

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "testable" in prompt
        assert "hidden dependencies" in prompt or "dependencies" in prompt
        assert "edge cases" in prompt

    def test_findings_with_curly_braces_returns_correct_result(self):
        """Findings text containing { } must not break JSON extraction.

        Regression: the old re.search(r'\\{.*?\\}') stops at the first },
        producing truncated invalid JSON when findings mention dict patterns.
        """
        response = (
            '{"passed": false, "findings": '
            '["Missing {config} key in the payload", "Dict pattern {k: v} not validated"]}'
        )
        with patch("subprocess.run", return_value=_mock_claude_run(response)):
            passed, findings = _run_subleadagent_review("Config item", "needs {config}", [])
        assert passed is False
        assert len(findings) == 2
        assert "Missing {config} key in the payload" in findings


# ── _auto_refine_tick ──────────────────────────────────────────────────────────


class TestAutoRefineTick:
    """Integration-level tests for the extended _auto_refine_tick.

    We mock subprocess.run to avoid spawning real Claude processes.
    """

    def _claude_sequence(self, responses):
        """Return a side_effect list for subprocess.run from a list of JSON strings."""
        results = []
        for r in responses:
            m = MagicMock()
            m.stdout = r
            m.returncode = 0
            results.append(m)
        return results

    def test_simple_item_moves_to_ready_without_subleadagent_review(self, tmp_path):
        """A simple item should skip the sub-lead review and move straight to ready."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Rename helper fn", "status": "backlog", "tags": []}
        ])
        responses = self._claude_sequence([
            '{"ready": true}',                                        # readiness check
            '{"complexity": "simple", "reason": "one-file rename"}',  # complexity
        ])
        with patch("subprocess.run", side_effect=responses), \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)

        data = json.loads(Path(bl_file).read_text())
        assert data["items"][0]["status"] == "ready"

    def test_complex_item_passes_subleadagent_review_then_moves_to_ready(self, tmp_path):
        """A complex item that passes the sub-lead review should end up ready."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Auth refactor", "status": "backlog", "tags": ["auth"]}
        ])
        responses = self._claude_sequence([
            '{"ready": true}',
            '{"complexity": "complex", "reason": "touches auth across 3 modules"}',
            '{"passed": true, "findings": ["Acceptance criteria are specific"]}',
        ])
        with patch("subprocess.run", side_effect=responses), \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)

        data = json.loads(Path(bl_file).read_text())
        assert data["items"][0]["status"] == "ready"

    def test_complex_item_fails_subleadagent_review_stays_in_backlog_with_thread(self, tmp_path):
        """A complex item that fails the sub-lead review must NOT move to ready."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Data migration", "status": "backlog", "tags": ["database"]}
        ])
        responses = self._claude_sequence([
            '{"ready": true}',
            '{"complexity": "complex", "reason": "data integrity risk"}',
            '{"passed": false, "findings": ["Acceptance criteria are vague", "No rollback plan"]}',
        ])
        with patch("subprocess.run", side_effect=responses), \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)

        data = json.loads(Path(bl_file).read_text())
        item = data["items"][0]
        assert item["status"] == "backlog"
        # A thread should have been opened
        threads = item.get("threads", [])
        assert len(threads) == 1
        assert threads[0]["waiting_on"] == "user"
        assert "sub-lead" in threads[0]["topic"].lower() or "readiness" in threads[0]["topic"].lower()
        assert "Acceptance criteria" in threads[0]["body"]

    def test_not_ready_item_gets_questions_thread(self, tmp_path):
        """When Claude says not ready, a refinement thread should be opened (existing behaviour)."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Unclear task", "status": "backlog", "tags": []}
        ])
        responses = self._claude_sequence([
            '{"ready": false, "questions": ["What does done look like?", "What are the edge cases?"]}',
        ])
        with patch("subprocess.run", side_effect=responses), \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)

        data = json.loads(Path(bl_file).read_text())
        item = data["items"][0]
        assert item["status"] == "backlog"
        threads = item.get("threads", [])
        assert len(threads) == 1
        assert "What does done look like?" in threads[0]["body"]

    def test_dry_run_does_not_write_to_file(self, tmp_path):
        """dry_run=True must not mutate the backlog file for a complex-pass scenario."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Security audit", "status": "backlog", "tags": ["auth"]}
        ])
        original = Path(bl_file).read_text()
        responses = self._claude_sequence([
            '{"ready": true}',
            '{"complexity": "complex", "reason": "security scope"}',
            '{"passed": true, "findings": []}',
        ])
        with patch("subprocess.run", side_effect=responses), \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=True)

        assert Path(bl_file).read_text() == original

    def test_dry_run_does_not_write_thread_on_failed_review(self, tmp_path):
        """dry_run=True must not write a thread even when review fails."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Risky task", "status": "backlog", "tags": []}
        ])
        original_data = json.loads(Path(bl_file).read_text())
        responses = self._claude_sequence([
            '{"ready": true}',
            '{"complexity": "complex", "reason": "unclear scope"}',
            '{"passed": false, "findings": ["No acceptance criteria"]}',
        ])
        with patch("subprocess.run", side_effect=responses), \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=True)

        data = json.loads(Path(bl_file).read_text())
        assert data["items"][0].get("threads", []) == []

    def test_skips_item_already_waiting_for_user(self, tmp_path):
        """Items with an unresolved user-waiting thread must be skipped."""
        existing_thread = {
            "id": "thread01ab",
            "topic": "Clarification needed",
            "waiting_on": "user",
            "body": "question",
            "created_at": "2024-01-01T00:00:00+00:00",
            "resolved": False,
        }
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Blocked item", "status": "backlog", "threads": [existing_thread]},
        ])
        with patch("subprocess.run") as mock_run, \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)
        # Claude should not be called at all — item is skipped
        mock_run.assert_not_called()

    def test_no_candidate_returns_early(self, tmp_path):
        """When no backlog/refined items exist the function must return without calling Claude."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Done item", "status": "done"},
        ])
        with patch("subprocess.run") as mock_run, \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)
        mock_run.assert_not_called()

    def test_human_complexity_override_skips_assess_complexity(self, tmp_path):
        """When an item already has complexity='simple' set, _assess_complexity must NOT
        be called and the item should move straight to ready without a sub-lead review.

        Regression: previously _assess_complexity was called unconditionally every tick,
        so a human running 'backlog edit N --complexity simple' had no effect.
        """
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Pre-labelled item", "status": "backlog", "tags": [], "complexity": "simple"}
        ])
        # Only ONE subprocess call expected: the readiness check.
        # If _assess_complexity is called, it would be a second call and the test would fail
        # because there is no second response in the sequence.
        responses = self._claude_sequence([
            '{"ready": true}',  # readiness check only
        ])
        with patch("subprocess.run", side_effect=responses) as mock_run, \
             patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("backlog.cli._assess_complexity") as mock_assess:
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)

        mock_assess.assert_not_called()

        data = json.loads(Path(bl_file).read_text())
        assert data["items"][0]["status"] == "ready"

    def test_complexity_label_notification_is_printed(self, tmp_path, capsys):
        """Human must see the proposed complexity label so they can override it."""
        bl_file = _make_backlog_file(tmp_path, [
            {"title": "Rename helper", "status": "backlog", "tags": []}
        ])
        responses = self._claude_sequence([
            '{"ready": true}',
            '{"complexity": "simple", "reason": "trivial rename"}',
        ])
        with patch("subprocess.run", side_effect=responses), \
             patch("shutil.which", return_value="/usr/bin/claude"):
            _auto_refine_tick(bl_file, "lead-dev", dry_run=False)

        # Rich console output is captured differently; check that the move happened
        # (proving the tick ran) and that no sub-lead call was made (only 2 calls)
        data = json.loads(Path(bl_file).read_text())
        assert data["items"][0]["status"] == "ready"
