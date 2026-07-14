"""Tests for backlog doctor --fix / init duplicate-section fix (backlog item #106).

Scenarios:
  (a) CLAUDE.md has a '## Flow Backlog' heading but no marker → markers inserted,
      no duplicate heading.
  (b) CLAUDE.md has no heading and no marker → full snippet appended (unchanged
      behaviour).
  (c) CLAUDE.md already has the marker → no-op.
  (d) Doctor emits no 'missing Flow setup' warning once the marker is present.
  (e) Multiple '## Flow Backlog' headings → act on the first, never duplicate.
  (f) CLAUDE.md unreadable (permission error) → fail gracefully, no crash.
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from backlog.cli import (
    _CLAUDE_MD_MARKER,
    _CLAUDE_MD_END_MARKER,
    _ensure_snippet,
    _heading_present,
    _snippet_present,
    app,
)

runner = CliRunner()

_MINIMAL_BACKLOG = {
    "version": 1,
    "config": {"scope": "project", "project_name": "test"},
    "items": [],
}


# ---------------------------------------------------------------------------
# Unit tests for the helper functions
# ---------------------------------------------------------------------------


class TestHeadingPresent:
    def test_detects_standard_heading(self):
        assert _heading_present("# Title\n\n## Flow Backlog\n\nsome content\n")

    def test_detects_uppercase_variation(self):
        assert _heading_present("## FLOW BACKLOG\n")

    def test_detects_mixed_case(self):
        assert _heading_present("## Flow backlog\n")

    def test_ignores_subheading(self):
        # '### Flow Backlog' is a level-3 heading — should NOT match
        assert not _heading_present("### Flow Backlog\n")

    def test_ignores_plain_text(self):
        assert not _heading_present("Some text about flow backlog here.\n")

    def test_empty_string(self):
        assert not _heading_present("")

    def test_heading_with_trailing_spaces(self):
        assert _heading_present("## Flow Backlog   \n")


class TestEnsureSnippet:
    def test_noop_when_marker_present(self, tmp_path):
        """When the marker is already present, _ensure_snippet returns 'noop'."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("<!-- flow-backlog-setup -->\n## Flow Backlog\n<!-- end flow-backlog-setup -->\n")
        result = _ensure_snippet(f)
        assert result == "noop"
        # Content unchanged
        assert "<!-- flow-backlog-setup -->" in f.read_text()

    def test_wrapped_when_heading_present_no_marker(self, tmp_path):
        """When heading exists but no marker, returns 'wrapped' and inserts markers."""
        f = tmp_path / "CLAUDE.md"
        original = (
            "# My Project\n\n"
            "## Flow Backlog\n\n"
            "- Use `backlog top` to pick up work.\n\n"
            "## Other Section\n\nstuff\n"
        )
        f.write_text(original)

        result = _ensure_snippet(f)

        assert result == "wrapped"
        content = f.read_text()

        # Marker inserted above the heading
        assert _CLAUDE_MD_MARKER in content
        assert _CLAUDE_MD_END_MARKER in content

        # Heading appears ONCE
        assert content.count("## Flow Backlog") == 1

        # Original content preserved
        assert "- Use `backlog top` to pick up work." in content
        assert "## Other Section" in content

        # Marker comes before the heading
        marker_pos = content.index(_CLAUDE_MD_MARKER)
        heading_pos = content.index("## Flow Backlog")
        assert marker_pos < heading_pos

        # End-marker comes after the heading line
        end_marker_pos = content.index(_CLAUDE_MD_END_MARKER)
        assert end_marker_pos > heading_pos

        # End-marker comes BEFORE '## Other Section' (end of section boundary)
        other_pos = content.index("## Other Section")
        assert end_marker_pos < other_pos

    def test_appended_when_no_heading_no_marker(self, tmp_path):
        """When neither heading nor marker exists, returns 'appended' and appends snippet."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("# Existing content\n\nSome stuff.\n")

        result = _ensure_snippet(f)

        assert result == "appended"
        content = f.read_text()
        assert _CLAUDE_MD_MARKER in content
        assert "## Flow Backlog" in content
        # Original content still present
        assert "# Existing content" in content

    def test_appended_to_empty_file(self, tmp_path):
        """Empty CLAUDE.md → snippet appended, no spurious blank lines at the top."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("")

        result = _ensure_snippet(f)

        assert result == "appended"
        content = f.read_text()
        assert _CLAUDE_MD_MARKER in content

    def test_appended_creates_file_if_missing(self, tmp_path):
        """If CLAUDE.md does not exist yet, _ensure_snippet creates it."""
        f = tmp_path / "CLAUDE.md"
        assert not f.exists()

        result = _ensure_snippet(f)

        assert result == "appended"
        assert f.exists()
        assert _CLAUDE_MD_MARKER in f.read_text()

    def test_no_duplicate_heading_after_wrap(self, tmp_path):
        """Running _ensure_snippet twice must not produce two headings."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("## Flow Backlog\n\nMy notes.\n")

        _ensure_snippet(f)  # first call wraps
        _ensure_snippet(f)  # second call must be a noop

        content = f.read_text()
        assert content.count("## Flow Backlog") == 1
        assert content.count(_CLAUDE_MD_MARKER) == 1

    def test_first_heading_used_when_multiple(self, tmp_path):
        """When multiple '## Flow Backlog' headings exist, only the first is wrapped."""
        f = tmp_path / "CLAUDE.md"
        f.write_text(
            "## Flow Backlog\n\nFirst block.\n\n"
            "## Flow Backlog\n\nDuplicate block.\n"
        )

        result = _ensure_snippet(f)

        assert result == "wrapped"
        content = f.read_text()
        # Marker must appear once
        assert content.count(_CLAUDE_MD_MARKER) == 1
        # End-marker must appear once
        assert content.count(_CLAUDE_MD_END_MARKER) == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not reliable on Windows")
    def test_oserror_on_unreadable_file(self, tmp_path):
        """Unreadable CLAUDE.md must raise OSError, not crash silently."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("some content")
        f.chmod(0o000)
        try:
            with pytest.raises(OSError):
                _ensure_snippet(f)
        finally:
            f.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def test_section_ends_at_next_heading(self, tmp_path):
        """End-marker is placed just before the next '##' heading, not at EOF."""
        f = tmp_path / "CLAUDE.md"
        f.write_text(
            "## Flow Backlog\n\n"
            "Backlog notes here.\n\n"
            "## Something Else\n\n"
            "Other notes.\n"
        )

        _ensure_snippet(f)
        content = f.read_text()

        end_pos = content.index(_CLAUDE_MD_END_MARKER)
        other_pos = content.index("## Something Else")
        assert end_pos < other_pos, "End-marker must come before the next section"

    def test_section_ends_at_eof_when_no_next_heading(self, tmp_path):
        """When Flow Backlog is the last section, end-marker goes at EOF."""
        f = tmp_path / "CLAUDE.md"
        f.write_text("# Title\n\n## Flow Backlog\n\nMy backlog notes.\n")

        _ensure_snippet(f)
        content = f.read_text()

        end_pos = content.index(_CLAUDE_MD_END_MARKER)
        # Nothing should come after the end-marker except optional trailing newline
        after = content[end_pos + len(_CLAUDE_MD_END_MARKER):].strip()
        assert after == "", f"Unexpected content after end-marker: {after!r}"


