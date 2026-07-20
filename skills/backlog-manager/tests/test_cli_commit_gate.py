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

from backlog.cli import app, _HOOK_MARKER, _HOOK_SCRIPT

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


# ── Branch guard (#116) ────────────────────────────────────────────────────────

def _setup_hook_repo(tmp_path: Path, branch: str = "main") -> Path:
    """Create a git repo on the given branch with the backlog hook installed."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    # Rename the default branch to match what was requested.
    _git(tmp_path, "checkout", "-b", branch)
    # Need an initial commit so the hook can find a HEAD.
    readme = tmp_path / "README"
    readme.write_text("init")
    _git(tmp_path, "add", "README")
    _git(tmp_path, "commit", "-m", "initial")
    # Install the hook.
    backlog = tmp_path / "backlog.json"
    runner.invoke(app, ["init", "--file", str(backlog)])
    return tmp_path


def _write_and_stage_backlog(repo: Path, integration_branch: str = "") -> None:
    """Write a valid backlog.json (optionally with integration_branch) and stage it."""
    data = dict(_VALID_BACKLOG)
    data = {
        "version": 1,
        "config": {
            "scope": "project",
            "project_name": "test",
        },
        "items": [],
    }
    if integration_branch:
        data["config"]["integration_branch"] = integration_branch
    backlog = repo / "backlog.json"
    backlog.write_text(json.dumps(data, indent=2))
    _git(repo, "add", "backlog.json")


def test_branch_guard_blocks_feature_branch(tmp_path):
    """backlog.json staged on a feature branch must be blocked by the hook."""
    repo = _setup_hook_repo(tmp_path, branch="main")
    # Create and switch to a feature branch.
    _git(repo, "checkout", "-b", "feature/my-task")
    _write_and_stage_backlog(repo)

    result = _git(repo, "commit", "-m", "should be blocked")

    assert result.returncode != 0, "Hook should have blocked this commit"
    assert "backlog.json" in result.stderr or "trunk" in result.stderr or "staged" in result.stderr


def test_branch_guard_allows_main(tmp_path):
    """backlog.json staged on main must be allowed by the hook."""
    repo = _setup_hook_repo(tmp_path, branch="main")
    _write_and_stage_backlog(repo)

    result = _git(repo, "commit", "-m", "valid on main")

    assert result.returncode == 0, f"Hook blocked commit on main: {result.stderr}"


def test_branch_guard_allows_master(tmp_path):
    """backlog.json staged on master must be allowed by the hook."""
    repo = _setup_hook_repo(tmp_path, branch="master")
    _write_and_stage_backlog(repo)

    result = _git(repo, "commit", "-m", "valid on master")

    assert result.returncode == 0, f"Hook blocked commit on master: {result.stderr}"


def test_branch_guard_allows_configured_integration_branch(tmp_path):
    """backlog.json staged on config.integration_branch must be allowed."""
    repo = _setup_hook_repo(tmp_path, branch="develop")
    # Write backlog.json with integration_branch set to develop and stage it.
    _write_and_stage_backlog(repo, integration_branch="develop")

    result = _git(repo, "commit", "-m", "valid on configured integration branch")

    assert result.returncode == 0, (
        f"Hook blocked commit on configured integration_branch 'develop': {result.stderr}"
    )


def test_branch_guard_allows_detached_head(tmp_path):
    """Detached HEAD must be allowed (fail-open — do not break rebases/CI)."""
    repo = _setup_hook_repo(tmp_path, branch="main")
    # Detach HEAD at current commit.
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--detach", head_sha)
    _write_and_stage_backlog(repo)

    result = _git(repo, "commit", "-m", "detached HEAD commit")

    assert result.returncode == 0, (
        f"Hook should fail-open on detached HEAD but got: {result.stderr}"
    )


def test_branch_guard_non_backlog_file_on_feature_branch_allowed(tmp_path):
    """A non-backlog file staged on a feature branch must not be blocked."""
    repo = _setup_hook_repo(tmp_path, branch="main")
    _git(repo, "checkout", "-b", "feature/code-change")
    # Stage a non-backlog file only.
    code_file = repo / "app.py"
    code_file.write_text("print('hello')")
    _git(repo, "add", "app.py")

    result = _git(repo, "commit", "-m", "code change, no backlog.json")

    assert result.returncode == 0, (
        f"Hook blocked a commit with no backlog.json staged: {result.stderr}"
    )


def test_branch_guard_error_message_is_actionable(tmp_path):
    """The branch-guard error must tell the user how to fix it (git restore --staged)."""
    repo = _setup_hook_repo(tmp_path, branch="main")
    _git(repo, "checkout", "-b", "feat/123-my-feature")
    _write_and_stage_backlog(repo)

    result = _git(repo, "commit", "-m", "should be blocked with guidance")

    assert result.returncode != 0
    # The error message must mention how to fix the problem.
    assert "git restore --staged backlog.json" in result.stderr


def test_branch_guard_existing_conflict_marker_check_still_runs(tmp_path):
    """Regression: conflict-marker check must still fire even with the new branch guard."""
    repo = _setup_hook_repo(tmp_path, branch="main")
    conflict_content = (
        "<<<<<<< HEAD\n"
        '{"version": 1, "config": {}, "items": []}\n'
        "=======\n"
        '{"version": 1, "config": {}, "items": []}\n'
        ">>>>>>> branch\n"
    )
    backlog = repo / "backlog.json"
    backlog.write_text(conflict_content)
    _git(repo, "add", "backlog.json")

    result = _git(repo, "commit", "-m", "should fail on conflict markers")

    assert result.returncode != 0
    assert "conflict markers" in result.stderr


def test_branch_guard_existing_json_validity_check_still_runs(tmp_path):
    """Regression: JSON-validity check must still fire even with the new branch guard."""
    repo = _setup_hook_repo(tmp_path, branch="main")
    backlog = repo / "backlog.json"
    backlog.write_text("{not valid json!!!")
    _git(repo, "add", "backlog.json")

    result = _git(repo, "commit", "-m", "should fail on invalid JSON")

    assert result.returncode != 0
    assert "not valid JSON" in result.stderr or "not valid json" in result.stderr.lower()


# ── install-hook command (#117) ────────────────────────────────────────────────

def _make_git_repo(tmp_path: Path) -> Path:
    """Create a bare git repo suitable for hook tests."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test User")
    return tmp_path


