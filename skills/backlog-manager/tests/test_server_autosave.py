"""Tests for server auto-commit on shutdown and periodic auto-save."""

import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import MagicMock, call, patch

import pytest

# We import the functions directly from server module
from backlog.server import (
    _auto_commit,
    _start_periodic_commit,
    _parse_protected_branches,
    DEFAULT_PROTECTED_BRANCHES,
)


def _branch_result(name):
    """git rev-parse --abbrev-ref HEAD result returning *name*."""
    return _make_subprocess_result(returncode=0, stdout=name.encode())


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_subprocess_result(returncode=0, stdout=b"", stderr=b""):
    r = MagicMock()
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ---------------------------------------------------------------------------
# _auto_commit — dirty file triggers git add + git commit
# ---------------------------------------------------------------------------

class TestAutoCommit:

    def test_dirty_file_triggers_commit(self, tmp_path):
        """When git diff returns non-zero (dirty), auto-commit runs."""
        fake_file = str(tmp_path / "backlog.json")
        branch     = _branch_result("feature/x")  # non-protected
        diff_dirty = _make_subprocess_result(returncode=1)   # 1 = dirty
        add_ok     = _make_subprocess_result(returncode=0)
        commit_ok  = _make_subprocess_result(returncode=0)

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch, diff_dirty, add_ok, commit_ok]) as mock_run:
            _auto_commit(fake_file)

        calls = mock_run.call_args_list
        assert any("add" in str(c) for c in calls), "git add was not called"
        assert any("commit" in str(c) for c in calls), "git commit was not called"

    def test_clean_file_is_noop(self, tmp_path):
        """When git diff returns 0 (clean), neither add nor commit is called."""
        fake_file = str(tmp_path / "backlog.json")
        branch     = _branch_result("feature/x")  # non-protected
        diff_clean = _make_subprocess_result(returncode=0)   # 0 = clean

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch, diff_clean]) as mock_run:
            _auto_commit(fake_file)

        # Only branch lookup + git diff — no add, no commit
        calls = mock_run.call_args_list
        assert mock_run.call_count == 2
        assert "rev-parse" in str(calls[0])
        assert "diff" in str(calls[1])
        assert not any("add" in str(c) or "commit" in str(c) for c in calls)

    def test_git_commit_failure_logs_warning_not_crash(self, tmp_path, capsys):
        """When git commit fails, a WARNING is printed and no exception is raised."""
        fake_file = str(tmp_path / "backlog.json")
        branch      = _branch_result("feature/x")  # non-protected
        diff_dirty  = _make_subprocess_result(returncode=1)
        add_ok      = _make_subprocess_result(returncode=0)
        commit_fail = _make_subprocess_result(returncode=1)

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch, diff_dirty, add_ok, commit_fail]):
            _auto_commit(fake_file)   # must not raise

        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "auto-commit" in captured.err.lower()

    def test_git_not_available_does_not_crash(self, tmp_path):
        """If git is not available (FileNotFoundError), _auto_commit swallows the error."""
        fake_file = str(tmp_path / "backlog.json")

        with patch("backlog.server.subprocess.run", side_effect=FileNotFoundError("no git")):
            _auto_commit(fake_file)   # must not raise

    def test_git_not_in_repo_does_not_crash(self, tmp_path):
        """If file is not in a git repo (non-zero diff returncode but also OSError), skip silently."""
        fake_file = str(tmp_path / "backlog.json")

        with patch("backlog.server.subprocess.run", side_effect=OSError("not a repo")):
            _auto_commit(fake_file)   # must not raise


# ---------------------------------------------------------------------------
# _start_periodic_commit — timer fires at interval
# ---------------------------------------------------------------------------

class TestPeriodicCommit:

    def test_periodic_commit_fires_after_interval(self, tmp_path):
        """With interval=0.05s, _auto_commit is called at least once."""
        fake_file = str(tmp_path / "backlog.json")
        called = threading.Event()

        def fake_auto_commit(fp, *args, **kwargs):
            called.set()

        with patch("backlog.server._auto_commit", side_effect=fake_auto_commit):
            _start_periodic_commit(fake_file, interval=0.05)
            fired = called.wait(timeout=2.0)

        assert fired, "_auto_commit was not called within 2 seconds"

    def test_zero_interval_disables_periodic_commit(self, tmp_path):
        """interval=0 should not start any timer — verified by patching threading.Timer."""
        import backlog.server as srv

        fake_file = str(tmp_path / "backlog.json")

        with patch("backlog.server.threading.Timer") as mock_timer:
            srv._start_periodic_commit(fake_file, interval=0)

        mock_timer.assert_not_called()


