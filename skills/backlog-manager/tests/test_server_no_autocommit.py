"""The board server must NEVER mutate git (item #94).

These tests assert that across the save path AND process startup/shutdown the
board only ever issues read-only git invocations (status / rev-parse / config)
and never `git add` or `git commit`. They also guard against the removed
auto-commit wiring (atexit handler + SIGTERM trap) reappearing.
"""

import json
import subprocess
from unittest import mock

import pytest

import backlog.server as server
from backlog.core import BacklogStore


_MUTATING_GIT = {"add", "commit", "stash", "reset", "rm", "checkout"}


def _git_subcommand(argv):
    """Return the git subcommand from an argv list, or None if not a git call."""
    if not argv:
        return None
    if argv[0] != "git":
        return None
    return argv[1] if len(argv) > 1 else None


def _assert_no_mutating_git(calls):
    for call in calls:
        argv = call.args[0] if call.args else call.kwargs.get("args")
        if not isinstance(argv, (list, tuple)):
            continue
        sub = _git_subcommand(argv)
        assert sub not in _MUTATING_GIT, f"board issued a mutating git call: {argv}"


_EMPTY_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}


@pytest.fixture
def backlog_path(tmp_path):
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
    return p


# ---------------------------------------------------------------------------
# Save path
# ---------------------------------------------------------------------------

class TestSavePathNoGitMutation:
    def test_store_write_issues_no_subprocess_at_all(self, backlog_path):
        """An atomic save (BacklogStore.write) must not shell out to git."""
        store = BacklogStore(str(backlog_path))
        with mock.patch("subprocess.run") as run, \
                mock.patch("subprocess.check_output") as check_output:
            data = store.read()
            data["items"].append({"id": "x1", "title": "edit", "status": "backlog"})
            store.write(data)

        run.assert_not_called()
        check_output.assert_not_called()

        # The edit landed on disk via the atomic os.replace path.
        on_disk = json.loads(backlog_path.read_text())
        assert any(i["title"] == "edit" for i in on_disk["items"])


# ---------------------------------------------------------------------------
# Startup + shutdown lifecycle
# ---------------------------------------------------------------------------

class TestLifecycleNoGitMutation:
    def test_main_startup_and_shutdown_make_no_mutating_git_calls(
        self, backlog_path, monkeypatch
    ):
        """Running main() through startup and a Ctrl-C shutdown must only ever
        fire read-only git commands — never add/commit."""
        monkeypatch.setattr(
            "sys.argv",
            ["backlog-server", "--file", str(backlog_path), "--no-open", "--port", "0"],
        )

        # Read-only git calls (status / config) return clean/empty results.
        def fake_run(argv, *a, **kw):
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = "" if kw.get("text") else b""
            result.stderr = "" if kw.get("text") else b""
            return result

        # serve_forever raises KeyboardInterrupt to simulate Ctrl-C shutdown.
        fake_server = mock.MagicMock()
        fake_server.serve_forever.side_effect = KeyboardInterrupt()

        with mock.patch("subprocess.run", side_effect=fake_run) as run, \
                mock.patch("subprocess.check_output", return_value=b"unknown"), \
                mock.patch.object(server, "ThreadingBacklogServer", return_value=fake_server):
            server.main()

        # Clean shutdown path was taken (Ctrl-C).
        fake_server.server_close.assert_called_once()
        _assert_no_mutating_git(run.call_args_list)

    def test_no_atexit_or_sigterm_autocommit_wiring(self):
        """The removed auto-commit wiring must not reappear: no atexit handler
        and no SIGTERM trap are registered by main()."""
        # The auto-commit symbols are gone entirely.
        for sym in (
            "_auto_commit",
            "_start_periodic_commit",
            "_current_git_branch",
            "_parse_protected_branches",
            "DEFAULT_PROTECTED_BRANCHES",
        ):
            assert not hasattr(server, sym), f"{sym} should have been removed"

        # The module no longer imports atexit or the signal module.
        assert not hasattr(server, "atexit")
        assert not hasattr(server, "signal")
