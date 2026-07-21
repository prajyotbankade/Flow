"""Tests for proactive drift hint on `backlog commit` (backlog item #119).

After `backlog commit` succeeds, the command must print the same drift warning
that `backlog doctor` would show when local trunk is ahead of origin by
backlog-only commits.  When there is no drift (or no origin remote), the hint
must be absent.  The commit itself must always succeed (exit 0) regardless.

Test setup mirrors test_doctor_trunk_ahead.py: a local bare repo acts as a fake
'origin' so origin/<trunk> refs exist without any network access.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Shared data / helpers
# ---------------------------------------------------------------------------

_MINIMAL_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}

_UPDATED_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [
        {
            "id": "aabb1122",
            "title": "New item",
            "status": "backlog",
            "tags": [],
            "description": "",
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
    ],
}

_UPDATED2_BACKLOG = {
    "version": 2,
    "config": {"scope": "project", "project_name": "test"},
    "items": [
        {
            "id": "ccdd3344",
            "title": "Second item",
            "status": "backlog",
            "tags": [],
            "description": "",
            "links": [],
            "threads": [],
            "lane_history": [],
            "execution_history": [],
            "gate_from": 0,
            "reopen_count": 0,
            "skip_count": 0,
            "created_at": "2024-01-02T00:00:00+00:00",
            "updated_at": "2024-01-02T00:00:00+00:00",
        }
    ],
}


def _git(*args, cwd):
    """Run git command, assert success, return stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"git {' '.join(args)} failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout.strip()


def _init_repo_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare 'origin' and a local clone with an initial backlog.json commit.

    The initial backlog.json is pushed to origin so origin/main exists and is
    in sync with local main.  Returns (local_repo_path, bare_origin_path).
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "-b", "main"],
        cwd=origin,
        check=True,
        capture_output=True,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)

    backlog_file = repo / "backlog.json"
    backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
    _git("add", "backlog.json", cwd=repo)
    _git("commit", "-m", "initial backlog", cwd=repo)
    _git("push", "-u", "origin", "main", cwd=repo)

    return repo, origin


# ---------------------------------------------------------------------------
# Drift hint present: local trunk ends up ahead of origin by backlog-only commit
# ---------------------------------------------------------------------------

class TestCommitDriftHintPresent:
    def test_hint_printed_when_drift_created(self, tmp_path):
        """backlog commit prints the drift hint when local trunk is ahead of origin
        by the backlog-only commit that was just made."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # Modify backlog.json — NOT committed yet; backlog commit will do it
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))

        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}\nOutput:\n{result.output}"
        assert "Committed" in result.output, f"Expected 'Committed' in output:\n{result.output}"
        assert "backlog-only" in result.output, (
            f"Expected drift hint with 'backlog-only' in output:\n{result.output}"
        )
        assert "origin/main" in result.output, (
            f"Expected 'origin/main' in hint:\n{result.output}"
        )

    def test_hint_contains_remediation_guidance(self, tmp_path):
        """The drift hint should mention how to avoid the problem."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))

        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        # Should contain either "Diff PRs" or "land backlog" — same wording as doctor
        assert "Diff PRs" in result.output or "land backlog" in result.output, (
            f"Expected remediation guidance in output:\n{result.output}"
        )

    def test_exit_code_zero_even_with_hint(self, tmp_path):
        """The drift hint must never change the exit code — commit stays exit 0."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))

        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, (
            f"Drift hint must not change exit code — got {result.exit_code}\nOutput:\n{result.output}"
        )

    def test_backlog_actually_committed_when_hint_shown(self, tmp_path):
        """The backlog.json commit is real — HEAD advances — regardless of hint."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        before_hash = _git("rev-parse", "HEAD", cwd=repo)

        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))

        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        after_hash = _git("rev-parse", "HEAD", cwd=repo)
        assert after_hash != before_hash, "Expected HEAD to advance after commit"

        # Committed file must be exactly backlog.json
        names = _git("show", "--name-only", "--format=", "HEAD", cwd=repo).split()
        assert names == ["backlog.json"], f"Unexpected files in commit: {names}"


# ---------------------------------------------------------------------------
# No drift hint: local trunk in sync with origin
# ---------------------------------------------------------------------------

class TestCommitNoHintWhenInSync:
    def test_no_hint_when_pushed_before_commit(self, tmp_path):
        """When local trunk matches origin trunk, backlog commit prints no drift hint."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # Commit a change AND push it — so after commit local is still in sync
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "pre-push update", cwd=repo)
        _git("push", "origin", "main", cwd=repo)

        # Now make another change for `backlog commit` to pick up
        backlog_file.write_text(json.dumps(_UPDATED2_BACKLOG, indent=2))
        # Push immediately after commit to put us back in sync is NOT done here
        # The commit itself will put local ahead by 1 — but we want to test the
        # in-sync case, so instead test with a freshly pushed state.
        # Reset to pushed state and test a noop commit (nothing to commit).
        # Actually the right test is: verify that when local trunk IS in sync
        # before the commit and the commit is a noop, no hint appears.
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))  # restore to pushed state

        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "backlog-only" not in result.output, (
            f"Unexpected drift hint when in sync:\n{result.output}"
        )

    def test_no_hint_on_noop_commit(self, tmp_path):
        """When backlog.json is already up to date, no commit happens and no hint."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # No changes — noop path
        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        assert "nothing to commit" in result.output
        assert "backlog-only" not in result.output, (
            f"Unexpected drift hint on noop:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# No drift hint: no origin remote
# ---------------------------------------------------------------------------

class TestCommitNoHintWithoutOrigin:
    def test_no_hint_no_crash_without_origin(self, tmp_path):
        """When there is no origin remote, commit succeeds and prints no drift hint."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@example.com", cwd=repo)
        _git("config", "user.name", "Test User", cwd=repo)

        backlog_file = repo / "backlog.json"
        backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "initial", cwd=repo)

        # Modify so there is something to commit
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))

        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0, f"Expected exit 0, got {result.exit_code}\nOutput:\n{result.output}"
        assert "backlog-only" not in result.output, (
            f"Unexpected drift hint when no origin:\n{result.output}"
        )
        # Must not leak a traceback
        assert "Traceback" not in result.output


# ---------------------------------------------------------------------------
# Commit still succeeds: hint is additive, never blocking
# ---------------------------------------------------------------------------

class TestCommitHintIsNonFatal:
    def test_commit_output_has_committed_line_before_hint(self, tmp_path):
        """The 'Committed <hash> <msg>' line appears before any drift hint."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))

        result = runner.invoke(
            app,
            ["commit", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 0
        output = result.output
        committed_pos = output.find("Committed")
        hint_pos = output.find("hint:")
        assert committed_pos != -1, f"'Committed' line missing:\n{output}"
        assert hint_pos != -1, f"'hint:' line missing:\n{output}"
        assert committed_pos < hint_pos, (
            "Expected 'Committed' line to appear before the drift hint"
        )
