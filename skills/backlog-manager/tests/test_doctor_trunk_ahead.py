"""Tests for backlog doctor trunk-ahead-of-origin check (backlog item #118).

Each test sets up a local bare repo as a fake 'origin' so origin/<trunk> refs
exist without hitting the network.  No fetch is ever issued.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app, _check_trunk_ahead_of_origin

runner = CliRunner()

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_MINIMAL_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}

_UPDATED_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [{"id": "aabb1122", "title": "New item", "status": "backlog",
               "tags": [], "description": "", "links": [], "threads": [],
               "lane_history": [], "execution_history": [],
               "gate_from": 0, "reopen_count": 0, "skip_count": 0,
               "created_at": "2024-01-01T00:00:00+00:00",
               "updated_at": "2024-01-01T00:00:00+00:00"}],
}


def _git(*args, cwd):
    """Run git command in cwd, assert success, return stdout."""
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
    """Create a bare 'origin' repo and a local clone with a committed backlog.json.

    Returns (local_repo_path, bare_origin_path).
    """
    origin = tmp_path / "origin.git"
    origin.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main"], cwd=origin, check=True, capture_output=True)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    _git("remote", "add", "origin", str(origin), cwd=repo)

    # Initial commit with backlog.json
    backlog_file = repo / "backlog.json"
    backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
    _git("add", "backlog.json", cwd=repo)
    _git("commit", "-m", "initial backlog", cwd=repo)

    # Push so origin/main exists
    _git("push", "-u", "origin", "main", cwd=repo)

    return repo, origin


# ---------------------------------------------------------------------------
# Test: backlog-only commits ahead → warning
# ---------------------------------------------------------------------------

class TestBacklogOnlyAhead:
    def test_warns_with_correct_commit_count(self, tmp_path):
        """doctor warns when local trunk has backlog-only commits not on origin."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # Add a backlog-only commit on main (not pushed)
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "backlog: add item", cwd=repo)

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert len(issues) == 1, f"Expected 1 issue, got {issues}"
        msg = issues[0]
        assert "backlog-only" in msg, f"Expected 'backlog-only' in: {msg}"
        assert "1 backlog-only commit(s)" in msg, f"Expected count=1 in: {msg}"
        assert "origin/main" in msg, f"Expected 'origin/main' in: {msg}"
        assert "main" in msg, f"Expected trunk name 'main' in: {msg}"

    def test_warns_with_two_backlog_commits(self, tmp_path):
        """Warning shows correct count when there are two backlog-only commits ahead."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # First unpushed backlog commit
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "backlog: first update", cwd=repo)

        # Second unpushed backlog commit
        updated2 = dict(_UPDATED_BACKLOG)
        updated2["version"] = 3
        backlog_file.write_text(json.dumps(updated2, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "backlog: second update", cwd=repo)

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert len(issues) == 1, f"Expected 1 issue, got {issues}"
        assert "2 backlog-only commit(s)" in issues[0], f"Expected count=2 in: {issues[0]}"

    def test_warning_via_doctor_command_exits_nonzero(self, tmp_path):
        """doctor command exits with code 1 when trunk is backlog-only-ahead."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "backlog: add item", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}\nOutput:\n{result.output}"
        assert "backlog-only" in result.output, f"Expected 'backlog-only' in output:\n{result.output}"
        assert "origin/main" in result.output, f"Expected 'origin/main' in output:\n{result.output}"

    def test_warning_mentions_remediation(self, tmp_path):
        """Warning message includes remediation guidance."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "backlog: update", cwd=repo)

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert issues, "Expected at least one issue"
        msg = issues[0]
        # Should mention diffing against origin/<trunk> or landing state
        assert "origin/main" in msg and ("Diff PRs" in msg or "land backlog" in msg), (
            f"Expected remediation guidance in: {msg}"
        )


# ---------------------------------------------------------------------------
# Test: local trunk in sync with origin → no warning
# ---------------------------------------------------------------------------

class TestInSync:
    def test_in_sync_no_warning(self, tmp_path):
        """When local trunk == origin/trunk, doctor emits no warning."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert not issues, f"Unexpected issues: {issues}"
        # Should have a neutral 'in sync' note in ok
        in_sync_notes = [m for m in ok if "in sync" in m]
        assert in_sync_notes, f"Expected 'in sync' note in ok list: {ok}"

    def test_in_sync_via_doctor_command_passes(self, tmp_path):
        """doctor exits 0 and reports no trunk-ahead issues when in sync."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
            catch_exceptions=False,
        )

        # May exit 0 or 1 due to other checks, but must not have backlog-only warning
        assert "backlog-only" not in result.output, (
            f"Unexpected backlog-only warning when in sync:\n{result.output}"
        )
        assert "in sync" in result.output, (
            f"Expected 'in sync' note in output:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Test: ahead with non-backlog files → no drift warning
# ---------------------------------------------------------------------------

class TestAheadWithNonBacklogFiles:
    def test_no_warning_when_non_backlog_files_changed(self, tmp_path):
        """When trunk is ahead with non-backlog files, no drift warning is emitted."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # Commit a non-backlog file change (not pushed)
        (repo / "src.py").write_text("print('hello')")
        _git("add", "src.py", cwd=repo)
        _git("commit", "-m", "add source file", cwd=repo)

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert not issues, f"Got unexpected drift warning: {issues}"

    def test_neutral_note_when_non_backlog_files_changed(self, tmp_path):
        """A neutral note appears in ok when trunk is ahead with non-backlog changes."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        (repo / "README.md").write_text("# My project")
        _git("add", "README.md", cwd=repo)
        _git("commit", "-m", "add readme", cwd=repo)

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert not issues, f"Got unexpected drift warning: {issues}"
        neutral_notes = [m for m in ok if "non-backlog" in m or "incl." in m]
        assert neutral_notes, f"Expected a neutral note about non-backlog changes in ok: {ok}"

    def test_no_warning_when_backlog_and_code_both_changed(self, tmp_path):
        """When trunk is ahead with both backlog.json AND other files, no drift warning."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # Commit both backlog.json + another file (not pushed)
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))
        (repo / "feature.py").write_text("x = 1")
        _git("add", "backlog.json", "feature.py", cwd=repo)
        _git("commit", "-m", "add feature + update backlog", cwd=repo)

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert not issues, f"Got unexpected drift warning when non-backlog files also changed: {issues}"


