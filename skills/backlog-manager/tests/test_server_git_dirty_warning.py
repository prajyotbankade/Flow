"""Tests for git dirty-file warning on backlog-server startup."""

import subprocess
import sys
from unittest.mock import patch, MagicMock

import pytest

from backlog.server import check_git_dirty_warning


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_process(stdout: str, returncode: int = 0):
    result = MagicMock()
    result.stdout = stdout
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# check_git_dirty_warning — unit tests
# ---------------------------------------------------------------------------

class TestGitDirtyWarning:
    """Unit tests for check_git_dirty_warning(file_path, stderr)."""

    def _run(self, stdout: str, returncode: int = 0, side_effect=None,
             no_git_check: bool = False, file_path: str = "/repo/backlog.json"):
        """Helper: patch subprocess.run and capture stderr output."""
        captured = []

        def fake_stderr(msg):
            captured.append(msg)

        with patch("backlog.server.subprocess.run") as mock_run:
            if side_effect is not None:
                mock_run.side_effect = side_effect
            else:
                mock_run.return_value = _make_completed_process(stdout, returncode)

            check_git_dirty_warning(
                file_path=file_path,
                print_warning=fake_stderr,
                no_git_check=no_git_check,
            )

        return captured, mock_run

    # --- dirty cases ---

    def test_modified_file_warns(self):
        captured, _ = self._run(" M backlog.json")
        assert any("WARNING" in m for m in captured)

    def test_untracked_file_warns(self):
        """?? prefix (untracked) should also trigger the warning."""
        captured, _ = self._run("?? backlog.json")
        assert any("WARNING" in m for m in captured)

    def test_staged_change_warns(self):
        captured, _ = self._run("M  backlog.json")
        assert any("WARNING" in m for m in captured)

    # --- clean cases ---

    def test_clean_file_no_warning(self):
        captured, _ = self._run("")  # empty output = clean
        assert captured == []

    # --- suppression ---

    def test_no_git_check_flag_suppresses(self):
        """--no-git-check suppresses the git check entirely."""
        captured, mock_run = self._run(" M backlog.json", no_git_check=True)
        mock_run.assert_not_called()
        assert captured == []

    def test_env_var_suppresses(self, monkeypatch):
        """BACKLOG_NO_GIT_CHECK=1 suppresses the git check."""
        monkeypatch.setenv("BACKLOG_NO_GIT_CHECK", "1")
        captured = []

        with patch("backlog.server.subprocess.run") as mock_run:
            from backlog.server import check_git_dirty_warning
            check_git_dirty_warning(
                file_path="/repo/backlog.json",
                print_warning=captured.append,
                no_git_check=False,  # flag is off — env var should still suppress
            )
            mock_run.assert_not_called()

        assert captured == []

    # --- failure modes ---

    def test_git_not_installed_skips_silently(self):
        """FileNotFoundError (git not installed) → no warning, no crash."""
        captured, _ = self._run("", side_effect=FileNotFoundError("git not found"))
        assert captured == []

    def test_not_a_git_repo_skips_silently(self):
        """Non-zero returncode (not a git repo) → no warning, no crash."""
        captured, _ = self._run("", returncode=128)
        assert captured == []

    def test_timeout_skips_silently(self):
        """subprocess.TimeoutExpired → no warning, no crash."""
        captured, _ = self._run(
            "", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=1)
        )
        assert captured == []

    def test_warning_message_text(self):
        """Warning message matches the exact spec."""
        captured, _ = self._run(" M backlog.json")
        assert captured == [
            "WARNING: backlog.json has uncommitted git changes"
            " — server state may diverge from committed history"
        ]
