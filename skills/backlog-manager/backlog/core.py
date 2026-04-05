"""BacklogStore — all backlog business logic.

Gate enforcement, lane_history/gate_from watermark, optimistic locking,
and atomic writes live here. The CLI and server are both thin adapters
over this module.
"""

import json
import os
import random
import string
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .exceptions import ConflictError, GateViolationError, ItemNotFoundError

DEFAULT_STATUSES = [
    {"id": "backlog",     "label": "Backlog",      "color": "#94a3b8"},
    {"id": "refined",     "label": "Refined",      "color": "#f59e0b"},
    {"id": "ready",       "label": "Ready",         "color": "#10b981"},
    {"id": "in-progress", "label": "In Progress",  "color": "#3b82f6"},
    {"id": "code-review", "label": "Code Review",  "color": "#ec4899"},
    {"id": "done",        "label": "Done",          "color": "#8b5cf6"},
    {"id": "discarded",   "label": "Discarded",    "color": "#cbd5e1"},
]

STARTER_BACKLOG = {
    "version": 0,
    "config": {
        "scope": "project",
        "project_name": "",
    },
    "items": [],
}


def _generate_id(length: int = 8) -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=length))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_status_config(data: dict) -> list:
    """Return the configured statuses list, falling back to DEFAULT_STATUSES."""
    cfg = data.get("config", {}).get("statuses")
    if isinstance(cfg, list) and len(cfg) >= 2:
        return cfg
    return DEFAULT_STATUSES


# Keep private alias for internal use
_get_status_config = get_status_config


def _get_status_index(statuses: list, status_id: str) -> int:
    for i, s in enumerate(statuses):
        if s.get("id") == status_id:
            return i
    return -1


def _extract_lane(entry) -> str:
    """Support both old string format and new dict format for lane_history entries."""
    return entry.get("lane") if isinstance(entry, dict) else entry


def _normalize_lane_history(items: list) -> None:
    """Normalize bare-string lane_history entries to structured dicts in-place.

    Old format: ["backlog", "refined", ...]
    New format: [{"lane": "backlog", "at": <iso>, "by": <actor>}, ...]

    Bare strings written before structured tracking was in place are converted
    to {"lane": <value>, "at": null, "by": "unknown"} so callers always see
    a consistent schema.
    """
    for item in items:
        history = item.get("lane_history")
        if not isinstance(history, list):
            continue
        normalized = []
        for entry in history:
            if isinstance(entry, str):
                normalized.append({"lane": entry, "at": None, "by": "unknown"})
            else:
                normalized.append(entry)
        item["lane_history"] = normalized


def validate_lane_transition(item: dict, new_status: str, statuses: list) -> tuple[bool, str | None]:
    """Check if moving item to new_status satisfies gate rules.

    Returns (ok, error_message).
    - Moving backward is always allowed.
    - Discarding is always allowed.
    - Forward moves must satisfy the target lane's `requires`.
    """
    if new_status == item.get("status"):
        return True, None

    if new_status == "discarded":
        return True, None

    old_idx = _get_status_index(statuses, item.get("status", "backlog"))
    new_idx = _get_status_index(statuses, new_status)

    if new_idx <= old_idx:
        return True, None  # backward move always allowed

    target_config = next((s for s in statuses if s.get("id") == new_status), None)
    if not target_config:
        return True, None  # unknown status — let through

    requires = target_config.get("requires", [])
    if not requires:
        return True, None

    full_history = item.get("lane_history", [])
    gate_from = item.get("gate_from", 0)
    relevant = set(_extract_lane(e) for e in full_history[gate_from:])
    relevant.add(item.get("status", "backlog"))

    missing = [r for r in requires if r not in relevant]
    if missing:
        label_map = {s.get("id"): s.get("label", s.get("id")) for s in statuses}
        missing_labels = [label_map.get(m, m) for m in missing]
        target_label = target_config.get("label", new_status)
        return False, (
            f"Cannot move to '{target_label}' — requires passing through: "
            f"{', '.join(missing_labels)}"
        )

    return True, None


# Private alias
_validate_lane_transition = validate_lane_transition