# ---------------------------------------------------------------------------
# main() — argparse and suppress flags
# ---------------------------------------------------------------------------

class TestMainAutosaveFlag:
    """Verify that --no-git-check and env var suppress auto-commit registration."""

    def _run_main_and_capture_atexit(self, argv, env_override=None):
        """
        Run main() with given argv, intercept atexit.register calls,
        and return the list of registered functions.
        """
        import backlog.server as srv

        registered = []

        def fake_atexit(fn, *args, **kwargs):
            registered.append((fn, args))

        # Patch everything that would make serve_forever block or open browser
        with patch.object(srv, "atexit") as mock_atexit, \
             patch("backlog.server._start_periodic_commit") as mock_periodic, \
             patch("backlog.server.ThreadingBacklogServer") as mock_httpserver, \
             patch("backlog.server.webbrowser.open"), \
             patch("backlog.server.parse_agent_files", return_value={}), \
             patch("backlog.server.sys.argv", ["backlog-server"] + argv):

            mock_httpserver.return_value.serve_forever.side_effect = KeyboardInterrupt
            mock_atexit.register.side_effect = fake_atexit

            old_env = {}
            if env_override:
                for k, v in env_override.items():
                    old_env[k] = os.environ.get(k)
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            try:
                srv.main()
            except SystemExit:
                pass
            finally:
                for k, orig in old_env.items():
                    if orig is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = orig

        return registered, mock_periodic

    def test_no_git_check_flag_suppresses_atexit(self, tmp_path):
        """--no-git-check must prevent atexit registration."""
        bf = str(tmp_path / "backlog.json")
        (tmp_path / "backlog.json").write_text('{"version":1,"items":[]}')
        registered, mock_periodic = self._run_main_and_capture_atexit(
            ["--file", bf, "--no-open", "--no-git-check"]
        )
        auto_commit_fns = [fn for fn, _ in registered]
        import backlog.server as srv
        assert srv._auto_commit not in auto_commit_fns
        mock_periodic.assert_not_called()

    def test_env_var_suppresses_atexit(self, tmp_path):
        """BACKLOG_NO_GIT_CHECK=1 must prevent atexit registration."""
        bf = str(tmp_path / "backlog.json")
        (tmp_path / "backlog.json").write_text('{"version":1,"items":[]}')
        registered, mock_periodic = self._run_main_and_capture_atexit(
            ["--file", bf, "--no-open"],
            env_override={"BACKLOG_NO_GIT_CHECK": "1"}
        )
        auto_commit_fns = [fn for fn, _ in registered]
        import backlog.server as srv
        assert srv._auto_commit not in auto_commit_fns
        mock_periodic.assert_not_called()

    def test_autosave_interval_default_registered(self, tmp_path):
        """Without suppression flags, _start_periodic_commit is called with default 300."""
        bf = str(tmp_path / "backlog.json")
        (tmp_path / "backlog.json").write_text('{"version":1,"items":[]}')
        registered, mock_periodic = self._run_main_and_capture_atexit(
            ["--file", bf, "--no-open"],
            env_override={"BACKLOG_NO_GIT_CHECK": None}  # ensure cleared
        )
        mock_periodic.assert_called_once()
        _, kwargs_or_args = mock_periodic.call_args
        # _start_periodic_commit(backlog_file, interval)
        args = mock_periodic.call_args[0]
        assert args[1] == 300, f"Expected default interval 300, got {args[1]}"

    def test_autosave_interval_custom(self, tmp_path):
        """--autosave-interval 60 passes 60 to _start_periodic_commit."""
        bf = str(tmp_path / "backlog.json")
        (tmp_path / "backlog.json").write_text('{"version":1,"items":[]}')
        registered, mock_periodic = self._run_main_and_capture_atexit(
            ["--file", bf, "--no-open", "--autosave-interval", "60"],
            env_override={"BACKLOG_NO_GIT_CHECK": None}
        )
        args = mock_periodic.call_args[0]
        assert args[1] == 60

    def test_autosave_interval_zero_disables(self, tmp_path):
        """--autosave-interval 0 still calls _start_periodic_commit with 0 (it handles disabling)."""
        bf = str(tmp_path / "backlog.json")
        (tmp_path / "backlog.json").write_text('{"version":1,"items":[]}')
        registered, mock_periodic = self._run_main_and_capture_atexit(
            ["--file", bf, "--no-open", "--autosave-interval", "0"],
            env_override={"BACKLOG_NO_GIT_CHECK": None}
        )
        args = mock_periodic.call_args[0]
        assert args[1] == 0


