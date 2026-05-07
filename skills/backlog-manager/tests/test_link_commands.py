"""Tests for backlog link and unlink CLI commands."""

import json
import os
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()

VALID_TYPES = ["blocks", "discovered-during", "follow-up", "related"]


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


# ── link: happy path ──────────────────────────────────────────────────────────

class TestLinkHappyPath:
    def test_link_by_position(self, backlog_file):
        result = runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
            "--reason", "Alpha must finish before Beta",
        ])
        assert result.exit_code == 0, result.output
        assert "Linked #1" in result.output
        assert "#2" in result.output
        assert "blocks" in result.output
        assert "Alpha must finish before Beta" in result.output

    def test_link_by_item_id(self, backlog_file):
        data = json.loads(Path(backlog_file).read_text())
        src_id = data["items"][0]["id"]
        tgt_id = data["items"][1]["id"]
        result = runner.invoke(app, [
            "link", src_id,
            "--file", backlog_file,
            "--type", "related",
            "--target", tgt_id,
            "--reason", "They share the same domain",
        ])
        assert result.exit_code == 0, result.output
        assert "Linked" in result.output

    def test_link_stored_in_file(self, backlog_file):
        result = runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "follow-up",
            "--target", "2",
            "--reason", "Follow up from 1",
        ])
        assert result.exit_code == 0
        data = json.loads(Path(backlog_file).read_text())
        src_item = data["items"][0]
        assert len(src_item["links"]) == 1
        link = src_item["links"][0]
        assert link["type"] == "follow-up"
        assert link["reason"] == "Follow up from 1"
        assert link["item_id"] == data["items"][1]["id"]

    def test_all_valid_types_accepted(self, backlog_file):
        # Recreate a fresh file for each type test
        for link_type in VALID_TYPES:
            data = make_backlog("A", "B")
            Path(backlog_file).write_text(json.dumps(data, indent=2))
            result = runner.invoke(app, [
                "link", "1",
                "--file", backlog_file,
                "--type", link_type,
                "--target", "2",
                "--reason", "some reason",
            ])
            assert result.exit_code == 0, f"type={link_type} failed: {result.output}"


# ── link: --list subcommand ───────────────────────────────────────────────────

class TestLinkList:
    def test_list_shows_links(self, backlog_file):
        # First add a link
        runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
            "--reason", "prereq",
        ])
        result = runner.invoke(app, [
            "link", "--list", "1",
            "--file", backlog_file,
        ])
        assert result.exit_code == 0, result.output
        assert "blocks" in result.output
        assert "prereq" in result.output

    def test_list_no_links(self, backlog_file):
        result = runner.invoke(app, [
            "link", "--list", "1",
            "--file", backlog_file,
        ])
        assert result.exit_code == 0
        # Should indicate no links rather than crashing
        assert "No links" in result.output or result.output.strip() != ""

    def test_list_by_item_id(self, backlog_file):
        data = json.loads(Path(backlog_file).read_text())
        src_id = data["items"][0]["id"]
        runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "related",
            "--target", "2",
            "--reason", "related items",
        ])
        result = runner.invoke(app, [
            "link", "--list", src_id,
            "--file", backlog_file,
        ])
        assert result.exit_code == 0, result.output
        assert "related" in result.output


# ── link: error cases ─────────────────────────────────────────────────────────

class TestLinkErrors:
    def test_missing_reason_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
        ])
        assert result.exit_code == 1
        assert "--reason is required" in result.output

    def test_unknown_source_id_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "link", "99",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
            "--reason", "r",
        ])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_unknown_target_id_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "99",
            "--reason", "r",
        ])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_invalid_type_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "wontfix",
            "--target", "2",
            "--reason", "r",
        ])
        assert result.exit_code == 1
        # Should list valid types
        assert "blocks" in result.output or "valid" in result.output.lower()

    def test_self_link_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "1",
            "--reason", "r",
        ])
        assert result.exit_code == 1
        assert "itself" in result.output.lower()

    def test_duplicate_link_does_not_duplicate(self, backlog_file):
        args = [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
            "--reason", "r",
        ]
        runner.invoke(app, args)
        runner.invoke(app, args)
        data = json.loads(Path(backlog_file).read_text())
        src_item = data["items"][0]
        # Should not have duplicated the link
        assert len(src_item["links"]) == 1


# ── unlink: happy path ────────────────────────────────────────────────────────

class TestUnlinkHappyPath:
    def test_unlink_by_position(self, backlog_file):
        runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
            "--reason", "r",
        ])
        result = runner.invoke(app, [
            "unlink", "1",
            "--file", backlog_file,
            "--target", "2",
        ])
        assert result.exit_code == 0, result.output
        assert "Unlinked" in result.output
        assert "#1" in result.output
        assert "#2" in result.output

    def test_unlink_removes_link_from_file(self, backlog_file):
        runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
            "--reason", "r",
        ])
        runner.invoke(app, [
            "unlink", "1",
            "--file", backlog_file,
            "--target", "2",
        ])
        data = json.loads(Path(backlog_file).read_text())
        src_item = data["items"][0]
        assert src_item["links"] == []

    def test_unlink_by_item_id(self, backlog_file):
        data = json.loads(Path(backlog_file).read_text())
        src_id = data["items"][0]["id"]
        tgt_id = data["items"][1]["id"]
        runner.invoke(app, [
            "link", src_id,
            "--file", backlog_file,
            "--type", "related",
            "--target", tgt_id,
            "--reason", "r",
        ])
        result = runner.invoke(app, [
            "unlink", src_id,
            "--file", backlog_file,
            "--target", tgt_id,
        ])
        assert result.exit_code == 0, result.output
        assert "Unlinked" in result.output


# ── unlink: error cases ───────────────────────────────────────────────────────

class TestUnlinkErrors:
    def test_unlink_nonexistent_link_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "unlink", "1",
            "--file", backlog_file,
            "--target", "2",
        ])
        assert result.exit_code == 1
        assert "No link" in result.output or "not found" in result.output.lower()

    def test_unlink_unknown_source_exits_1(self, backlog_file):
        result = runner.invoke(app, [
            "unlink", "99",
            "--file", backlog_file,
            "--target", "2",
        ])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_unlink_unknown_target_exits_1(self, backlog_file):
        runner.invoke(app, [
            "link", "1",
            "--file", backlog_file,
            "--type", "blocks",
            "--target", "2",
            "--reason", "r",
        ])
        result = runner.invoke(app, [
            "unlink", "1",
            "--file", backlog_file,
            "--target", "99",
        ])
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ── BACKLOG_FILE env var ──────────────────────────────────────────────────────

class TestEnvVar:
    def test_respects_backlog_file_env(self, backlog_file, monkeypatch):
        monkeypatch.setenv("BACKLOG_FILE", backlog_file)
        result = runner.invoke(app, [
            "link", "1",
            "--type", "blocks",
            "--target", "2",
            "--reason", "env test",
        ])
        assert result.exit_code == 0, result.output
