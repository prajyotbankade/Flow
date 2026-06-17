"""CLI tests for the `board` command: the board no longer forwards autosave /
protected-branch guard flags — the board never commits to git (item #94)."""

import json
from unittest import mock

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()

_EMPTY_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}


@pytest.fixture
def backlog_file(tmp_path):
    p = tmp_path / "backlog.json"
    p.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
    return str(p)


def _argv_of(call):
    """First positional arg (the argv list) of a subprocess.run call."""
    return call.args[0] if call.args else call.kwargs["args"]


class TestBoardDoesNotForwardGuardFlags:
    def test_default_forwards_only_file_and_port(self, backlog_file):
        with mock.patch("backlog.cli.subprocess.run") as run:
            result = runner.invoke(app, ["board", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        argv = _argv_of(run.call_args)
        assert "--autosave-interval" not in argv
        assert "--protected-branches" not in argv
        assert "--file" in argv
        assert "--port" in argv

    def test_autosave_interval_flag_rejected_by_typer(self, backlog_file):
        with mock.patch("backlog.cli.subprocess.run") as run:
            result = runner.invoke(
                app, ["board", "--file", backlog_file, "--autosave-interval", "30"]
            )
        assert result.exit_code != 0
        assert "No such option" in result.output
        run.assert_not_called()

    def test_protected_branches_flag_rejected_by_typer(self, backlog_file):
        with mock.patch("backlog.cli.subprocess.run") as run:
            result = runner.invoke(
                app,
                ["board", "--file", backlog_file, "--protected-branches", "main,stage"],
            )
        assert result.exit_code != 0
        assert "No such option" in result.output
        run.assert_not_called()

    def test_fallback_path_runs_without_guard_flags(self, backlog_file):
        """If backlog-server isn't on PATH, the script fallback must run and
        likewise carry no guard flags."""
        calls = []

        def side_effect(argv, *a, **kw):
            calls.append(argv)
            if calls and "backlog-server" in argv[0]:
                raise FileNotFoundError("backlog-server not found")
            return mock.DEFAULT

        with mock.patch("backlog.cli.subprocess.run", side_effect=side_effect):
            result = runner.invoke(app, ["board", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        assert len(calls) == 2  # first raised, fallback ran
        fallback_argv = calls[1]
        assert "--autosave-interval" not in fallback_argv
        assert "--protected-branches" not in fallback_argv
