"""Tests for backlog ref and unref CLI commands (external_refs field, #113)."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()


def make_backlog(*titles) -> dict:
    """Create a minimal in-memory backlog dict with N items."""
    items = []
    for i, title in enumerate(titles):
        items.append({
            "id": f"item{i:04d}ab",
            "title": title,
            "status": "backlog",
            "priority": "medium",
            "priority_weight": 5,
            "complexity": "medium",
            "category": None,
            "tags": [],
            "description": "",
            "assigned_to": None,
            "links": [],
            "threads": [],
            "lane_history": [],
            "execution_history": [],
            "gate_from": 0,
            "reopen_count": 0,
            "skip_count": 0,
            "created_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:00:00+00:00",
        })
    return {"version": 1, "config": {"scope": "project", "project_name": "test"}, "items": items}


@pytest.fixture
def backlog_file(tmp_path):
    """Write a fresh backlog.json with two items and return its path."""
    data = make_backlog("Item Alpha", "Item Beta")
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(data, indent=2))
    return str(p)


# ── ref: happy path ───────────────────────────────────────────────────────────

class TestRefHappyPath:
    def test_ref_add_by_position(self, backlog_file):
        result = runner.invoke(app, [
            "ref", "1",
            "--file", backlog_file,
            "--system", "jira",
            "--id", "PROJ-123",
        ])
        assert result.exit_code == 0, result.output
        assert "PROJ-123" in result.output

    def test_ref_add_with_url(self, backlog_file):
        result = runner.invoke(app, [
            "ref", "1",
            "--file", backlog_file,
            "--system", "jira",
            "--id", "PROJ-123",
            "--url", "https://co.atlassian.net/browse/PROJ-123",
        ])
        assert result.exit_code == 0, result.output
        assert "PROJ-123" in result.output

    def test_ref_stored_in_file(self, backlog_file):
        runner.invoke(app, [
            "ref", "1",
            "--file", backlog_file,
            "--system", "jira",
            "--id", "PROJ-123",
            "--url", "https://co.atlassian.net/browse/PROJ-123",
        ])
        data = json.loads(Path(backlog_file).read_text())
        item = data["items"][0]
        refs = item.get("external_refs", [])
        assert len(refs) == 1
        ref = refs[0]
        assert ref["system"] == "jira"
        assert ref["id"] == "PROJ-123"
        assert ref["url"] == "https://co.atlassian.net/browse/PROJ-123"

    def test_ref_without_url_stored_in_file(self, backlog_file):
        runner.invoke(app, [
            "ref", "1",
            "--file", backlog_file,
            "--system", "github",
            "--id", "GH-42",
        ])
        data = json.loads(Path(backlog_file).read_text())
        item = data["items"][0]
        refs = item.get("external_refs", [])
        assert len(refs) == 1
        assert refs[0]["system"] == "github"
        assert refs[0]["id"] == "GH-42"
        assert "url" not in refs[0] or refs[0].get("url") is None

    def test_ref_add_by_item_id(self, backlog_file):
        data = json.loads(Path(backlog_file).read_text())
        item_id = data["items"][0]["id"]
        result = runner.invoke(app, [
            "ref", item_id,
            "--file", backlog_file,
            "--system", "linear",
            "--id", "LIN-99",
        ])
        assert result.exit_code == 0, result.output

    def test_duplicate_refs_allowed(self, backlog_file):
        """Duplicates are allowed — same ref can be added twice."""
        args = [
            "ref", "1",
            "--file", backlog_file,
            "--system", "jira",
            "--id", "PROJ-123",
        ]
        runner.invoke(app, args)
        runner.invoke(app, args)
        data = json.loads(Path(backlog_file).read_text())
        refs = data["items"][0].get("external_refs", [])
        assert len(refs) == 2, f"expected 2 (duplicates allowed), got {len(refs)}"


# ── ref --list ────────────────────────────────────────────────────────────────

class TestRefList:
    def test_list_shows_refs(self, backlog_file):
        runner.invoke(app, [
            "ref", "1",
            "--file", backlog_file,
            "--system", "jira",
            "--id", "PROJ-123",
            "--url", "https://example.com",
        ])
        result = runner.invoke(app, [
            "ref", "--list", "1",
            "--file", backlog_file,
        ])
        assert result.exit_code == 0, result.output
        assert "jira" in result.output
        assert "PROJ-123" in result.output

    def test_list_no_refs(self, backlog_file):
        result = runner.invoke(app, [
            "ref", "--list", "1",
            "--file", backlog_file,
        ])
        assert result.exit_code == 0, result.output
        assert "No" in result.output or result.output.strip() != ""

    def test_list_shows_url_when_present(self, backlog_file):
        runner.invoke(app, [
            "ref", "1",
            "--file", backlog_file,
            "--system", "jira",
            "--id", "PROJ-456",
            "--url", "https://jira.example.com/browse/PROJ-456",
        ])
        result = runner.invoke(app, [
            "ref", "--list", "1",
            "--file", backlog_file,
        ])
        assert result.exit_code == 0, result.output
        assert "https://jira.example.com" in result.output

    def test_list_multiple_refs(self, backlog_file):
        runner.invoke(app, [
            "ref", "1", "--file", backlog_file,
            "--system", "jira", "--id", "PROJ-1",
        ])
        runner.invoke(app, [
            "ref", "1", "--file", backlog_file,
            "--system", "github", "--id", "GH-99",
        ])
        result = runner.invoke(app, [
            "ref", "--list", "1", "--file", backlog_file,
        ])
        assert result.exit_code == 0, result.output
        assert "jira" in result.output
        assert "PROJ-1" in result.output
        assert "github" in result.output
        assert "GH-99" in result.output


# ── unref: happy path ─────────────────────────────────────────────────────────

class TestUnrefHappyPath:
    def test_unref_removes_ref(self, backlog_file):
        runner.invoke(app, [
            "ref", "1", "--file", backlog_file,
            "--system", "jira", "--id", "PROJ-123",
        ])
        result = runner.invoke(app, [
            "unref", "1", "--file", backlog_file,
            "--target", "PROJ-123",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        refs = data["items"][0].get("external_refs", [])
        assert refs == []

    def test_unref_removes_all_matching_ids(self, backlog_file):
        """unref --target removes ALL copies when duplicates exist."""
        args = [
            "ref", "1", "--file", backlog_file,
            "--system", "jira", "--id", "PROJ-123",
        ]
        runner.invoke(app, args)
        runner.invoke(app, args)  # add duplicate
        data = json.loads(Path(backlog_file).read_text())
        assert len(data["items"][0].get("external_refs", [])) == 2
        result = runner.invoke(app, [
            "unref", "1", "--file", backlog_file,
            "--target", "PROJ-123",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        refs = data["items"][0].get("external_refs", [])
        assert refs == []

    def test_unref_only_removes_matching_ref(self, backlog_file):
        """Other refs with different id are untouched."""
        runner.invoke(app, [
            "ref", "1", "--file", backlog_file,
            "--system", "jira", "--id", "PROJ-123",
        ])
        runner.invoke(app, [
            "ref", "1", "--file", backlog_file,
            "--system", "github", "--id", "GH-42",
        ])
        runner.invoke(app, [
            "unref", "1", "--file", backlog_file,
            "--target", "PROJ-123",
        ])
        data = json.loads(Path(backlog_file).read_text())
        refs = data["items"][0].get("external_refs", [])
        assert len(refs) == 1
        assert refs[0]["id"] == "GH-42"

    def test_unref_prints_success_message(self, backlog_file):
        runner.invoke(app, [
            "ref", "1", "--file", backlog_file,
            "--system", "jira", "--id", "PROJ-123",
        ])
        result = runner.invoke(app, [
            "unref", "1", "--file", backlog_file,
            "--target", "PROJ-123",
        ])
        assert result.exit_code == 0, result.output
        assert "PROJ-123" in result.output


# ── unref: error cases ────────────────────────────────────────────────────────

class TestUnrefErrors:
    def test_unref_nonexistent_target_exits_1(self, backlog_file):
        """unref on non-existent id must exit 1 with an error — NOT silent no-op."""
        result = runner.invoke(app, [
            "unref", "1", "--file", backlog_file,
            "--target", "NONEXISTENT-999",
        ])
        assert result.exit_code == 1
        assert "No ref" in result.output or "not found" in result.output.lower()

    def test_unref_unknown_item_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "unref", "99", "--file", backlog_file,
            "--target", "PROJ-123",
        ])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ── backward compatibility ────────────────────────────────────────────────────

class TestBackwardCompat:
    def test_item_without_external_refs_loads_cleanly(self, backlog_file):
        """Existing items with no external_refs field work as-is."""
        data = json.loads(Path(backlog_file).read_text())
        # Ensure no external_refs key present
        assert "external_refs" not in data["items"][0]
        # ref --list should work fine and show "No refs"
        result = runner.invoke(app, [
            "ref", "--list", "1",
            "--file", backlog_file,
        ])
        assert result.exit_code == 0, result.output

    def test_backlog_list_works_without_external_refs(self, backlog_file):
        """Standard list command still works on a backlog without external_refs."""
        result = runner.invoke(app, ["list", "--file", backlog_file])
        assert result.exit_code == 0, result.output


# ── BACKLOG_FILE env var ──────────────────────────────────────────────────────

class TestEnvVar:
    def test_respects_backlog_file_env_for_ref(self, backlog_file, monkeypatch):
        monkeypatch.setenv("BACKLOG_FILE", backlog_file)
        result = runner.invoke(app, [
            "ref", "1",
            "--system", "jira",
            "--id", "PROJ-ENV-1",
        ])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(backlog_file).read_text())
        refs = data["items"][0].get("external_refs", [])
        assert any(r["id"] == "PROJ-ENV-1" for r in refs)
