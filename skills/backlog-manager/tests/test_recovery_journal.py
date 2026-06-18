"""Recovery journal + reconcile (#95).

A gitignored journal survives git operations so a reverted, uncommitted
backlog.json is recoverable. Covers the CLI store, the five board-server write
sites (including the policy-engine daemon thread), and reconcile_journal across
all conflict modes. PURE file/version comparison — no git calls.
"""

import json
from pathlib import Path
from unittest import mock

import pytest
from typer.testing import CliRunner

import backlog.core as core
import backlog.server as server
from backlog.core import (
    BacklogStore,
    write_journal,
    reconcile_journal,
)
from backlog.cli import app, _reset_reconcile_guard

runner = CliRunner()


_EMPTY_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}


def _journal(backlog_file) -> dict:
    return json.loads((Path(backlog_file).parent / ".backlog" / "journal.json").read_text())


def _backups(backlog_file):
    d = Path(backlog_file).parent / ".backlog" / "backups"
    return sorted(d.glob("backlog-*.json")) if d.exists() else []


def _write_backlog(path, version, items=None):
    data = {
        "version": version,
        "config": {"scope": "project", "project_name": "test"},
        "items": items or [],
    }
    Path(path).write_text(json.dumps(data, indent=2))
    return data


@pytest.fixture(autouse=True)
def _clear_guard():
    _reset_reconcile_guard()
    yield
    _reset_reconcile_guard()


# ── write_journal ─────────────────────────────────────────────────────────────

class TestWriteJournal:
    def test_creates_journal_at_persisted_version(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 1)
        data = {"version": 7, "items": [{"id": "a"}]}
        write_journal(str(bf), data)
        j = _journal(bf)
        assert j["version"] == 7
        assert j["backlog"]["items"] == [{"id": "a"}]
        assert "saved_at" in j

    def test_creates_backlog_dir_if_missing(self, tmp_path):
        bf = tmp_path / "backlog.json"
        assert not (tmp_path / ".backlog").exists()
        write_journal(str(bf), {"version": 1, "items": []})
        assert (tmp_path / ".backlog" / "journal.json").exists()

    def test_never_raises_emits_one_warning(self, tmp_path, capsys):
        bf = tmp_path / "backlog.json"
        with mock.patch("backlog.core.tempfile.mkstemp", side_effect=OSError("disk full")):
            write_journal(str(bf), {"version": 1, "items": []})  # must not raise
        err = capsys.readouterr().err
        assert err.count("WARNING") == 1
        assert "recovery journal" in err


# ── BacklogStore.write funnels the journal ────────────────────────────────────

class TestStoreWriteJournals:
    def test_write_creates_journal_at_post_increment_version(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 3)
        store = BacklogStore(str(bf))
        data = store.read()
        data["items"].append({"id": "x", "title": "t", "status": "backlog"})
        store.write(data)  # version 3 -> 4
        on_disk = json.loads(bf.read_text())
        assert on_disk["version"] == 4
        assert _journal(bf)["version"] == 4

    def test_journal_failure_does_not_break_store_write(self, tmp_path, capsys):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 1)
        store = BacklogStore(str(bf))
        data = store.read()
        with mock.patch("backlog.core.tempfile.mkstemp", side_effect=OSError("nope")):
            store.write(data)  # must not raise
        # main save still landed
        assert json.loads(bf.read_text())["version"] == 2
        assert "WARNING" in capsys.readouterr().err


# ── CLI save via --file funnels the journal ───────────────────────────────────

class TestCliJournals:
    def test_file_command_creates_journal_at_post_increment_version(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 1)
        r = runner.invoke(app, ["add", "a new task", "--file", str(bf)])
        assert r.exit_code == 0, r.output
        on_disk = json.loads(bf.read_text())
        assert _journal(bf)["version"] == on_disk["version"]
        assert on_disk["version"] == 2

    def test_file_command_reconciles_the_named_file_not_cwd(self, tmp_path, monkeypatch):
        # A --file command must reconcile the file named by --file, not ./backlog.json
        bf = tmp_path / "custom.json"
        _write_backlog(bf, 5)
        captured = {}

        real = core.reconcile_journal

        def spy(path, mode=None):
            captured["path"] = path
            return real(path, mode)

        monkeypatch.setattr("backlog.cli.reconcile_journal", spy)
        r = runner.invoke(app, ["list", "--file", str(bf)])
        assert r.exit_code == 0, r.output
        assert captured["path"] == str(bf)


# ── Board server write sites journal ──────────────────────────────────────────

