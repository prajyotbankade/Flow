#!/usr/bin/env python3
"""
Backlog Board Server

Serves the visual Kanban board and provides a REST API for reading/writing backlog.json.

Usage:
    python backlog_server.py [--port PORT] [--file PATH]

Options:
    --port PORT     Port to serve on (default: 8089)
    --file PATH     Path to backlog.json (default: ./backlog.json)

API:
    GET  /api/backlog              Full backlog (for leaders and web board)
    GET  /api/backlog?agent=name   Filtered view (assigned to agent + unassigned ready items)
    PUT  /api/backlog              Full backlog write (version checked, atomic)
    PUT  /api/items/<id>           Single item update (version checked, atomic)
"""

import argparse
import json
import os
import subprocess
import tempfile
import threading
import webbrowser
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = Path(__file__).parent.resolve()
ASSETS_DIR = SCRIPT_DIR.parent / "assets"

# Lock to serialize writes — prevents race conditions between concurrent requests
write_lock = threading.Lock()

# Maximum allowed request body size (10 MB)
MAX_BODY_SIZE = 10 * 1024 * 1024


def get_git_user():
    """Read the local git user identity. Falls back gracefully if git is unavailable."""
    try:
        name = subprocess.check_output(
            ["git", "config", "user.name"], stderr=subprocess.DEVNULL
        ).decode().strip()
        email = subprocess.check_output(
            ["git", "config", "user.email"], stderr=subprocess.DEVNULL
        ).decode().strip()
        return {"name": name or "unknown", "email": email or ""}
    except Exception:
        return {"name": "unknown", "email": ""}


GIT_USER = get_git_user()


