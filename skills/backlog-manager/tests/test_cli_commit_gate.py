"""Tests for the commit-time integrity gate added in backlog item #102.

The gate runs BEFORE git-add so a corrupt backlog.json is never staged or
committed. Covers:
- valid JSON commits successfully (happy path unchanged)
- invalid JSON aborts non-zero AND git HEAD is unchanged
- conflict markers abort non-zero
- false-positive guard: description containing "=======" mid-line still commits
- pre-commit hook blocks a staged bad backlog.json
- backlog init does NOT overwrite an existing pre-commit hook
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import app, _HOOK_MARKER

runner = CliRunner()


_VALID_BACKLOG = {
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
    """Git repo with backlog.json already committed once (HEAD exists)."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    backlog = tmp_path / "backlog.json"
    backlog.write_text(json.dumps(_VALID_BACKLOG, indent=2))
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


# ── Core validation: valid file ────────────────────────────────────────────────

def test_valid_json_commits_successfully(git_repo):
    """Happy path: valid JSON passes gate and produces a new commit."""
    _edit_backlog(git_repo)
    before = _head(git_repo)

    result = runner.invoke(app, ["commit", "--file", str(git_repo / "backlog.json")])

    assert result.exit_code == 0, result.output
    assert _head(git_repo) != before


# ── Core validation: invalid JSON ─────────────────────────────────────────────

def test_invalid_json_aborts_non_zero(git_repo):
    """Invalid JSON must exit non-zero and print a clear error."""
    backlog = git_repo / "backlog.json"
    backlog.write_text("{broken json!!!")  # not valid JSON

    result = runner.invoke(app, ["commit", "--file", str(backlog)])

    assert result.exit_code != 0
    output = result.output.lower()
    assert "not valid json" in output or "json" in output


def test_invalid_json_head_unchanged(git_repo):
    """Invalid JSON must not produce a new commit."""
    before = _head(git_repo)
    backlog = git_repo / "backlog.json"
    backlog.write_text("{broken json!!!")

    runner.invoke(app, ["commit", "--file", str(backlog)])

    assert _head(git_repo) == before


def test_invalid_json_nothing_staged(git_repo):
    """Invalid JSON must not stage any changes (git add must not run)."""
    backlog = git_repo / "backlog.json"
    backlog.write_text("{broken json!!!")

    runner.invoke(app, ["commit", "--file", str(backlog)])

    staged = _git(git_repo, "diff", "--cached", "--name-only").stdout.strip()
    assert "backlog.json" not in staged


# ── Core validation: conflict markers ─────────────────────────────────────────

# A real conflicted backlog.json starts with "<<<<<<< HEAD" and is therefore
# invalid JSON too.  The conflict-marker check MUST fire first and produce a
# diagnostic that names "conflict markers" and includes the offending line
# numbers — not a generic "not valid json" error.

def _normalize(text: str) -> str:
    """Collapse newlines to spaces for substring assertions against Rich-wrapped output."""
    return " ".join(text.split())


def test_conflict_markers_abort_non_zero(git_repo):
    """A file with git conflict markers must be rejected non-zero."""
    backlog = git_repo / "backlog.json"
    conflict_content = (
        "<<<<<<< HEAD\n"
        '{"version": 1, "config": {}, "items": []}\n'
        "=======\n"
        '{"version": 1, "config": {}, "items": [{"id": "x"}]}\n'
        ">>>>>>> feature-branch\n"
    )
    backlog.write_text(conflict_content)

    result = runner.invoke(app, ["commit", "--file", str(backlog)])

    assert result.exit_code != 0
    output = _normalize(result.output).lower()
    # Must name conflict markers specifically — not a generic JSON error.
    assert "conflict markers" in output
    # Must include at least one line number.
    assert any(char.isdigit() for char in output)
    # Must NOT fall through to the generic JSON error (marker check fires first).
    assert "not valid json" not in output


def test_conflict_markers_error_names_line_numbers(git_repo):
    """The conflict-marker error must include the exact offending line numbers."""
    backlog = git_repo / "backlog.json"
    # Markers are on lines 1, 3, and 5 of this content.
    conflict_content = (
        "<<<<<<< HEAD\n"                                    # line 1
        '{"version": 1, "config": {}, "items": []}\n'      # line 2
        "=======\n"                                         # line 3
        '{"version": 1, "config": {}, "items": []}\n'      # line 4
        ">>>>>>> branch\n"                                  # line 5
    )
    backlog.write_text(conflict_content)

    result = runner.invoke(app, ["commit", "--file", str(backlog)])

    assert result.exit_code != 0
    output = _normalize(result.output)
    # The output must contain "conflict markers" (case-insensitive).
    assert "conflict markers" in output.lower()
    # Lines 1, 3, and 5 are the markers — at least one must appear in the output.
    assert "1" in output or "3" in output or "5" in output