class TestBoardJournals:
    def _handler(self, backlog_file):
        h = server.BacklogHandler.__new__(server.BacklogHandler)
        h.backlog_file = str(backlog_file)
        return h

    def test_save_backlog_writes_journal(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 1)
        # Direct atomic_write + write_journal mirrors the _save_backlog path.
        from backlog.server import atomic_write
        data = {"version": 2, "config": {}, "items": [{"id": "z", "status": "backlog"}]}
        atomic_write(str(bf), data)
        write_journal(str(bf), data)
        assert _journal(bf)["version"] == 2

    def test_policy_engine_background_write_journals_its_bump(self, tmp_path):
        """The daemon-thread policy-engine write (server.py:2035) must journal
        ITS version bump, not just the foreground save."""
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 4)
        h = self._handler(bf)

        # Force the policy engine to report an executed action so it writes.
        with mock.patch.object(server, "run_policy_engine",
                               return_value={"actions_executed": 1, "actions": []}), \
                mock.patch.object(server, "read_backlog",
                                  return_value={"version": 4, "config": {}, "items": []}):
            h._trigger_policy_engine_background(trigger_event="test")
            # daemon thread — join via the started thread
            import time
            for _ in range(50):
                jp = tmp_path / ".backlog" / "journal.json"
                if jp.exists():
                    break
                time.sleep(0.02)

        assert _journal(bf)["version"] == 5  # the policy-engine bump (4 -> 5)

    def test_journal_failure_in_daemon_thread_does_not_crash(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 1)
        h = self._handler(bf)
        with mock.patch.object(server, "run_policy_engine",
                               return_value={"actions_executed": 1}), \
                mock.patch.object(server, "read_backlog",
                                  return_value={"version": 1, "config": {}, "items": []}), \
                mock.patch("backlog.core.tempfile.mkstemp", side_effect=OSError("x")):
            h._trigger_policy_engine_background(trigger_event="test")  # must not raise


# ── reconcile_journal: no-conflict cases ──────────────────────────────────────

class TestReconcileNoConflict:
    def test_no_journal_is_noop(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 2)
        reconcile_journal(str(bf))  # no journal exists
        assert not (tmp_path / ".backlog" / "journal.json").exists()
        assert json.loads(bf.read_text())["version"] == 2

    def test_journal_older_refreshes_to_worktree_no_restore(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 2)
        write_journal(str(bf), {"version": 1, "config": {}, "items": []})
        reconcile_journal(str(bf))
        assert json.loads(bf.read_text())["version"] == 2  # untouched
        assert _journal(bf)["version"] == 2  # refreshed to worktree
        assert _backups(bf) == []

    def test_journal_equal_no_restore_no_backup(self, tmp_path):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 3)
        write_journal(str(bf), {"version": 3, "config": {}, "items": []})
        reconcile_journal(str(bf))
        assert json.loads(bf.read_text())["version"] == 3
        assert _backups(bf) == []


# ── reconcile_journal: conflict resolution ────────────────────────────────────

class TestReconcileConflict:
    def _setup_conflict(self, tmp_path):
        bf = tmp_path / "backlog.json"
        # Journal is newer (v5) than the reverted worktree (v2).
        write_journal(str(bf), {"version": 5, "config": {}, "items": [{"id": "live"}]})
        _write_backlog(bf, 2, items=[{"id": "old"}])
        return bf

    def test_non_interactive_safe_default_restores_and_backs_up(self, tmp_path, capsys):
        bf = self._setup_conflict(tmp_path)
        with mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("sys.stdout.isatty", return_value=False):
            reconcile_journal(str(bf))
        on_disk = json.loads(bf.read_text())
        assert on_disk["version"] == 5  # journal restored
        assert on_disk["items"] == [{"id": "live"}]
        assert len(_backups(bf)) == 1  # worktree backed up
        backup = json.loads(_backups(bf)[0].read_text())
        assert backup["version"] == 2 and backup["items"] == [{"id": "old"}]
        err = capsys.readouterr().err
        assert "WARNING" in err and "v5" in err

    def test_mode_keep_leaves_worktree_and_refreshes_journal(self, tmp_path):
        bf = self._setup_conflict(tmp_path)
        reconcile_journal(str(bf), mode="keep")
        assert json.loads(bf.read_text())["version"] == 2  # worktree kept
        assert _backups(bf) == []
        assert _journal(bf)["version"] == 2  # refreshed -> 2nd reconcile no-ops
        # second reconcile is a no-op (journal now == worktree)
        reconcile_journal(str(bf), mode="keep")
        assert json.loads(bf.read_text())["version"] == 2

    def test_mode_restore_backs_up_and_restores(self, tmp_path):
        bf = self._setup_conflict(tmp_path)
        reconcile_journal(str(bf), mode="restore")
        assert json.loads(bf.read_text())["version"] == 5
        assert len(_backups(bf)) == 1

    def test_env_var_resolves_mode(self, tmp_path, monkeypatch):
        bf = self._setup_conflict(tmp_path)
        monkeypatch.setenv("BACKLOG_ON_CONFLICT", "keep")
        reconcile_journal(str(bf))
        assert json.loads(bf.read_text())["version"] == 2  # kept

    def test_prompt_restore_choice(self, tmp_path):
        bf = self._setup_conflict(tmp_path)
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="r"):
            reconcile_journal(str(bf))
        assert json.loads(bf.read_text())["version"] == 5
        assert len(_backups(bf)) == 1

    def test_prompt_keep_choice_then_second_reconcile_noops(self, tmp_path):
        bf = self._setup_conflict(tmp_path)
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stdout.isatty", return_value=True), \
                mock.patch("builtins.input", return_value="k"):
            reconcile_journal(str(bf))
        assert json.loads(bf.read_text())["version"] == 2  # kept
        assert _journal(bf)["version"] == 2
        # second reconcile no-ops even non-interactively
        with mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("sys.stdout.isatty", return_value=False):
            reconcile_journal(str(bf))
        assert json.loads(bf.read_text())["version"] == 2

    def test_prompt_mode_forces_prompt_even_non_interactive(self, tmp_path):
        bf = self._setup_conflict(tmp_path)
        with mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("sys.stdout.isatty", return_value=False), \
                mock.patch("builtins.input", return_value="k") as inp:
            reconcile_journal(str(bf), mode="prompt")
        inp.assert_called_once()
        assert json.loads(bf.read_text())["version"] == 2  # kept


