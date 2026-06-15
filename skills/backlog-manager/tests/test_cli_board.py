"""CLI tests for the `board` command: verify autosave guard flags are forwarded
to the backlog-server subprocess (item #100)."""

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


class TestBoardForwardsGuardFlags:
    def test_default_forwards_interval_300_and_protected_branches(self, backlog_file):
        with mock.patch("backlog.cli.subprocess.run") as run:
            result = runner.invoke(app, ["board", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        argv = _argv_of(run.call_args)
        assert "--autosave-interval" in argv
        assert argv[argv.index("--autosave-interval") + 1] == "300"
        assert "--protected-branches" in argv

    def test_protected_branches_forwarded_verbatim(self, backlog_file):
        with mock.patch("backlog.cli.subprocess.run") as run:
            result = runner.invoke(
                app,
                ["board", "--file", backlog_file, "--protected-branches", "main,stage"],
            )
        assert result.exit_code == 0, result.output
        argv = _argv_of(run.call_args)
        assert argv[argv.index("--protected-branches") + 1] == "main,stage"

    def test_autosave_interval_zero_forwarded_as_string(self, backlog_file):
        with mock.patch("backlog.cli.subprocess.run") as run:
            result = runner.invoke(
                app, ["board", "--file", backlog_file, "--autosave-interval", "0"]
            )
        assert result.exit_code == 0, result.output
        argv = _argv_of(run.call_args)
        assert argv[argv.index("--autosave-interval") + 1] == "0"

    def test_empty_protected_branches_forwarded(self, backlog_file):
        """Empty string is forwarded; server falls back to main,master."""
        with mock.patch("backlog.cli.subprocess.run") as run:
            result = runner.invoke(app, ["board", "--file", backlog_file])
        assert result.exit_code == 0, result.output
        argv = _argv_of(run.call_args)
        assert "--protected-branches" in argv
        assert argv[argv.index("--protected-branches") + 1] == ""

    def test_fallback_path_also_forwards_flags(self, backlog_file):
        """If backlog-server isn't on PATH, the script fallback must also get the flags."""
        calls = []

        def side_effect(argv, *a, **kw):
            calls.append(argv)
            if calls and "backlog-server" in argv[0]:
                raise FileNotFoundError("backlog-server not found")
            return mock.DEFAULT

        with mock.patch("backlog.cli.subprocess.run", side_effect=side_effect) as run:
            result = runner.invoke(
                app,
                ["board", "--file", backlog_file, "--protected-branches", "main,stage",
                 "--autosave-interval", "0"],
            )
        assert result.exit_code == 0, result.output
        assert len(calls) == 2  # first raised, fallback ran
        fallback_argv = calls[1]
        assert "--autosave-interval" in fallback_argv
        assert fallback_argv[fallback_argv.index("--autosave-interval") + 1] == "0"
        assert "--protected-branches" in fallback_argv
        assert fallback_argv[fallback_argv.index("--protected-branches") + 1] == "main,stage"