# ---------------------------------------------------------------------------
# Test: no origin remote → graceful skip
# ---------------------------------------------------------------------------

class TestNoOriginRemote:
    def test_no_crash_no_origin(self, tmp_path):
        """When there is no origin remote, check skips gracefully without crashing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@example.com", cwd=repo)
        _git("config", "user.name", "Test User", cwd=repo)

        backlog_file = repo / "backlog.json"
        backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "initial", cwd=repo)

        # No remote added — origin/main does not exist
        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert not issues, f"Got unexpected issue when no remote: {issues}"
        # Should have a graceful skip note in ok
        skip_notes = [m for m in ok if "absent" in m or "skip" in m or "no origin" in m]
        assert skip_notes, f"Expected graceful skip note in ok: {ok}"

    def test_no_crash_no_origin_via_doctor(self, tmp_path):
        """doctor command does not crash when there is no origin remote."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@example.com", cwd=repo)
        _git("config", "user.name", "Test User", cwd=repo)

        backlog_file = repo / "backlog.json"
        backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "initial", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            catch_exceptions=False,
        )

        if result.exception is not None and not isinstance(result.exception, (SystemExit, int)):
            raise AssertionError(f"Unexpected exception: {result.exception}") from result.exception
        # Must not say "backlog-only"
        assert "backlog-only" not in result.output, (
            f"Got unexpected drift warning without origin:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Test: origin/<trunk> ref absent → graceful skip
# ---------------------------------------------------------------------------

class TestOriginRefAbsent:
    def test_graceful_skip_when_origin_ref_absent(self, tmp_path):
        """When origin exists but origin/<trunk> ref does not, skip gracefully."""
        origin = tmp_path / "origin.git"
        origin.mkdir()
        # Init bare origin with a DIFFERENT branch name so origin/main won't exist
        subprocess.run(
            ["git", "init", "--bare", "-b", "otherbranch"],
            cwd=origin, check=True, capture_output=True,
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
        _git("commit", "-m", "initial", cwd=repo)
        # NOTE: we do NOT push, so origin/main does not exist as a ref

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert not issues, f"Got unexpected issue when origin/trunk absent: {issues}"
        skip_notes = [m for m in ok if "absent" in m or "skip" in m or "no origin" in m]
        assert skip_notes, f"Expected graceful skip note in ok: {ok}"

    def test_no_crash_when_origin_ref_absent_via_doctor(self, tmp_path):
        """doctor does not crash when origin exists but origin/<trunk> ref is missing."""
        origin = tmp_path / "origin.git"
        origin.mkdir()
        subprocess.run(
            ["git", "init", "--bare", "-b", "otherbranch"],
            cwd=origin, check=True, capture_output=True,
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
        _git("commit", "-m", "initial", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            catch_exceptions=False,
        )

        if result.exception is not None and not isinstance(result.exception, (SystemExit, int)):
            raise AssertionError(f"Unexpected exception: {result.exception}") from result.exception
        assert "backlog-only" not in result.output, (
            f"Got unexpected drift warning:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Test: outside git repo → graceful skip
# ---------------------------------------------------------------------------

class TestOutsideGitRepo:
    def test_graceful_skip_outside_git_repo(self, tmp_path):
        """Outside a git repo, trunk-ahead check skips gracefully."""
        non_git = tmp_path / "not-a-repo"
        non_git.mkdir()
        backlog_file = non_git / "backlog.json"
        backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))

        ok: list = []
        issues: list = []
        _check_trunk_ahead_of_origin(backlog_file, ok, issues)

        assert not issues, f"Got unexpected issue outside git repo: {issues}"
        skip_notes = [m for m in ok if "git" in m.lower() or "skip" in m.lower()]
        assert skip_notes, f"Expected graceful skip note in ok: {ok}"


# ---------------------------------------------------------------------------
# Test: existing _check_backlog_divergence behavior unchanged
# ---------------------------------------------------------------------------

class TestExistingDivergenceUnchanged:
    def test_both_checks_run_in_doctor(self, tmp_path):
        """Both _check_backlog_divergence and _check_trunk_ahead_of_origin run in doctor."""
        repo, _ = _init_repo_with_origin(tmp_path)
        backlog_file = repo / "backlog.json"

        # Set up on a feature branch with diverged backlog (triggers _check_backlog_divergence)
        _git("checkout", "-b", "feature/work", cwd=repo)
        backlog_file.write_text(json.dumps(_UPDATED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "diverged backlog on feature", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            catch_exceptions=False,
        )

        output = result.output
        # The divergence check should still warn
        assert "differs from" in output or "diverge" in output.lower() or "differ" in output.lower(), (
            f"Expected divergence warning still present:\n{output}"
        )
        # The trunk-ahead check should also run (in sync or skip note)
        assert "trunk-ahead" in output or "in sync" in output or "absent" in output or "skip" in output, (
            f"Expected trunk-ahead check output:\n{output}"
        )
