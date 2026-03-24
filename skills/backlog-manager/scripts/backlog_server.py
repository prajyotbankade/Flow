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
    GET  /api/scores               Computed scores for all items (Work Intelligence Engine)
    GET  /api/agents               Agent profiles with current load
    PUT  /api/backlog              Full backlog write (version checked, atomic, returns _events)
    PUT  /api/items/<id>           Single item update (version checked, atomic, returns _events)
"""

import argparse
import json
import os
import subprocess
import tempfile
import threading
import webbrowser
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
import math

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
        # Auto-increment reopen_count when moving backward from done
        if old_status == "done":
            new_item["reopen_count"] = new_item.get("reopen_count", 0) + 1


# ── Scoring defaults ──────────────────────────────────────────────────────────

DEFAULT_SCORING = {
    "priority_weight_factor": 1.0,
    "unblock_weight": 2.0,
    "freshness_boost_days": 3,
    "freshness_decay_days": 14,
    "freshness_decay_factor": 0.5,
    "complexity_bonus": {"low": 1.5, "medium": 0.0, "high": -1.0},
    "blocked_penalty": -3.0,
    "quick_win_bonus": 1.0,
    "reopen_penalty_per": -0.5,
    "skip_floor_per": 0.3,
    "position_weight": 0.5,
    "critical_bug_boost": 5.0,
}

DEFAULT_THRESHOLDS = {
    "max_active_per_agent": 3,
    "staleness_days_refined": 7,
    "staleness_days_ready": 5,
    "critical_unassigned_hours": 4,
    "skip_escalation_count": 5,
}

DEFAULT_MODEL_ROUTING = {"low": "haiku", "medium": "sonnet", "high": "opus"}


# ── Scoring engine ────────────────────────────────────────────────────────────

def resolve_blocks(items):
    """Build a map of item_id → list of item_ids it blocks (bidirectional).

    If item A has link {type: 'blocks', item_id: B}, then A blocks B.
    Returns {blocker_id: [blocked_id, ...]} and {blocked_id: [blocker_id, ...]}.
    """
    blocks_map = {}  # blocker → [items it blocks]
    blocked_by = {}  # blocked → [items blocking it]
    for item in items:
        iid = item.get("id")
        for link in item.get("links", []):
            if link.get("type") == "blocks":
                target = link.get("item_id")
                blocks_map.setdefault(iid, []).append(target)
                blocked_by.setdefault(target, []).append(iid)
    return blocks_map, blocked_by


def compute_freshness(updated_at_str, scoring_cfg):
    """Compute freshness score component from updated_at timestamp."""
    try:
        updated = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0.0
    now = datetime.now(timezone.utc)
    days = (now - updated).total_seconds() / 86400
    boost_days = scoring_cfg.get("freshness_boost_days", 3)
    decay_days = scoring_cfg.get("freshness_decay_days", 14)
    decay_factor = scoring_cfg.get("freshness_decay_factor", 0.5)
    if days <= boost_days:
        return (boost_days - days) / max(boost_days, 1)
    elif days >= decay_days:
        return -((days - decay_days) / max(decay_days, 1)) * decay_factor
    return 0.0


def compute_agent_affinity(item, agent_name, agent_profile, all_items):
    """Compute affinity score for assigning item to agent.

    Returns a numeric affinity: higher is better. Negative means overloaded.
    """
    affinity = 0.0
    item_tags = set(item.get("tags", []))
    agent_skills = set(agent_profile.get("skills", []))

    # Skill match: +2 per overlapping tag
    affinity += 2 * len(item_tags & agent_skills)

    # Complexity preference: +1 if item complexity in preferred list
    complexity = item.get("complexity")
    if complexity and complexity in agent_profile.get("preferred_complexity", []):
        affinity += 1

    # Linked item history: +3 if agent previously worked on a linked item
    linked_ids = {link.get("item_id") for link in item.get("links", [])}
    items_by_id = {i.get("id"): i for i in all_items}
    for lid in linked_ids:
        linked = items_by_id.get(lid)
        if not linked:
            continue
        if linked.get("assigned_to") == agent_name:
            affinity += 3
            break
        for entry in linked.get("lane_history", []):
            by = entry.get("by") if isinstance(entry, dict) else None
            if by == agent_name:
                affinity += 3
                break

    # Overload penalty: -5 if at or above max_active
    max_active = agent_profile.get("max_active", DEFAULT_THRESHOLDS["max_active_per_agent"])
    in_progress_count = sum(
        1 for i in all_items
        if i.get("assigned_to") == agent_name and i.get("status") == "in-progress"
    )
    if in_progress_count >= max_active:
        affinity -= 5

    return affinity


def compute_scores(data):
    """Compute scores for all items using the Work Intelligence Engine formula.

    Returns list of {id, title, status, score, score_breakdown, recommended_agent, recommended_model}.
    """
    config = data.get("config", {})
    scoring = {**DEFAULT_SCORING, **config.get("scoring", {})}
    agents_cfg = config.get("agents", {})
    model_routing = {**DEFAULT_MODEL_ROUTING, **config.get("model_routing", {})}
    items = data.get("items", [])
    total = len(items)
    if total == 0:
        return []

    blocks_map, blocked_by = resolve_blocks(items)
    done_or_discarded = {"done", "discarded"}
    items_by_id = {i.get("id"): i for i in items}

    results = []
    for idx, item in enumerate(items):
        iid = item.get("id")
        breakdown = {}

        # 1. Base priority
        pw = item.get("priority_weight")
        if pw is not None:
            breakdown["base_priority"] = pw * scoring["priority_weight_factor"]
        else:
            breakdown["base_priority"] = ((total - idx) / total) * 10 * scoring["position_weight"]

        # 2. Unblock weight
        blocks_count = len(blocks_map.get(iid, []))
        breakdown["unblock"] = scoring["unblock_weight"] * blocks_count

        # 3. Freshness
        breakdown["freshness"] = compute_freshness(item.get("updated_at", ""), scoring)

        # 4. Complexity
        complexity = item.get("complexity")
        cb = scoring.get("complexity_bonus", {})
        breakdown["complexity"] = cb.get(complexity, cb.get("medium", 0.0)) if complexity else 0.0

        # 5. Blocked penalty
        blockers = blocked_by.get(iid, [])
        is_blocked = any(
            items_by_id.get(b, {}).get("status") not in done_or_discarded
            for b in blockers
        )
        breakdown["blocked_penalty"] = scoring["blocked_penalty"] if is_blocked else 0.0

        # 6. Quick win bonus
        is_quick = complexity == "low" and not is_blocked
        breakdown["quick_win"] = scoring["quick_win_bonus"] if is_quick else 0.0

        # 7. Reopen penalty
        reopen = item.get("reopen_count", 0)
        breakdown["reopen"] = reopen * scoring["reopen_penalty_per"]

        # 8. Skip floor
        skip = item.get("skip_count", 0)
        breakdown["skip_floor"] = skip * scoring["skip_floor_per"]

        # 9. Critical bug boost
        is_critical_bug = (
            item.get("category") == "bug"
            and (item.get("priority_weight") or 0) >= 9
        )
        breakdown["critical_bug"] = scoring["critical_bug_boost"] if is_critical_bug else 0.0

        score = round(sum(breakdown.values()), 1)

        # Recommended agent
        best_agent = None
        best_affinity = -float("inf")
        for aname, aprofile in agents_cfg.items():
            aff = compute_agent_affinity(item, aname, aprofile, items)
            if aff > best_affinity:
                best_affinity = aff
                best_agent = aname
        # Only recommend if positive affinity
        if best_affinity <= 0:
            best_agent = None

        # Recommended model
        rec_model = model_routing.get(complexity or "medium", "sonnet")

        results.append({
            "id": iid,
            "title": item.get("title", ""),
            "status": item.get("status", "backlog"),
            "score": score,
            "score_breakdown": {k: round(v, 2) for k, v in breakdown.items()},
            "recommended_agent": best_agent,
            "recommended_model": rec_model,
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results


def get_agent_loads(data):
    """Compute current load for each configured agent."""
    config = data.get("config", {})
    agents_cfg = config.get("agents", {})
    items = data.get("items", [])
    result = {}
    for aname, aprofile in agents_cfg.items():
        in_progress = [
            i.get("id") for i in items
            if i.get("assigned_to") == aname and i.get("status") == "in-progress"
        ]
        result[aname] = {
            "skills": aprofile.get("skills", []),
            "max_active": aprofile.get("max_active", DEFAULT_THRESHOLDS.get("max_active_per_agent", 3)),
            "preferred_complexity": aprofile.get("preferred_complexity", []),
            "current_load": len(in_progress),
            "items_in_progress": in_progress,
        }
    return result


# ── Event detection ───────────────────────────────────────────────────────────

def detect_events(old_data, new_data):
    """Compare old and new backlog states to detect reprioritization trigger events."""
    events = []
    old_items = {i.get("id"): i for i in old_data.get("items", [])}
    new_items = {i.get("id"): i for i in new_data.get("items", [])}
    done_or_discarded = {"done", "discarded"}

    for nid, new_item in new_items.items():
        old_item = old_items.get(nid)

        if not old_item:
            # New item created
            if new_item.get("category") == "bug" and (new_item.get("priority_weight") or 0) >= 9:
                events.append({"type": "critical_bug_created", "item_id": nid})
            continue

        old_status = old_item.get("status")
        new_status = new_item.get("status")

        # Item reopened (moved backward from done)
        if old_status == "done" and new_status != "done" and new_status != "discarded":
            events.append({"type": "item_reopened", "item_id": nid})

        # Blocker resolved (item with blocks links moved to done/discarded)
        if old_status not in done_or_discarded and new_status in done_or_discarded:
            blocked_items = [
                link.get("item_id")
                for link in new_item.get("links", [])
                if link.get("type") == "blocks"
            ]
            if blocked_items:
                events.append({
                    "type": "blocker_resolved",
                    "item_id": nid,
                    "unblocked": blocked_items,
                })

        # Blocks link count changed
        old_blocks = [l for l in old_item.get("links", []) if l.get("type") == "blocks"]
        new_blocks = [l for l in new_item.get("links", []) if l.get("type") == "blocks"]
        if len(new_blocks) > len(old_blocks):
            events.append({"type": "blocks_changed", "item_id": nid})

    return events


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
        elif parsed.path == "/api/scores":
            self._serve_scores()
        elif parsed.path == "/api/agents":
            self._serve_agents()
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

    def _serve_scores(self):
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return
        scores = compute_scores(data)
        self._json_response(200, {"items": scores})

    def _serve_agents(self):
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return
        loads = get_agent_loads(data)
        self._json_response(200, {"agents": loads})

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

            # Detect events before writing
            events = detect_events(current, incoming)

            # Bump version and write atomically
            incoming["version"] = current_version + 1
            atomic_write(self.backlog_file, incoming)

        response = {"status": "ok", "version": incoming["version"]}
        if events:
            response["_events"] = events
        self._json_response(200, response)

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
            found_idx = -1
            old_snapshot = None
            for i, item in enumerate(data.get("items", [])):
                if item.get("id") == item_id:
                    old_snapshot = dict(item)  # Snapshot before mutation
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
                    found_idx = i
                    break

            if not found:
                self._json_error(404, f"Item {item_id} not found")
                return

            # Detect events by comparing old snapshot with updated data
            events = detect_events({"items": [old_snapshot]}, {"items": [data["items"][found_idx]]})

            data["version"] = current_version + 1
            atomic_write(self.backlog_file, data)

        response = {"status": "ok", "version": data["version"]}
        if events:
            response["_events"] = events
        self._json_response(200, response)

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
