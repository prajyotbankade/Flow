"""Handler-level server tests for external_refs validation (#113).

Constructs BacklogHandler via __new__ (no socket) and stubs rfile/headers/wfile
to drive _save_backlog and _update_item directly — the same pattern used in
TestBoardJournals (test_recovery_journal.py:147-151).

Covers:
- POST / (_save_backlog): 422 on ref missing system; 422 on ref missing id;
  200 ok when url absent but system+id present
- PUT /item/<id> (_update_item): same three cases
"""

import io
import json
from pathlib import Path
from unittest import mock

import pytest

import backlog.server as server
from backlog.server import atomic_write


# ── helpers ───────────────────────────────────────────────────────────────────

def _write_backlog(path: Path, version: int = 1, items=None):
    data = {
        "version": version,
        "config": {"scope": "project", "project_name": "test"},
        "items": items or [],
    }
    path.write_text(json.dumps(data))
    return data


def _make_item(item_id="abc12345", external_refs=None):
    item = {
        "id": item_id,
        "title": "Test item",
        "status": "backlog",
        "priority": "medium",
        "priority_weight": 5,
        "complexity": "medium",
        "category": None,
        "tags": [],
        "description": "",
        "assigned_to": None,
        "links": [],
        "threads": [],
        "lane_history": [],
        "execution_history": [],
        "gate_from": 0,
        "reopen_count": 0,
        "skip_count": 0,
        "created_at": "2024-01-01T00:00:00+00:00",
        "updated_at": "2024-01-01T00:00:00+00:00",
    }
    if external_refs is not None:
        item["external_refs"] = external_refs
    return item


class _FakeHeaders(dict):
    """Minimal headers dict that supports .get()."""
    def get(self, key, default=None):
        return super().get(key, default)


def _make_handler(backlog_file):
    """Build a BacklogHandler without a real socket, mirroring TestBoardJournals."""
    h = server.BacklogHandler.__new__(server.BacklogHandler)
    h.backlog_file = str(backlog_file)
    # Stub wfile so _json_error / _json_response don't crash
    h.wfile = io.BytesIO()
    return h


def _stub_request(handler, body_dict):
    """Inject a JSON body + matching Content-Length header into handler."""
    body_bytes = json.dumps(body_dict).encode()
    handler.rfile = io.BytesIO(body_bytes)
    handler.headers = _FakeHeaders({"Content-Length": str(len(body_bytes))})


def _capture_response(handler):
    """After a handler call, parse the response written to wfile."""
    handler.wfile.seek(0)
    raw = handler.wfile.read()
    if not raw:
        return None
    return json.loads(raw)


# We need to stub send_response / send_header / end_headers so the handler
# doesn't try to write HTTP framing to a BytesIO that doesn't expect it.
def _noop(*args, **kwargs):
    pass


def _patch_http_framing(handler):
    handler.send_response = _noop
    handler.send_header = _noop
    handler.end_headers = _noop


# ── _save_backlog (POST /) ────────────────────────────────────────────────────