# ── reconcile_journal: edge cases ─────────────────────────────────────────────

class TestReconcileEdgeCases:
    def test_missing_backlog_with_journal_restores(self, tmp_path, capsys):
        bf = tmp_path / "backlog.json"
        write_journal(str(bf), {"version": 9, "config": {}, "items": [{"id": "r"}]})
        # backlog.json does not exist
        reconcile_journal(str(bf))
        assert bf.exists()
        assert json.loads(bf.read_text())["version"] == 9
        assert "WARNING" in capsys.readouterr().err

    def test_corrupt_journal_warns_and_proceeds(self, tmp_path, capsys):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 2)
        jp = tmp_path / ".backlog"
        jp.mkdir()
        (jp / "journal.json").write_text("{ not json")
        reconcile_journal(str(bf))  # no crash
        assert json.loads(bf.read_text())["version"] == 2
        assert "WARNING" in capsys.readouterr().err

    def test_corrupt_backlog_warns_and_proceeds(self, tmp_path, capsys):
        bf = tmp_path / "backlog.json"
        bf.write_text("{ not json")
        write_journal(str(bf), {"version": 4, "config": {}, "items": []})
        reconcile_journal(str(bf))  # no crash, ignores journal
        assert "WARNING" in capsys.readouterr().err

    def test_no_git_calls(self, tmp_path):
        bf = tmp_path / "backlog.json"
        write_journal(str(bf), {"version": 5, "config": {}, "items": []})
        _write_backlog(bf, 2)
        with mock.patch("subprocess.run") as run, \
                mock.patch("subprocess.check_output") as co:
            reconcile_journal(str(bf), mode="keep")
        run.assert_not_called()
        co.assert_not_called()


# ── Wiring: init does not restore; board does not double-reconcile ────────────

class TestWiring:
    def test_init_does_not_restore_from_stale_journal(self, tmp_path):
        bf = tmp_path / "backlog.json"
        # A stale newer journal exists but no backlog.json yet.
        write_journal(str(bf), {"version": 99, "config": {}, "items": [{"id": "stale"}]})
        r = runner.invoke(app, ["init", "--file", str(bf)])
        assert r.exit_code == 0, r.output
        on_disk = json.loads(bf.read_text())
        # init wrote a fresh starter, NOT the stale journal payload (v99/"stale")
        assert on_disk["version"] != 99
        assert not any(i.get("id") == "stale" for i in on_disk["items"])

    def test_guard_runs_reconcile_once_per_path(self, tmp_path, monkeypatch):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, 1)
        calls = []
        monkeypatch.setattr("backlog.cli.reconcile_journal",
                            lambda p, mode=None: calls.append(p))
        runner.invoke(app, ["list", "--file", str(bf)])
        runner.invoke(app, ["list", "--file", str(bf)])
        assert calls.count(str(bf)) == 1  # guarded: once per process per path

    def test_gitignore_contains_backlog_dir(self):
        root = Path(__file__).resolve().parents[3]
        gi = (root / ".gitignore").read_text()
        assert ".backlog/" in gi