# ---------------------------------------------------------------------------
# _auto_commit — protected-branch guard (item #98)
# ---------------------------------------------------------------------------

class TestProtectedBranchGuard:

    def test_protected_branch_skips_commit(self, tmp_path):
        """On 'main', _auto_commit returns after branch lookup — no diff/add/commit."""
        fake_file = str(tmp_path / "backlog.json")
        branch = _branch_result("main")

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch]) as mock_run:
            _auto_commit(fake_file)

        calls = mock_run.call_args_list
        assert mock_run.call_count == 1, "expected only the branch lookup"
        assert "rev-parse" in str(calls[0])
        assert not any("commit" in str(c) or "add" in str(c) for c in calls)

    def test_master_branch_skips_commit(self, tmp_path):
        """'master' is protected by default as well."""
        fake_file = str(tmp_path / "backlog.json")
        branch = _branch_result("master")

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch]) as mock_run:
            _auto_commit(fake_file)

        assert mock_run.call_count == 1
        assert not any("commit" in str(c) for c in mock_run.call_args_list)

    def test_non_protected_branch_commits(self, tmp_path):
        """On 'feature/x', the commit proceeds as before."""
        fake_file = str(tmp_path / "backlog.json")
        branch     = _branch_result("feature/x")
        diff_dirty = _make_subprocess_result(returncode=1)
        add_ok     = _make_subprocess_result(returncode=0)
        commit_ok  = _make_subprocess_result(returncode=0)

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch, diff_dirty, add_ok, commit_ok]) as mock_run:
            _auto_commit(fake_file)

        calls = mock_run.call_args_list
        assert any("add" in str(c) for c in calls)
        assert any("commit" in str(c) for c in calls)

    def test_detached_head_treated_as_non_protected(self, tmp_path):
        """Detached HEAD (rev-parse returns 'HEAD') still commits."""
        fake_file = str(tmp_path / "backlog.json")
        branch     = _branch_result("HEAD")
        diff_dirty = _make_subprocess_result(returncode=1)
        add_ok     = _make_subprocess_result(returncode=0)
        commit_ok  = _make_subprocess_result(returncode=0)

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch, diff_dirty, add_ok, commit_ok]) as mock_run:
            _auto_commit(fake_file)

        calls = mock_run.call_args_list
        assert any("commit" in str(c) for c in calls), "detached HEAD should commit"

    def test_custom_protected_set_respected(self, tmp_path):
        """A custom protected set blocks a branch that is NOT a default protected name."""
        fake_file = str(tmp_path / "backlog.json")
        branch = _branch_result("release")

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch]) as mock_run:
            _auto_commit(fake_file, protected_branches={"release"})

        assert mock_run.call_count == 1, "custom protected branch should skip commit"
        assert not any("commit" in str(c) for c in mock_run.call_args_list)

    def test_custom_set_allows_main_to_commit(self, tmp_path):
        """If the custom set does not include 'main', autosave commits on main."""
        fake_file = str(tmp_path / "backlog.json")
        branch     = _branch_result("main")
        diff_dirty = _make_subprocess_result(returncode=1)
        add_ok     = _make_subprocess_result(returncode=0)
        commit_ok  = _make_subprocess_result(returncode=0)

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch, diff_dirty, add_ok, commit_ok]) as mock_run:
            _auto_commit(fake_file, protected_branches={"release"})

        assert any("commit" in str(c) for c in mock_run.call_args_list)

    def test_branch_match_is_case_sensitive(self, tmp_path):
        """'Main' (capitalised) is NOT protected by the default {main, master}."""
        fake_file = str(tmp_path / "backlog.json")
        branch     = _branch_result("Main")
        diff_dirty = _make_subprocess_result(returncode=1)
        add_ok     = _make_subprocess_result(returncode=0)
        commit_ok  = _make_subprocess_result(returncode=0)

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch, diff_dirty, add_ok, commit_ok]) as mock_run:
            _auto_commit(fake_file)

        assert any("commit" in str(c) for c in mock_run.call_args_list)

    def test_branch_lookup_failure_falls_through_to_commit(self, tmp_path):
        """If rev-parse returns non-zero, branch is unknown -> non-protected -> commit."""
        fake_file = str(tmp_path / "backlog.json")
        branch_fail = _make_subprocess_result(returncode=128, stderr=b"not a repo")
        diff_dirty  = _make_subprocess_result(returncode=1)
        add_ok      = _make_subprocess_result(returncode=0)
        commit_ok   = _make_subprocess_result(returncode=0)

        with patch("backlog.server.subprocess.run",
                   side_effect=[branch_fail, diff_dirty, add_ok, commit_ok]) as mock_run:
            _auto_commit(fake_file)

        assert any("commit" in str(c) for c in mock_run.call_args_list)

    def test_git_unavailable_no_crash_no_commit(self, tmp_path):
        """git missing: rev-parse raises, then diff raises -> no commit, no crash."""
        fake_file = str(tmp_path / "backlog.json")

        with patch("backlog.server.subprocess.run",
                   side_effect=FileNotFoundError("no git")) as mock_run:
            _auto_commit(fake_file)   # must not raise

        assert not any("commit" in str(c) for c in mock_run.call_args_list)