def test_install_hook_on_existing_project(tmp_path):
    """install-hook installs the hook even when backlog.json already exists."""
    repo = _make_git_repo(tmp_path)
    # Simulate a project that already has backlog.json (no hook yet).
    (repo / "backlog.json").write_text("{}")
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert not hook.exists()

    result = runner.invoke(app, ["install-hook", "--file", str(repo / "backlog.json")])

    assert result.exit_code == 0, result.output
    assert hook.exists()
    assert _HOOK_MARKER in hook.read_text()
    assert os.access(hook, os.X_OK)
    assert "installed" in result.output.lower() or "hook" in result.output.lower()


def test_install_hook_refreshes_outdated_flow_hook(tmp_path):
    """install-hook overwrites a stale flow-managed hook with the current script."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    # Write an old flow-managed hook that is missing the branch-guard.
    old_content = f"#!/bin/sh\n{_HOOK_MARKER}\n# old hook without branch guard\nexit 0\n"
    hook.write_text(old_content)
    hook.chmod(0o755)

    result = runner.invoke(app, ["install-hook", "--file", str(repo / "backlog.json")])

    assert result.exit_code == 0, result.output
    new_content = hook.read_text()
    # Must be the current script.
    assert new_content == _HOOK_SCRIPT
    # The branch-guard line from #116 must be present.
    assert "restore --staged backlog.json" in new_content
    assert "refreshed" in result.output.lower()


def test_install_hook_up_to_date_noop(tmp_path):
    """install-hook reports up-to-date and leaves the hook unchanged when current."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text(_HOOK_SCRIPT)
    hook.chmod(0o755)

    result = runner.invoke(app, ["install-hook", "--file", str(repo / "backlog.json")])

    assert result.exit_code == 0, result.output
    # Content must be unchanged.
    assert hook.read_text() == _HOOK_SCRIPT
    assert "up to date" in result.output.lower() or "up-to-date" in result.output.lower()