def test_conflict_markers_head_unchanged(git_repo):
    """Conflict markers must not produce a new commit."""
    before = _head(git_repo)
    backlog = git_repo / "backlog.json"
    conflict_content = (
        "<<<<<<< HEAD\n"
        '{"version": 1, "config": {}, "items": []}\n'
        "=======\n"
        '{"version": 1, "config": {}, "items": []}\n'
        ">>>>>>> branch\n"
    )
    backlog.write_text(conflict_content)

    runner.invoke(app, ["commit", "--file", str(backlog)])

    assert _head(git_repo) == before


def test_conflict_marker_check_precedes_json_check(git_repo):
    """Prove the marker check fires BEFORE the JSON check.

    A real conflicted file is also invalid JSON.  If the JSON check ran first
    the error would say 'not valid json'; the marker check must intercept it
    and emit the conflict-marker diagnostic instead.
    """
    backlog = git_repo / "backlog.json"
    # This content is BOTH conflicted AND invalid JSON — only the marker
    # diagnostic (with "conflict marker" and a line number) is acceptable.
    conflict_and_invalid = (
        "<<<<<<< HEAD\n"
        "not json at all\n"
        "=======\n"
        "also not json\n"
        ">>>>>>> branch\n"
    )
    backlog.write_text(conflict_and_invalid)

    result = runner.invoke(app, ["commit", "--file", str(backlog)])

    assert result.exit_code != 0
    output = _normalize(result.output).lower()
    # Must be the marker diagnostic, not the JSON one.
    assert "conflict markers" in output
    assert "not valid json" not in output


# ── False-positive guard ───────────────────────────────────────────────────────

def test_equals_mid_line_not_a_conflict_marker(git_repo):
    """A description containing '=======' mid-line must NOT be flagged."""
    backlog = git_repo / "backlog.json"
    data = {
        "version": 1,
        "config": {"scope": "project", "project_name": "test"},
        "items": [
            {
                "id": "x1",
                "title": "Use separator =======  for display",
                "status": "backlog",
                "description": "render ======= as a horizontal rule",
            }
        ],
    }
    backlog.write_text(json.dumps(data, indent=2))
    before = _head(git_repo)

    result = runner.invoke(app, ["commit", "--file", str(backlog)])

    assert result.exit_code == 0, result.output
    assert _head(git_repo) != before


def test_less_than_marker_mid_line_not_flagged(git_repo):
    """'<<<<<<< ' appearing after other characters must not be flagged."""
    backlog = git_repo / "backlog.json"
    data = {
        "version": 1,
        "config": {"scope": "project", "project_name": "test"},
        "items": [
            {
                "id": "x2",
                "title": "some item <<<<<<< not a marker",
                "status": "backlog",
            }
        ],
    }
    backlog.write_text(json.dumps(data, indent=2))

    result = runner.invoke(app, ["commit", "--file", str(backlog)])

    assert result.exit_code == 0, result.output


# ── Pre-commit hook ────────────────────────────────────────────────────────────

def test_init_installs_precommit_hook(tmp_path):
    """backlog init should install a pre-commit hook in .git/hooks/."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    result = runner.invoke(app, ["init", "--file", str(tmp_path / "backlog.json")])

    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook.exists(), f"pre-commit hook not created; init output:\n{result.output}"
    content = hook.read_text()
    assert _HOOK_MARKER in content
    # Must be executable.
    assert os.access(hook, os.X_OK)


def test_init_does_not_overwrite_existing_hook(tmp_path):
    """backlog init must NEVER clobber a pre-existing pre-commit hook."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    original_content = "#!/bin/sh\n# my existing hook\nexit 0\n"
    hook.write_text(original_content)
    hook.chmod(0o755)

    result = runner.invoke(app, ["init", "--file", str(tmp_path / "backlog.json")])

    # Content must be unchanged.
    assert hook.read_text() == original_content
    # init must have printed guidance instead.
    assert "already exists" in result.output or "manually" in result.output.lower()