# ---------------------------------------------------------------------------
# _parse_protected_branches
# ---------------------------------------------------------------------------

class TestParseProtectedBranches:

    def test_empty_falls_back_to_default(self):
        assert _parse_protected_branches("") == set(DEFAULT_PROTECTED_BRANCHES)

    def test_whitespace_falls_back_to_default(self):
        assert _parse_protected_branches("   ") == set(DEFAULT_PROTECTED_BRANCHES)

    def test_none_falls_back_to_default(self):
        assert _parse_protected_branches(None) == set(DEFAULT_PROTECTED_BRANCHES)

    def test_comma_separated_parsed(self):
        assert _parse_protected_branches("main,release,prod") == {"main", "release", "prod"}

    def test_whitespace_and_empty_entries_stripped(self):
        assert _parse_protected_branches(" main , , release ") == {"main", "release"}

    def test_only_commas_falls_back_to_default(self):
        assert _parse_protected_branches(",,,") == set(DEFAULT_PROTECTED_BRANCHES)


# ---------------------------------------------------------------------------
# main() — --protected-branches CLI flag plumbing
# ---------------------------------------------------------------------------

class TestProtectedBranchesFlag(TestMainAutosaveFlag):
    """Inherits the main()-driver helper from TestMainAutosaveFlag."""

    def test_protected_branches_flag_plumbed_to_periodic(self, tmp_path):
        bf = str(tmp_path / "backlog.json")
        (tmp_path / "backlog.json").write_text('{"version":1,"items":[]}')
        registered, mock_periodic = self._run_main_and_capture_atexit(
            ["--file", bf, "--no-open", "--protected-branches", "main,release"],
            env_override={"BACKLOG_NO_GIT_CHECK": None},
        )
        args = mock_periodic.call_args[0]
        # _start_periodic_commit(backlog_file, interval, protected_branches)
        assert len(args) >= 3, "protected_branches not passed positionally"
        assert args[2] == {"main", "release"}

    def test_default_protected_branches_when_flag_absent(self, tmp_path):
        bf = str(tmp_path / "backlog.json")
        (tmp_path / "backlog.json").write_text('{"version":1,"items":[]}')
        registered, mock_periodic = self._run_main_and_capture_atexit(
            ["--file", bf, "--no-open"],
            env_override={"BACKLOG_NO_GIT_CHECK": None},
        )
        args = mock_periodic.call_args[0]
        assert args[2] == set(DEFAULT_PROTECTED_BRANCHES)
