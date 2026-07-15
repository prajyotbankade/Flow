"""Tests for backlog doctor multi-branch divergence check (backlog item #74)."""

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MINIMAL_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}

_DIVERGED_BACKLOG = {
    "version": 2,
    "config": {"scope": "project", "project_name": "test-diverged"},
    "items": [{"id": "aabb1122", "title": "Extra item on branch", "status": "backlog",
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
    assert result.returncode == 0, f"git {' '.join(args)} failed:\n{result.stderr}"
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    """Initialise a git repo with a main branch and a committed backlog.json."""
    _git("init", "-b", "main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test User", cwd=path)
    backlog_file = path / "backlog.json"
    backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
    _git("add", "backlog.json", cwd=path)
    _git("commit", "-m", "initial backlog", cwd=path)


# ---------------------------------------------------------------------------
# Test: diverged branch warns
# ---------------------------------------------------------------------------

class TestDivergenceWarns:
    def test_warns_when_branch_backlog_differs_from_trunk(self, tmp_path):
        """doctor WARNS when branch's committed backlog.json differs from main."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        # Create a feature branch and commit a diverged backlog.json
        _git("checkout", "-b", "feature/my-work", cwd=repo)
        diverged_file = repo / "backlog.json"
        diverged_file.write_text(json.dumps(_DIVERGED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "diverged backlog", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(repo / "backlog.json")],
            env={**os.environ, "BACKLOG_FILE": str(repo / "backlog.json")},
            catch_exceptions=False,
        )

        # Doctor should exit non-zero and mention divergence
        assert result.exit_code == 1, f"Expected exit 1, got {result.exit_code}.\nOutput:\n{result.output}"
        output = result.output
        assert "differ" in output.lower() or "diverge" in output.lower() or "differs" in output.lower(), (
            f"Expected divergence warning in output:\n{output}"
        )
        assert "BACKLOG_FILE" in output or "worktree" in output, (
            f"Expected remediation hint in output:\n{output}"
        )

    def test_warning_mentions_trunk_branch(self, tmp_path):
        """Divergence warning names the trunk branch so users know what to compare."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        _git("checkout", "-b", "my-feature", cwd=repo)
        (repo / "backlog.json").write_text(json.dumps(_DIVERGED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "diverged", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(repo / "backlog.json")],
            catch_exceptions=False,
        )

        # 'main' should appear somewhere in the output
        assert "main" in result.output, (
            f"Expected trunk branch name 'main' in output:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# Test: canonical setup is clean
# ---------------------------------------------------------------------------

class TestCanonicalClean:
    def test_no_divergence_warning_on_trunk(self, tmp_path):
        """On the trunk branch itself, doctor must NOT emit a divergence warning."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        # We are already on 'main' after _init_repo
        result = runner.invoke(
            app,
            ["doctor", "--file", str(repo / "backlog.json")],
            catch_exceptions=False,
        )

        output = result.output
        # The divergence warning must not appear
        assert "forked" not in output, f"False positive divergence on trunk:\n{output}"
        assert "differs from 'main'" not in output, f"False positive divergence on trunk:\n{output}"
        # Should mention the canonical-copy / no-divergence status
        assert "trunk" in output or "no divergence" in output or "canonical" in output or "main" in output, (
            f"Expected healthy note for canonical setup:\n{output}"
        )

    def test_no_divergence_when_branch_matches_trunk(self, tmp_path):
        """If a branch's committed backlog.json is identical to trunk, no warning."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        # Branch off but do NOT change backlog.json — commit something else
        _git("checkout", "-b", "feature/no-backlog-change", cwd=repo)
        other_file = repo / "other.txt"
        other_file.write_text("hello")
        _git("add", "other.txt", cwd=repo)
        _git("commit", "-m", "add other file", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(repo / "backlog.json")],
            catch_exceptions=False,
        )

        output = result.output
        assert "differs from" not in output, (
            f"False positive: got divergence warning even though backlog.json is unchanged:\n{output}"
        )


# ---------------------------------------------------------------------------
# Test: graceful degradation
# ---------------------------------------------------------------------------

class TestGracefulDegradation:
    def test_outside_git_repo_skips_check(self, tmp_path):
        """Outside a git repo, doctor must not crash and must skip divergence check."""
        non_git_dir = tmp_path / "not-a-repo"
        non_git_dir.mkdir()
        backlog_file = non_git_dir / "backlog.json"
        backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))

        # Run with cwd set to a non-git directory
        old_cwd = os.getcwd()
        try:
            os.chdir(non_git_dir)
            result = runner.invoke(
                app,
                ["doctor", "--file", str(backlog_file)],
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)

        # Must not crash with an unhandled Python exception.
        # Typer raises SystemExit/Exit(1) as result.exception for non-zero exit codes,
        # so we only reject non-integer exceptions (real tracebacks).
        if result.exception is not None and not isinstance(result.exception, (SystemExit, int)):
            raise AssertionError(f"Unexpected exception: {result.exception}") from result.exception
        # Should mention skipping or not being in a git repo
        output = result.output
        assert "git" in output.lower() or "skip" in output.lower(), (
            f"Expected graceful skip message in output:\n{output}"
        )

    def test_detached_head_skips_check(self, tmp_path):
        """Detached HEAD state: divergence check is skipped gracefully."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        # Get the commit hash and detach HEAD
        commit_hash = _git("rev-parse", "HEAD", cwd=repo)
        _git("checkout", commit_hash, cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(repo / "backlog.json")],
            catch_exceptions=False,
        )

        if result.exception is not None and not isinstance(result.exception, (SystemExit, int)):
            raise AssertionError(f"Unexpected exception: {result.exception}") from result.exception
        output = result.output
        # Should mention detached HEAD or skip
        assert "detach" in output.lower() or "skip" in output.lower() or "HEAD" in output, (
            f"Expected detached-HEAD skip note in output:\n{output}"
        )

    def test_no_trunk_branch_skips_check(self, tmp_path):
        """When neither main nor master exists and origin/HEAD is absent, skip gracefully."""
        repo = tmp_path / "repo"
        repo.mkdir()
        # Use 'trunk' as init branch — no 'main' or 'master' ref
        result_init = subprocess.run(
            ["git", "init", "-b", "trunk"],
            cwd=repo, capture_output=True, text=True,
        )
        assert result_init.returncode == 0
        _git("config", "user.email", "test@example.com", cwd=repo)
        _git("config", "user.name", "Test User", cwd=repo)
        backlog_file = repo / "backlog.json"
        backlog_file.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "initial", cwd=repo)

        # Check out a feature branch so we're not on trunk
        _git("checkout", "-b", "feature/work", cwd=repo)
        (repo / "backlog.json").write_text(json.dumps(_DIVERGED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "diverged", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(repo / "backlog.json")],
            catch_exceptions=False,
        )

        if result.exception is not None and not isinstance(result.exception, (SystemExit, int)):
            raise AssertionError(f"Unexpected exception: {result.exception}") from result.exception
        output = result.output
        # Must mention skipping (trunk not found or equivalent)
        assert "skip" in output.lower() or "no trunk" in output.lower() or "trunk" in output.lower(), (
            f"Expected skip-no-trunk message:\n{output}"
        )
        # Must not report a divergence warning (trunk wasn't found)
        assert "differs from" not in output, f"Got spurious divergence warning:\n{output}"


# ---------------------------------------------------------------------------
# Test: configurable integration_branch (backlog item #107)
# ---------------------------------------------------------------------------


def _make_backlog_with_integration_branch(branch_name: str) -> dict:
    """Return a minimal backlog.json with config.integration_branch set."""
    return {
        "version": 1,
        "config": {
            "scope": "project",
            "project_name": "test",
            "integration_branch": branch_name,
        },
        "items": [],
    }


class TestConfigurableIntegrationBranch:
    """Tests for config.integration_branch in backlog doctor (item #107)."""

    def test_on_integration_branch_no_divergence_warning(self, tmp_path):
        """When HEAD is ON the configured integration_branch, no divergence warning."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Init repo with 'stage' as the initial branch
        subprocess.run(["git", "init", "-b", "stage"], cwd=repo, check=True, capture_output=True)
        _git("config", "user.email", "test@example.com", cwd=repo)
        _git("config", "user.name", "Test User", cwd=repo)

        backlog_data = _make_backlog_with_integration_branch("stage")
        backlog_file = repo / "backlog.json"
        backlog_file.write_text(json.dumps(backlog_data, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "initial backlog on stage", cwd=repo)

        # HEAD is on 'stage' (the configured integration_branch)
        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            catch_exceptions=False,
        )

        output = result.output
        # Must NOT emit any divergence warning
        assert "differs from" not in output, (
            f"False divergence warning: HEAD is ON integration_branch 'stage':\n{output}"
        )
        assert "forked" not in output, (
            f"False forked warning on integration branch:\n{output}"
        )
        # Must mention trunk / no divergence
        assert "trunk" in output.lower() or "no divergence" in output.lower(), (
            f"Expected canonical/no-divergence note in output:\n{output}"
        )
        # Should mention 'stage' somewhere (the branch name)
        assert "stage" in output, (
            f"Expected branch name 'stage' in output:\n{output}"
        )

    def test_integration_branch_takes_precedence_over_autodetect(self, tmp_path):
        """config.integration_branch takes precedence over origin/HEAD auto-detection."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Init with 'main' as default branch (would be auto-detected as trunk)
        _git("init", "-b", "main", cwd=tmp_path / "repo")
        _git("config", "user.email", "test@example.com", cwd=repo)
        _git("config", "user.name", "Test User", cwd=repo)

        # Backlog config says integration_branch = 'stage'
        backlog_data = _make_backlog_with_integration_branch("stage")
        backlog_file = repo / "backlog.json"
        backlog_file.write_text(json.dumps(backlog_data, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "initial", cwd=repo)

        # Create and switch to 'stage' branch (this is the integration branch per config)
        _git("checkout", "-b", "stage", cwd=repo)
        # backlog.json is the same content on stage
        _git("commit", "--allow-empty", "-m", "empty stage commit", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            catch_exceptions=False,
        )

        output = result.output
        # Must NOT emit divergence warning (we are on the configured integration branch)
        assert "differs from" not in output, (
            f"False positive: should not warn when on configured integration branch:\n{output}"
        )

    def test_unset_integration_branch_unchanged_behavior(self, tmp_path):
        """When integration_branch is absent, behavior is unchanged (main detection works)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)  # uses _MINIMAL_BACKLOG (no integration_branch)

        # Branch off and commit diverged backlog
        _git("checkout", "-b", "feature/x", cwd=repo)
        (repo / "backlog.json").write_text(json.dumps(_DIVERGED_BACKLOG, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "diverged", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(repo / "backlog.json")],
            catch_exceptions=False,
        )

        # Same divergence warning as before — behavior unchanged
        output = result.output
        assert result.exit_code == 1, f"Expected exit 1:\n{output}"
        assert "differ" in output.lower() or "diverge" in output.lower(), (
            f"Expected divergence warning with no integration_branch set:\n{output}"
        )

    def test_missing_integration_branch_graceful_degrade(self, tmp_path):
        """When config.integration_branch names a non-existent branch, degrade gracefully."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git("init", "-b", "main", cwd=repo)
        _git("config", "user.email", "test@example.com", cwd=repo)
        _git("config", "user.name", "Test User", cwd=repo)

        # Set integration_branch to a branch that does NOT exist
        backlog_data = _make_backlog_with_integration_branch("nonexistent-branch")
        backlog_file = repo / "backlog.json"
        backlog_file.write_text(json.dumps(backlog_data, indent=2))
        _git("add", "backlog.json", cwd=repo)
        _git("commit", "-m", "initial", cwd=repo)

        result = runner.invoke(
            app,
            ["doctor", "--file", str(backlog_file)],
            catch_exceptions=False,
        )

        # Must not raise an unhandled exception
        if result.exception is not None and not isinstance(result.exception, (SystemExit, int)):
            raise AssertionError(f"Unexpected exception: {result.exception}") from result.exception

        output = result.output
        # Must mention the missing branch or note degradation (not crash)
        assert "nonexistent-branch" in output or "not found" in output.lower() or "skip" in output.lower() or "warn" in output.lower() or "does not exist" in output.lower(), (
            f"Expected graceful-degrade note for missing branch in output:\n{output}"
        )