def test_precommit_hook_blocks_invalid_json(tmp_path):
    """The installed hook must block a commit when staged backlog.json is invalid JSON."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    # Install the hook via init.
    backlog = tmp_path / "backlog.json"
    runner.invoke(app, ["init", "--file", str(backlog)])

    # Write an invalid JSON file and stage it.
    backlog.write_text("{broken!!!")
    _git(tmp_path, "add", "backlog.json")

    # Attempt a commit — the hook should reject it.
    result = _git(tmp_path, "commit", "-m", "bad commit")
    assert result.returncode != 0


def test_precommit_hook_blocks_conflict_markers(tmp_path):
    """The installed hook must block a commit when staged backlog.json has conflict markers."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    # Install the hook via init.
    backlog = tmp_path / "backlog.json"
    runner.invoke(app, ["init", "--file", str(backlog)])

    # Write a file with conflict markers and stage it.
    conflict_content = (
        "<<<<<<< HEAD\n"
        '{"version": 1, "config": {}, "items": []}\n'
        "=======\n"
        '{"version": 1, "config": {}, "items": []}\n'
        ">>>>>>> branch\n"
    )
    backlog.write_text(conflict_content)
    _git(tmp_path, "add", "backlog.json")

    result = _git(tmp_path, "commit", "-m", "bad commit")
    assert result.returncode != 0


def test_precommit_hook_allows_valid_json(tmp_path):
    """The installed hook must allow a commit when staged backlog.json is valid."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    # Install the hook via init.
    backlog = tmp_path / "backlog.json"
    runner.invoke(app, ["init", "--file", str(backlog)])

    # The file was created by init; stage it.
    _git(tmp_path, "add", "backlog.json")
    result = _git(tmp_path, "commit", "-m", "valid commit")
    assert result.returncode == 0, result.stderr


# ── Worktree hook-install fix (#104) ──────────────────────────────────────────

@pytest.fixture
def git_repo_with_worktree(tmp_path):
    """Main repo with one initial commit plus a linked worktree."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init")
    _git(main, "config", "user.email", "test@example.com")
    _git(main, "config", "user.name", "Test User")
    # Need at least one commit so 'git worktree add' can create a branch.
    (main / "README").write_text("init")
    _git(main, "add", "README")
    _git(main, "commit", "-m", "initial")

    worktree = tmp_path / "wt"
    _git(main, "worktree", "add", str(worktree), "-b", "wt-branch")
    return main, worktree


def test_worktree_installs_hook(git_repo_with_worktree):
    """(a) backlog init from inside a linked worktree must install the hook."""
    main, worktree = git_repo_with_worktree

    result = runner.invoke(app, ["init", "--file", str(worktree / "backlog.json")])

    # The hook must be written into the *common* hooks dir (the main repo's
    # .git/hooks), which is what 'git rev-parse --git-path hooks' resolves to
    # for a linked worktree.
    hook = main / ".git" / "hooks" / "pre-commit"
    assert hook.exists(), (
        f"pre-commit hook not created in worktree scenario; "
        f"init output:\n{result.output}"
    )
    assert _HOOK_MARKER in hook.read_text()
    assert os.access(hook, os.X_OK)


def test_plain_repo_still_installs_hook(tmp_path):
    """(b) Regression: a non-worktree repo still installs at .git/hooks/pre-commit."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")

    result = runner.invoke(app, ["init", "--file", str(tmp_path / "backlog.json")])

    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    assert hook.exists(), f"pre-commit hook not created; init output:\n{result.output}"
    assert _HOOK_MARKER in hook.read_text()
    assert os.access(hook, os.X_OK)


def test_non_git_dir_silent_skip(tmp_path):
    """(c) Non-git directory: no hook created, no exception raised."""
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()

    result = runner.invoke(app, ["init", "--file", str(non_git / "backlog.json")])

    # Must not raise and must not create any pre-commit hook anywhere.
    assert result.exit_code == 0, result.output
    assert not (non_git / ".git").exists()
    # Confirm no hook path was accidentally created.
    hooks_candidates = list(non_git.rglob("pre-commit"))
    assert hooks_candidates == []


def test_worktree_foreign_hook_not_clobbered(git_repo_with_worktree):
    """(d) Foreign hook in the common hooks dir must never be clobbered."""
    main, worktree = git_repo_with_worktree

    # Pre-install a foreign hook in the main repo's hooks dir.
    hooks_dir = main / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    original_content = "#!/bin/sh\n# foreign hook\nexit 0\n"
    hook.write_text(original_content)
    hook.chmod(0o755)

    # Run init from the worktree directory.
    result = runner.invoke(app, ["init", "--file", str(worktree / "backlog.json")])

    # Content must be unchanged.
    assert hook.read_text() == original_content, "Foreign hook was clobbered!"
    # Init must have printed guidance.
    assert "already exists" in result.output or "manually" in result.output.lower()