def test_install_hook_does_not_clobber_foreign_hook(tmp_path):
    """install-hook never overwrites a hook that was not installed by Flow."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    foreign_content = "#!/bin/sh\n# my custom hook\nexit 0\n"
    hook.write_text(foreign_content)
    hook.chmod(0o755)

    result = runner.invoke(app, ["install-hook", "--file", str(repo / "backlog.json")])

    assert result.exit_code == 0, result.output
    # Content must be unchanged.
    assert hook.read_text() == foreign_content, "Foreign hook was clobbered!"
    assert "foreign" in result.output.lower() or "already exists" in result.output.lower()


def test_install_hook_non_git_repo_skips_gracefully(tmp_path):
    """install-hook exits 0 and prints skip message when not in a git repo."""
    non_git = tmp_path / "not_a_repo"
    non_git.mkdir()

    result = runner.invoke(app, ["install-hook", "--file", str(non_git / "backlog.json")])

    assert result.exit_code == 0, result.output
    assert "not a git repo" in result.output.lower() or "skipped" in result.output.lower() or "not available" in result.output.lower()


# ── doctor hook state (#117) ───────────────────────────────────────────────────

def test_doctor_reports_hook_missing(tmp_path):
    """doctor reports hook as missing when no pre-commit hook is present."""
    repo = _make_git_repo(tmp_path)
    (repo / "backlog.json").write_text("{}")

    result = runner.invoke(app, ["doctor", "--file", str(repo / "backlog.json")])

    # Doctor exits 1 because of the missing hook issue.
    assert result.exit_code != 0 or "missing" in result.output.lower() or "not installed" in result.output.lower()
    assert "hook" in result.output.lower()


def test_doctor_reports_hook_outdated(tmp_path):
    """doctor reports hook as outdated when flow hook content is stale."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    old_content = f"#!/bin/sh\n{_HOOK_MARKER}\n# old version\nexit 0\n"
    hook.write_text(old_content)
    hook.chmod(0o755)
    (repo / "backlog.json").write_text("{}")

    result = runner.invoke(app, ["doctor", "--file", str(repo / "backlog.json")])

    assert "hook" in result.output.lower()
    assert "outdated" in result.output.lower() or "stale" in result.output.lower()


def test_doctor_reports_hook_foreign(tmp_path):
    """doctor reports foreign hook state without flagging it as an error."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text("#!/bin/sh\n# foreign\nexit 0\n")
    hook.chmod(0o755)
    (repo / "backlog.json").write_text("{}")

    result = runner.invoke(app, ["doctor", "--file", str(repo / "backlog.json")])

    # Foreign hook is reported as ok (not an error) — doctor only warns about missing/outdated.
    assert "hook" in result.output.lower()
    assert "foreign" in result.output.lower() or "not installed by flow" in result.output.lower()


def test_doctor_reports_hook_ok(tmp_path):
    """doctor reports hook as ok when the current flow hook is installed."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    hook.write_text(_HOOK_SCRIPT)
    hook.chmod(0o755)
    (repo / "backlog.json").write_text("{}")

    result = runner.invoke(app, ["doctor", "--file", str(repo / "backlog.json")])

    assert "hook" in result.output.lower()
    assert "up to date" in result.output.lower() or "up-to-date" in result.output.lower()


def test_doctor_fix_installs_missing_hook(tmp_path):
    """doctor --fix installs a missing hook."""
    repo = _make_git_repo(tmp_path)
    (repo / "backlog.json").write_text("{}")
    hook = repo / ".git" / "hooks" / "pre-commit"
    assert not hook.exists()

    result = runner.invoke(app, ["doctor", "--fix", "--file", str(repo / "backlog.json")])

    assert result.exit_code == 0, result.output
    assert hook.exists()
    assert _HOOK_MARKER in hook.read_text()
    assert os.access(hook, os.X_OK)


def test_doctor_fix_refreshes_outdated_hook(tmp_path):
    """doctor --fix refreshes a stale flow-managed hook to the current script."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    old_content = f"#!/bin/sh\n{_HOOK_MARKER}\n# old hook\nexit 0\n"
    hook.write_text(old_content)
    hook.chmod(0o755)
    (repo / "backlog.json").write_text("{}")

    result = runner.invoke(app, ["doctor", "--fix", "--file", str(repo / "backlog.json")])

    assert result.exit_code == 0, result.output
    assert hook.read_text() == _HOOK_SCRIPT
    assert "restore --staged backlog.json" in hook.read_text()


def test_doctor_fix_does_not_clobber_foreign_hook(tmp_path):
    """doctor --fix never overwrites a foreign (non-flow) pre-commit hook."""
    repo = _make_git_repo(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook = hooks_dir / "pre-commit"
    foreign_content = "#!/bin/sh\n# my hook\nexit 0\n"
    hook.write_text(foreign_content)
    hook.chmod(0o755)
    (repo / "backlog.json").write_text("{}")

    result = runner.invoke(app, ["doctor", "--fix", "--file", str(repo / "backlog.json")])

    # Foreign hook must remain untouched.
    assert hook.read_text() == foreign_content, "doctor --fix clobbered a foreign hook!"
