"""Tests for the deliberate `backlog commit` command (item #96).

The command commits ONLY backlog.json into git history when run, replacing the
auto-commit removed in #94. It must never sweep up other staged/unstaged files,
must no-op when clean, and must fail gracefully outside a git repo.
"""

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app

runner = CliRunner()


_EMPTY_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )


@pytest.fixture
def git_repo(tmp_path):
    """A git repo with backlog.json already committed once (so HEAD exists)."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
    _git(tmp_path, "add", "backlog.json")
    _git(tmp_path, "commit", "-m", "initial")
    return tmp_path


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _edit_backlog(repo: Path) -> None:
    backlog = repo / "backlog.json"
    data = json.loads(backlog.read_text())
    data["items"].append({"id": "x1", "title": "edited", "status": "backlog"})
    backlog.write_text(json.dumps(data, indent=2))


def test_commit_creates_one_commit_with_only_backlog(git_repo):
    _edit_backlog(git_repo)
    before = _head(git_repo)

    result = runner.invoke(app, ["commit", "--file", str(git_repo / "backlog.json")])
    assert result.exit_code == 0, result.output

    after = _head(git_repo)
    assert after != before

    # Exactly one new commit.
    count = _git(git_repo, "rev-list", "--count", f"{before}..{after}").stdout.strip()
    assert count == "1"

    # That commit contains ONLY backlog.json.
    names = _git(git_repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert names == ["backlog.json"]

    # Default message.
    subject = _git(git_repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "chore(backlog): checkpoint"


def test_commit_custom_message(git_repo):
    _edit_backlog(git_repo)
    result = runner.invoke(
        app, ["commit", "-m", "custom msg", "--file", str(git_repo / "backlog.json")]
    )
    assert result.exit_code == 0, result.output
    subject = _git(git_repo, "log", "-1", "--format=%s").stdout.strip()
    assert subject == "custom msg"


def test_commit_leaves_other_staged_file_untouched(git_repo):
    _edit_backlog(git_repo)
    # Stage an unrelated second file.
    other = git_repo / "other.txt"
    other.write_text("unrelated change")
    _git(git_repo, "add", "other.txt")

    result = runner.invoke(app, ["commit", "--file", str(git_repo / "backlog.json")])
    assert result.exit_code == 0, result.output

    # The committed change is only backlog.json.
    names = _git(git_repo, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert names == ["backlog.json"]

    # other.txt is still staged but uncommitted.
    staged = _git(git_repo, "diff", "--cached", "--name-only").stdout.split()
    assert "other.txt" in staged


def test_commit_noop_when_unchanged(git_repo):
    before = _head(git_repo)
    result = runner.invoke(app, ["commit", "--file", str(git_repo / "backlog.json")])
    assert result.exit_code == 0, result.output
    assert "nothing to commit" in result.output
    assert _head(git_repo) == before  # No new commit.


def test_commit_never_committed_file(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))
    # Never committed before; HEAD doesn't exist yet either, but git allows
    # a first commit. (Note: --quiet diff against non-existent HEAD still
    # detects staged content.)

    result = runner.invoke(app, ["commit", "--file", str(backlog)])
    assert result.exit_code == 0, result.output

    names = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout.split()
    assert names == ["backlog.json"]


def test_commit_not_a_git_repo(tmp_path):
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps(_EMPTY_BACKLOG, indent=2))

    result = runner.invoke(app, ["commit", "--file", str(backlog)])
    assert result.exit_code != 0
    assert "git repository" in result.output.lower()
    # No traceback leaked.
    assert "Traceback" not in result.output