def apply_lane_transition(item: dict, new_status: str, statuses: list, moved_by: str = "cli") -> None:
    """Mutate item in place: append lane_history entry and update gate_from on backward moves.

    Forward move:  append current lane to lane_history, gate_from unchanged.
    Backward move: append current lane to lane_history, set gate_from = len(history)
                   so the item must re-earn forward gates from its new position.
    """
    old_status = item.get("status", "backlog")
    if old_status == new_status:
        return

    old_idx = _get_status_index(statuses, old_status)
    new_idx = _get_status_index(statuses, new_status)

    history = list(item.get("lane_history", []))
    if not isinstance(history, list):
        history = []

    history.append({"lane": old_status, "at": _now_iso(), "by": moved_by})
    item["lane_history"] = history

    if new_idx < old_idx:
        # Backward move — reset watermark
        item["gate_from"] = len(history)
        if old_status == "done":
            item["reopen_count"] = item.get("reopen_count", 0) + 1

    item["status"] = new_status
    item["updated_at"] = _now_iso()


# Private alias
_apply_lane_transition = apply_lane_transition


class BacklogStore:
    """All backlog CRUD + gate enforcement. File-backed, thread-safe via atomic writes."""

    def __init__(self, file_path: str) -> None:
        self.file_path = os.path.abspath(file_path)

    # ── Read / Write ──────────────────────────────────────────────────────────

    def read(self) -> dict:
        """Read and parse backlog.json. Returns empty starter if file does not exist.

        Normalizes bare-string lane_history entries to structured dicts so all
        callers (CLI, server, board) always see a consistent schema.
        """
        try:
            with open(self.file_path, "r") as f:
                data = json.load(f)
        except FileNotFoundError:
            return dict(STARTER_BACKLOG)
        except json.JSONDecodeError as e:
            raise ValueError(f"backlog.json is corrupted: {e}")
        _normalize_lane_history(data.get("items", []))
        return data

    def write(self, data: dict, expected_version: int | None = None) -> None:
        """Atomically write data to backlog.json, incrementing the version.

        If expected_version is provided and the file's current version is higher,
        raises ConflictError (optimistic locking).
        """
        if expected_version is not None:
            try:
                current = self.read()
                current_version = current.get("version", 0)
                if expected_version < current_version:
                    raise ConflictError(
                        f"Version conflict: you have v{expected_version}, "
                        f"current is v{current_version}. Re-read and retry."
                    )
            except ValueError:
                pass  # Corrupted file — overwrite is acceptable

        data["version"] = data.get("version", 0) + 1
        dir_path = os.path.dirname(self.file_path)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=dir_path, suffix=".tmp", delete=False
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        os.replace(tmp_path, self.file_path)

    def init(self) -> None:
        """Write a starter backlog.json. Raises if file already exists."""
        if os.path.exists(self.file_path):
            raise FileExistsError(f"{self.file_path} already exists.")
        project_name = Path(self.file_path).parent.name
        starter = dict(STARTER_BACKLOG)
        starter["config"]["project_name"] = project_name
        # write() increments version to 1; we want it to start at 0
        starter["version"] = -1
        self.write(starter)

    # ── Item lookup ───────────────────────────────────────────────────────────

    def get_item(self, position: int) -> tuple[dict, dict]:
        """Return (data, item) for the 1-based position. Raises ItemNotFoundError."""
        data = self.read()
        items = data.get("items", [])
        idx = position - 1
        if idx < 0 or idx >= len(items):
            raise ItemNotFoundError(
                f"Item #{position} not found (backlog has {len(items)} item(s))."
            )
        return data, items[idx]

    # ── CRUD operations ───────────────────────────────────────────────────────

    def add_item(self, title: str, **fields) -> dict:
        """Append a new item to the bottom of the backlog. Returns the new item."""
        data = self.read()
        now = _now_iso()
        item = {
            "id": _generate_id(),
            "title": title,
            "status": "backlog",
            "priority": fields.get("priority"),
            "priority_weight": fields.get("priority_weight", 5),
            "complexity": fields.get("complexity"),
            "category": fields.get("category"),
            "tags": fields.get("tags", []),
            "description": fields.get("description", ""),
            "assigned_to": fields.get("assigned_to"),
            "links": [],
            "threads": [],
            "lane_history": [],
            "gate_from": 0,
            "reopen_count": 0,
            "skip_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        # Remove None-valued optional fields to keep JSON clean
        item = {k: v for k, v in item.items() if v is not None or k in (
            "id", "title", "status", "description", "links", "threads",
            "lane_history", "gate_from", "reopen_count", "skip_count",
            "created_at", "updated_at", "tags",
        )}
        data.setdefault("items", []).append(item)
        self.write(data)
        return item

    def move_item(self, position: int, target_status: str, moved_by: str = "cli") -> dict:
        """Move item to target_status, enforcing gate rules.

        Raises GateViolationError if gates are not satisfied.
        Raises ItemNotFoundError if position is out of range.
        Raises ConflictError if the file was modified since it was read.
        """
        data, item = self.get_item(position)
        statuses = _get_status_config(data)

        ok, err = _validate_lane_transition(item, target_status, statuses)
        if not ok:
            raise GateViolationError(err)

        _apply_lane_transition(item, target_status, statuses, moved_by=moved_by)
        self.write(data, expected_version=data.get("version", 0))
        return item

    def assign_item(self, position: int, agent: str) -> dict:
        """Assign item to agent. Raises ItemNotFoundError if not found."""
        data, item = self.get_item(position)
        item["assigned_to"] = agent
        item["updated_at"] = _now_iso()
        self.write(data, expected_version=data.get("version", 0))
        return item

    def unassign_item(self, position: int) -> dict:
        """Remove assignment from item. Raises ItemNotFoundError if not found."""
        data, item = self.get_item(position)
        item["assigned_to"] = None
        item["updated_at"] = _now_iso()
        self.write(data, expected_version=data.get("version", 0))
        return item

    def edit_item(self, position: int, **fields) -> dict:
        """Update one or more fields on an item. Status changes go through move_item."""
        if "status" in fields:
            raise ValueError("Use move_item() to change status — gate rules apply.")
        data, item = self.get_item(position)
        allowed = {"title", "description", "priority", "priority_weight",
                   "complexity", "category", "tags", "assigned_to"}
        for k, v in fields.items():
            if k in allowed:
                item[k] = v
        item["updated_at"] = _now_iso()
        self.write(data, expected_version=data.get("version", 0))
        return item

    def discard_item(self, position: int, moved_by: str = "cli") -> dict:
        """Discard item — always allowed from any lane."""
        data, item = self.get_item(position)
        statuses = _get_status_config(data)
        _apply_lane_transition(item, "discarded", statuses, moved_by=moved_by)
        self.write(data, expected_version=data.get("version", 0))
        return item

    def restore_item(self, position: int, moved_by: str = "cli") -> dict:
        """Restore item to backlog lane. Always allowed — resets gate_from watermark."""
        data, item = self.get_item(position)
        statuses = _get_status_config(data)
        _apply_lane_transition(item, "backlog", statuses, moved_by=moved_by)
        self.write(data, expected_version=data.get("version", 0))
        return item

    def pick_item(self, agent: str, moved_by: str = "cli") -> dict:
        """Pick the highest-priority ready item, move to in-progress, assign to agent.

        Priority is determined by position in the array (earlier = higher priority).
        Raises ItemNotFoundError if no ready items exist.
        Raises ConflictError if the file was modified since it was read.
        """
        data = self.read()
        statuses = _get_status_config(data)
        items = data.get("items", [])

        ready_item = next(
            (item for item in items if item.get("status") == "ready"), None
        )
        if not ready_item:
            raise ItemNotFoundError("No ready items in the backlog.")

        _apply_lane_transition(ready_item, "in-progress", statuses, moved_by=moved_by)
        ready_item["assigned_to"] = agent
        ready_item["updated_at"] = _now_iso()
        self.write(data, expected_version=data.get("version", 0))
        return ready_item

    def ingest_result(self, report: dict) -> dict:
        """Process a handoff result report and drive the backlog forward.

        Returns a summary dict describing every action taken.
        Raises ValueError on schema violations or unknown item_id.
        """
        # ── Validate schema ───────────────────────────────────────────────────
        item_id = report.get("item_id")
        status = report.get("status")
        summary = report.get("summary", "")

        if not item_id:
            raise ValueError("result file missing required field: item_id")
        if status not in ("done", "blocked", "partial"):
            raise ValueError(
                f"invalid status {status!r} — must be done, blocked, or partial"
            )

        # ── Load backlog ──────────────────────────────────────────────────────
        data = self.read()
        items = data.setdefault("items", [])
        statuses = _get_status_config(data)

        target = next((i for i in items if i.get("id") == item_id), None)
        if target is None:
            raise ValueError(f"item_id {item_id!r} not found in backlog")

        now = _now_iso()
        actions: list[str] = []

        # ── Status handling ───────────────────────────────────────────────────
        if status == "done":
            # Advance to next lane after current (code-review if present, else done)
            status_ids = [s.get("id") for s in statuses]
            current = target.get("status", "backlog")
            try:
                current_idx = status_ids.index(current)
            except ValueError:
                current_idx = -1
            # Skip special-cased lanes: discarded, done — find next sensible lane
            next_lane = "done"
            for sid in status_ids[current_idx + 1:]:
                if sid not in ("discarded",):
                    next_lane = sid
                    break
            _apply_lane_transition(target, next_lane, statuses, moved_by="ingest")
            actions.append(f"advanced item {item_id} to {next_lane!r}")

        else:
            # blocked or partial — item stays in-progress, open a thread
            thread_body = (
                report.get("blocker", summary)
                if status == "blocked"
                else summary
            )
            thread = {
                "id": _generate_id(),
                "waiting_on": "lead",
                "status": status,
                "body": thread_body,
                "created_at": now,
                "resolved": False,
            }
            target.setdefault("threads", []).append(thread)
            target["updated_at"] = now
            actions.append(
                f"opened thread on item {item_id} (waiting_on=lead, status={status})"
            )

        # ── Discovered items ──────────────────────────────────────────────────
        new_items: list[dict] = []

        for bug in report.get("bugs_found", []):
            new_id = _generate_id()
            new_item = {
                "id": new_id,
                "title": bug.get("title", "Untitled bug"),
                "status": "backlog",
                "priority_weight": 8,
                "category": "bug",
                "description": bug.get("description", ""),
                "tags": [],
                "links": [
                    {
                        "item_id": item_id,
                        "type": "discovered-during",
                        "reason": f"Found while working on {item_id}",
                    }
                ],
                "threads": [],
                "lane_history": [],
                "gate_from": 0,
                "reopen_count": 0,
                "skip_count": 0,
                "created_at": now,
                "updated_at": now,
            }
            new_items.append(new_item)
            actions.append(f"created bug item {new_id!r}: {new_item['title']!r}")

        for fu in report.get("follow_ups", []):
            new_id = _generate_id()
            new_item = {
                "id": new_id,
                "title": fu.get("title", "Untitled follow-up"),
                "status": "backlog",
                "priority_weight": 5,
                "category": "feature",
                "description": fu.get("description", ""),
                "tags": [],
                "links": [
                    {
                        "item_id": item_id,
                        "type": "follow-up",
                        "reason": f"Follow-up from {item_id}",
                    }
                ],
                "threads": [],
                "lane_history": [],
                "gate_from": 0,
                "reopen_count": 0,
                "skip_count": 0,
                "created_at": now,
                "updated_at": now,
            }
            new_items.append(new_item)
            actions.append(f"created follow-up item {new_id!r}: {new_item['title']!r}")

        items.extend(new_items)

        self.write(data, expected_version=data.get("version", 0))

        return {
            "item_id": item_id,
            "status_applied": status,
            "next_lane": target.get("status"),
            "new_items": [{"id": i["id"], "title": i["title"], "category": i["category"]} for i in new_items],
            "actions": actions,
            "note": "Tribunal should re-run to recompute scores for affected items.",
        }

    def reorder(self, position: int, new_position: int) -> None:
        """Move item from position to new_position (both 1-based). Adjusts priority order."""
        data = self.read()
        items = data.get("items", [])
        n = len(items)
        if not (1 <= position <= n):
            raise ItemNotFoundError(f"Item #{position} not found.")
        if not (1 <= new_position <= n):
            raise ItemNotFoundError(f"Target position #{new_position} is out of range.")
        item = items.pop(position - 1)
        items.insert(new_position - 1, item)
        data["items"] = items
        self.write(data)