class TestSaveBacklogExternalRefsValidation:
    def _call_save(self, tmp_path, items):
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, version=1, items=items)
        h = _make_handler(bf)
        _patch_http_framing(h)

        body = {
            "version": 1,
            "config": {"scope": "project", "project_name": "test"},
            "items": items,
        }
        _stub_request(h, body)

        # suppress policy engine background trigger
        with mock.patch.object(h, "_trigger_policy_engine_background"):
            h._save_backlog()

        h.wfile.seek(0)
        return json.loads(h.wfile.read())

    def _capture_status(self, tmp_path, items):
        """Return the HTTP status code that was passed to send_response."""
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, version=1, items=items)
        h = _make_handler(bf)

        status_holder = {}

        def capture_send_response(status):
            status_holder["status"] = status

        h.send_response = capture_send_response
        h.send_header = _noop
        h.end_headers = _noop

        body = {
            "version": 1,
            "config": {"scope": "project", "project_name": "test"},
            "items": items,
        }
        _stub_request(h, body)

        with mock.patch.object(h, "_trigger_policy_engine_background"):
            h._save_backlog()

        return status_holder.get("status")

    def test_save_422_on_ref_missing_system(self, tmp_path):
        items = [_make_item(external_refs=[{"id": "PROJ-1"}])]
        status = self._capture_status(tmp_path, items)
        assert status == 422, f"expected 422, got {status}"

    def test_save_422_on_ref_missing_id(self, tmp_path):
        items = [_make_item(external_refs=[{"system": "jira"}])]
        status = self._capture_status(tmp_path, items)
        assert status == 422, f"expected 422, got {status}"

    def test_save_ok_when_url_absent(self, tmp_path):
        items = [_make_item(external_refs=[{"system": "jira", "id": "PROJ-1"}])]
        status = self._capture_status(tmp_path, items)
        assert status == 200, f"expected 200, got {status}"

    def test_save_ok_when_external_refs_absent(self, tmp_path):
        """Items with no external_refs field are accepted (backward compat)."""
        items = [_make_item()]
        status = self._capture_status(tmp_path, items)
        assert status == 200, f"expected 200, got {status}"

    def test_save_ok_when_external_refs_empty(self, tmp_path):
        items = [_make_item(external_refs=[])]
        status = self._capture_status(tmp_path, items)
        assert status == 200, f"expected 200, got {status}"

    def test_save_422_error_body_contains_message(self, tmp_path):
        """The 422 response body must have an 'error' key with meaningful text."""
        bf = tmp_path / "backlog.json"
        items = [_make_item(external_refs=[{"id": "PROJ-1"}])]
        _write_backlog(bf, version=1, items=items)
        h = _make_handler(bf)
        _patch_http_framing(h)

        body = {
            "version": 1,
            "config": {"scope": "project", "project_name": "test"},
            "items": items,
        }
        _stub_request(h, body)

        with mock.patch.object(h, "_trigger_policy_engine_background"):
            h._save_backlog()

        h.wfile.seek(0)
        resp = json.loads(h.wfile.read())
        assert "error" in resp
        assert "system" in resp["error"] or "id" in resp["error"] or "ref" in resp["error"].lower()


# ── _update_item (PUT /item/<id>) ─────────────────────────────────────────────

class TestUpdateItemExternalRefsValidation:
    ITEM_ID = "abc12345"

    def _call_update(self, tmp_path, item_data):
        item = _make_item(item_id=self.ITEM_ID)
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, version=1, items=[item])
        h = _make_handler(bf)

        status_holder = {}

        def capture_send_response(status):
            status_holder["status"] = status

        h.send_response = capture_send_response
        h.send_header = _noop
        h.end_headers = _noop

        _stub_request(h, item_data)

        with mock.patch.object(h, "_trigger_policy_engine_background"):
            h._update_item(self.ITEM_ID)

        return status_holder.get("status")

    def test_update_422_on_ref_missing_system(self, tmp_path):
        item_data = {"external_refs": [{"id": "PROJ-1"}]}
        status = self._call_update(tmp_path, item_data)
        assert status == 422, f"expected 422, got {status}"

    def test_update_422_on_ref_missing_id(self, tmp_path):
        item_data = {"external_refs": [{"system": "jira"}]}
        status = self._call_update(tmp_path, item_data)
        assert status == 422, f"expected 422, got {status}"

    def test_update_ok_when_url_absent(self, tmp_path):
        item_data = {"external_refs": [{"system": "jira", "id": "PROJ-1"}]}
        status = self._call_update(tmp_path, item_data)
        assert status == 200, f"expected 200, got {status}"

    def test_update_ok_when_external_refs_absent(self, tmp_path):
        """No external_refs key in update payload — accepted."""
        item_data = {"title": "Updated title"}
        status = self._call_update(tmp_path, item_data)
        assert status == 200, f"expected 200, got {status}"

    def test_update_ok_when_external_refs_empty(self, tmp_path):
        item_data = {"external_refs": []}
        status = self._call_update(tmp_path, item_data)
        assert status == 200, f"expected 200, got {status}"

    def test_update_422_error_body_contains_message(self, tmp_path):
        """The 422 response body must have an 'error' key with meaningful text."""
        item = _make_item(item_id=self.ITEM_ID)
        bf = tmp_path / "backlog.json"
        _write_backlog(bf, version=1, items=[item])
        h = _make_handler(bf)
        _patch_http_framing(h)

        item_data = {"external_refs": [{"system": "jira"}]}  # missing id
        _stub_request(h, item_data)

        with mock.patch.object(h, "_trigger_policy_engine_background"):
            h._update_item(self.ITEM_ID)

        h.wfile.seek(0)
        resp = json.loads(h.wfile.read())
        assert "error" in resp
        assert "id" in resp["error"] or "ref" in resp["error"].lower()