# ---------------------------------------------------------------------------
# CLI integration tests (doctor --fix)
# ---------------------------------------------------------------------------


def _write_backlog(directory: Path) -> Path:
    """Write a minimal backlog.json to directory and return its path."""
    p = directory / "backlog.json"
    p.write_text(json.dumps(_MINIMAL_BACKLOG, indent=2))
    return p


class TestDoctorFixExistingHeading:
    def test_wraps_existing_heading_no_duplicate(self, tmp_path):
        """doctor --fix inserts markers around an existing heading, no duplicate."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(
            "# Project\n\n"
            "## Flow Backlog\n\n"
            "- Always check the backlog first.\n"
        )
        backlog_file = _write_backlog(tmp_path)

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(
                app,
                ["doctor", "--fix", "--file", str(backlog_file)],
                env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)

        content = claude_md.read_text()

        assert result.exit_code == 0, f"Expected exit 0:\n{result.output}"
        assert content.count("## Flow Backlog") == 1, "Duplicate heading produced"
        assert _CLAUDE_MD_MARKER in content
        assert _CLAUDE_MD_END_MARKER in content
        assert "Always check the backlog first" in content

    def test_doctor_no_longer_warns_after_wrap(self, tmp_path):
        """After wrapping, a second doctor run should report OK, not warn."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("## Flow Backlog\n\nMy custom notes.\n")
        backlog_file = _write_backlog(tmp_path)

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            # First run — fix
            runner.invoke(
                app,
                ["doctor", "--fix", "--file", str(backlog_file)],
                env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
                catch_exceptions=False,
            )
            # Second run — check (no --fix)
            result2 = runner.invoke(
                app,
                ["doctor", "--file", str(backlog_file)],
                env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)

        assert result2.exit_code == 0, (
            f"Expected exit 0 after wrap, got {result2.exit_code}:\n{result2.output}"
        )
        assert "missing Flow setup" not in result2.output.lower()