def read_backlog(filepath):
    """Read backlog.json, returning parsed data.
    Raises ValueError if the file exists but contains invalid JSON."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"version": 0, "config": {"scope": "project", "project_name": ""}, "items": []}
    except json.JSONDecodeError as e:
        raise ValueError(f"backlog.json is corrupted and cannot be read: {e}")


DEFAULT_STATUSES = [
    {"id": "backlog", "label": "Backlog", "color": "#94a3b8"},
    {"id": "refined", "label": "Refined", "color": "#f59e0b"},
    {"id": "ready", "label": "Ready", "color": "#10b981"},
    {"id": "in-progress", "label": "In Progress", "color": "#3b82f6"},
    {"id": "code-review", "label": "Code Review", "color": "#ec4899"},
    {"id": "done", "label": "Done", "color": "#8b5cf6"},
    {"id": "discarded", "label": "Discarded", "color": "#cbd5e1"},
]


def atomic_write(filepath, data):
    """Write data to file atomically — write to temp file then rename.
    This prevents corruption if the process crashes mid-write."""
    dir_path = os.path.dirname(os.path.abspath(filepath))
    with tempfile.NamedTemporaryFile(mode='w', dir=dir_path, suffix='.tmp',
                                     delete=False) as tmp:
        json.dump(data, tmp, indent=2)
        tmp_path = tmp.name
    os.replace(tmp_path, filepath)


def filter_for_agent(data, agent_name):
    """Return a filtered copy of the backlog for a worker agent.
    Includes: items assigned to this agent + unassigned ready items."""
    filtered = {
        "version": data.get("version", 0),
        "config": data.get("config", {}),
        "items": [
            item for item in data.get("items", [])
            if item.get("assigned_to") == agent_name
            or (item.get("status") == "ready" and not item.get("assigned_to"))
        ]
    }
    return filtered


def get_status_config(data):
    """Get the statuses config, falling back to defaults."""
    cfg = data.get("config", {}).get("statuses")
    if isinstance(cfg, list) and len(cfg) >= 2:
        return cfg
    return DEFAULT_STATUSES


def get_status_index(statuses, status_id):
    """Get the index of a status in the ordered list. Returns -1 if not found."""
    for i, s in enumerate(statuses):
        if s.get("id") == status_id:
            return i
    return -1


def validate_lane_transition(item, new_status, statuses):
    """Check if moving an item to new_status satisfies gate rules.

    Returns (ok: bool, error_message: str | None).

    Rules:
    - Moving backward (to an earlier lane) is always allowed
    - Moving forward: the target lane's `requires` must all appear in item's lane_history
    """
    if new_status == item.get("status"):
        return True, None  # No move

    # Discarding is always allowed from any lane — bypass gate rules
    if new_status == "discarded":
        return True, None

    old_idx = get_status_index(statuses, item.get("status", "backlog"))
    new_idx = get_status_index(statuses, new_status)

    # Moving backward is always allowed
    if new_idx <= old_idx:
        return True, None

    # Find the target status config
    target_config = None
    for s in statuses:
        if s.get("id") == new_status:
            target_config = s
            break

    if not target_config:
        return True, None  # Unknown status, let it through

    requires = target_config.get("requires", [])
    if not requires:
        return True, None  # No gate rules

    full_history = item.get("lane_history", [])
    gate_from = item.get("gate_from", 0)
    # Only consider history from the current journey (gate_from onward)
    # Handle both old format (string) and new format (dict with "lane" key)
    def extract_lane(entry):
        return entry.get("lane") if isinstance(entry, dict) else entry
    relevant_history = set(extract_lane(e) for e in full_history[gate_from:])
    # Also count the current status as part of history
    relevant_history.add(item.get("status", "backlog"))
    lane_history = relevant_history

    missing = [r for r in requires if r not in lane_history]
    if missing:
        # Get labels for the missing lanes
        label_map = {s.get("id"): s.get("label", s.get("id")) for s in statuses}
        missing_labels = [label_map.get(m, m) for m in missing]
        target_label = target_config.get("label", new_status)
        return False, f"Cannot move to '{target_label}' — requires passing through: {', '.join(missing_labels)}"

    return True, None


def enforce_lane_history(old_item, new_item, statuses):
    """Enforce lane_history integrity on status changes.
    - Always append current lane to history (preserves full audit trail)
    - Moving backward: set gate_from watermark so the rule engine ignores old history
    - Accepts _moved_by hint from client (e.g., 'board', 'user', agent name); defaults to 'agent'
    """
    old_status = old_item.get("status", "backlog")
    new_status = new_item.get("status", old_status)

    if old_status == new_status:
        return  # No move, nothing to do

    old_idx = get_status_index(statuses, old_status)
    new_idx = get_status_index(statuses, new_status)

    # Use the OLD item's lane_history as the base (server is authoritative)
    history = list(old_item.get("lane_history", []))
    if not isinstance(history, list):
        history = []

    # Determine who moved the item — accept _moved_by hint, default to 'agent'
    moved_by = new_item.pop("_moved_by", "agent")

    # Always append current lane to preserve audit trail — with who + when
    history.append({
        "lane": old_status,
        "at": datetime.now(timezone.utc).isoformat(),
        "by": moved_by
    })
    new_item["lane_history"] = history

    if new_idx < old_idx:
        # Moving backward — set watermark so gates only count from here onward
        new_item["gate_from"] = len(history)


class BacklogHandler(BaseHTTPRequestHandler):
    backlog_file = "backlog.json"

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._serve_html()
        elif parsed.path == "/api/backlog":
            params = parse_qs(parsed.query)
            agent = params.get("agent", [None])[0]
            self._serve_backlog(agent)
        elif parsed.path == "/api/user":
            self._serve_user()
        else:
            self.send_error(404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/backlog":
            self._save_backlog()
        elif parsed.path.startswith("/api/items/"):
            item_id = parsed.path.split("/api/items/")[1]
            self._update_item(item_id)
        else:
            self.send_error(404)

    def _serve_html(self):
        html_path = ASSETS_DIR / "backlog-board.html"
        if not html_path.exists():
            self.send_error(500, "backlog-board.html not found")
            return
        content = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(content))
        self.end_headers()
        self.wfile.write(content)

    def _serve_user(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(GIT_USER).encode())

    def _serve_backlog(self, agent=None):
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return
        if agent:
            data = filter_for_agent(data, agent)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _save_backlog(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            self._json_error(413, "Request body too large")
            return
        body = self.rfile.read(content_length)
        try:
            incoming = json.loads(body)
        except json.JSONDecodeError as e:
            self._json_error(400, f"Invalid JSON: {e}")
            return

        with write_lock:
            try:
                current = read_backlog(self.backlog_file)
            except ValueError as e:
                self._json_error(500, str(e))
                return

            incoming_version = incoming.get("version", 0)
            current_version = current.get("version", 0)

            # Version check — reject if the client's version is behind
            if incoming_version < current_version:
                self._json_error(409, f"Version conflict: you have v{incoming_version}, "
                                      f"current is v{current_version}. Re-read and retry.")
                return

            # Gate validation + lane_history enforcement for items whose status changed
            statuses = get_status_config(incoming)
            current_items = {i.get("id"): i for i in current.get("items", [])}
            first_status = statuses[0].get("id", "backlog") if statuses else "backlog"
            for item in incoming.get("items", []):
                old_item = current_items.get(item.get("id"))
                if old_item and item.get("status") != old_item.get("status"):
                    ok, err = validate_lane_transition(old_item, item.get("status"), statuses)
                    if not ok:
                        self._json_error(422, err)
                        return
                    # Server-side enforcement: append lane_history + set watermark
                    enforce_lane_history(old_item, item, statuses)
                elif not old_item and item.get("status", first_status) != first_status:
                    # New item — validate it can enter the chosen lane
                    virtual_old = {"status": first_status, "lane_history": [], "gate_from": 0}
                    ok, err = validate_lane_transition(virtual_old, item.get("status"), statuses)
                    if not ok:
                        self._json_error(422, err)
                        return
                else:
                    # Clean up _moved_by hint if present but no status change
                    item.pop("_moved_by", None)

            # Bump version and write atomically
            incoming["version"] = current_version + 1
            atomic_write(self.backlog_file, incoming)

        self._json_response(200, {"status": "ok", "version": incoming["version"]})

    def _update_item(self, item_id):
        """Update a single item by ID. Reads current file, patches the item, writes back."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            self._json_error(413, "Request body too large")
            return
        body = self.rfile.read(content_length)
        try:
            item_data = json.loads(body)
        except json.JSONDecodeError as e:
            self._json_error(400, f"Invalid JSON: {e}")
            return

        with write_lock:
            try:
                data = read_backlog(self.backlog_file)
            except ValueError as e:
                self._json_error(500, str(e))
                return

            client_version = item_data.pop("_version", None)
            current_version = data.get("version", 0)

            if client_version is not None and client_version < current_version:
                self._json_error(409, f"Version conflict: you have v{client_version}, "
                                      f"current is v{current_version}. Re-read and retry.")
                return

            # Find and update the item
            found = False
            for i, item in enumerate(data.get("items", [])):
                if item.get("id") == item_id:
                    # Gate validation if status is changing
                    new_status = item_data.get("status")
                    if new_status and new_status != item.get("status"):
                        statuses = get_status_config(data)
                        ok, err = validate_lane_transition(item, new_status, statuses)
                        if not ok:
                            self._json_error(422, err)
                            return
                        # Enforce lane_history append + watermark
                        enforce_lane_history(item, item_data, statuses)
                    else:
                        # Clean up _moved_by hint if present but no status change
                        item_data.pop("_moved_by", None)
                    data["items"][i].update(item_data)
                    found = True
                    break

            if not found:
                self._json_error(404, f"Item {item_id} not found")
                return

            data["version"] = current_version + 1
            atomic_write(self.backlog_file, data)

        self._json_response(200, {"status": "ok", "version": data["version"]})

    def _json_response(self, status, obj):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def _json_error(self, status, message):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

    def log_message(self, format, *args):
        if args and (str(args[0]).startswith("4") or str(args[0]).startswith("5")):
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description="Backlog Board Server")
    parser.add_argument("--port", type=int, default=8089, help="Port (default: 8089)")
    parser.add_argument("--file", type=str, default="backlog.json", help="Path to backlog.json")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open the browser")
    args = parser.parse_args()

    BacklogHandler.backlog_file = os.path.abspath(args.file)

    server = HTTPServer(("localhost", args.port), BacklogHandler)
    url = f"http://localhost:{args.port}"

    print(f"Backlog board: {url}")
    print(f"Reading from:  {BacklogHandler.backlog_file}")
    print(f"Git user:      {GIT_USER['name']} <{GIT_USER['email']}>")
    print("Press Ctrl+C to stop\n")

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