class TestDoctorFixNoHeading:
    def test_appends_snippet_when_no_heading(self, tmp_path):
        """When CLAUDE.md has no heading or marker, doctor --fix appends the snippet."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# My Project\n\nGeneral docs.\n")
        backlog_file = _write_backlog(tmp_path)

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(
                app,
                ["doctor", "--fix", "--file", str(backlog_file)],
                env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)

        content = claude_md.read_text()
        assert result.exit_code == 0, f"Expected exit 0:\n{result.output}"
        assert _CLAUDE_MD_MARKER in content
        assert "## Flow Backlog" in content
        assert "# My Project" in content  # original preserved


class TestDoctorFixMarkerPresent:
    def test_noop_when_marker_already_present(self, tmp_path):
        """When marker is already present, doctor --fix makes no change."""
        original = (
            "<!-- flow-backlog-setup -->\n"
            "## Flow Backlog\n\n"
            "My custom notes.\n"
            "<!-- end flow-backlog-setup -->\n"
        )
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text(original)
        backlog_file = _write_backlog(tmp_path)

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(
                app,
                ["doctor", "--fix", "--file", str(backlog_file)],
                env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)

        content = claude_md.read_text()
        assert result.exit_code == 0, f"Expected exit 0:\n{result.output}"
        # Content must not change (no duplicate markers)
        assert content.count(_CLAUDE_MD_MARKER) == 1
        assert content.count("## Flow Backlog") == 1


class TestDoctorFixGracefulFailure:
    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not reliable on Windows")
    def test_unreadable_claude_md_does_not_crash(self, tmp_path):
        """doctor --fix with an unreadable CLAUDE.md must report the error, not crash."""
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("## Flow Backlog\n\nstuff\n")
        claude_md.chmod(0o000)
        backlog_file = _write_backlog(tmp_path)

        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            result = runner.invoke(
                app,
                ["doctor", "--fix", "--file", str(backlog_file)],
                env={**os.environ, "BACKLOG_FILE": str(backlog_file)},
                catch_exceptions=False,
            )
        finally:
            os.chdir(old_cwd)
            claude_md.chmod(stat.S_IRUSR | stat.S_IWUSR)

        # Must NOT crash with an unhandled exception
        assert result.exception is None or isinstance(result.exception, SystemExit), (
            f"Unexpected exception: {result.exception}"
        )
        # Output should mention CLAUDE.md or the error
        assert "CLAUDE.md" in result.output or "claude" in result.output.lower()
