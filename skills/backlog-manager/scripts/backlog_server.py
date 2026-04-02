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
    GET  /api/recommend            Tribunal-justified recommendation (optional ?agent=name&commit=true)
    GET  /api/decisions            Stored decision history with outcomes
    GET  /api/agents               Agent profiles with current load
    GET  /api/graph                Live dependency graph with readiness, critical path, conflicts
    GET  /api/pulse                Proactive push payload + policy intelligence (optional ?agent=name)
    PUT  /api/backlog              Full backlog write (version checked, atomic, returns _events)
    PUT  /api/items/<id>           Single item update (version checked, atomic, returns _events)
    POST /api/items/<id>/signal    Append a readiness signal to an item
    GET  /api/policies             List all policies with staleness analysis
    POST /api/policies             Create a new policy
    PUT  /api/policies/<id>        Update a policy (name, description, priority, active)
    DELETE /api/policies/<id>      Delete a policy
    GET  /api/policies/log         Recent policy fire history (optional ?limit=N)
    GET  /api/policies/evaluate    Manually trigger policy evaluation
    GET  /api/policies/suggestions LLM-generated rule suggestions based on patterns
"""

import argparse
import glob as glob_mod
import json
import os
import re
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
            data = json.load(f)
    except FileNotFoundError:
        data = {"version": 0, "config": {"scope": "project", "project_name": ""}, "items": []}
    except json.JSONDecodeError as e:
        raise ValueError(f"backlog.json is corrupted and cannot be read: {e}")

    # Merge agent profiles from .claude/agents/*.md into config.agents
    project_root = str(Path(filepath).parent)
    merge_agent_profiles(data, project_root)
    return data


# ── Agent file parsing (.claude/agents/*.md) ─────────────────────────────────

# Regex to extract YAML frontmatter between --- delimiters
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)

# Regex to parse a single YAML line: "key: value" (supports simple scalars and [] arrays)
_YAML_LINE_RE = re.compile(r"^(\w+)\s*:\s*(.+)$")

# Regex to parse a YAML inline list: [item1, item2, ...]
_YAML_LIST_RE = re.compile(r"^\[(.+)]$")


def _parse_simple_yaml(text):
    """Parse simple YAML frontmatter (flat key-value pairs, inline lists).
    No external dependency — handles the subset we need for agent files."""
    result = {}
    for line in text.strip().splitlines():
        m = _YAML_LINE_RE.match(line.strip())
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        list_m = _YAML_LIST_RE.match(val)
        if list_m:
            # Inline list: [a, b, c]
            result[key] = [v.strip().strip("'\"") for v in list_m.group(1).split(",")]
        else:
            # Scalar — try int, then keep as string
            try:
                result[key] = int(val)
            except ValueError:
                result[key] = val.strip("'\"")
    return result


def parse_agent_files(project_root):
    """Parse all .claude/agents/*.md files and return a dict of agent profiles.
    Each file's YAML frontmatter provides: name, skills, complexity, max_active.
    Returns {agent_name: {skills: [...], max_active: N, preferred_complexity: [...]}}."""
    agents_dir = os.path.join(project_root, ".claude", "agents")
    if not os.path.isdir(agents_dir):
        return {}

    agents = {}
    for fpath in sorted(glob_mod.glob(os.path.join(agents_dir, "*.md"))):
        try:
            with open(fpath, "r") as f:
                content = f.read()
        except OSError:
            continue

        fm_match = _FRONTMATTER_RE.match(content)
        if not fm_match:
            continue

        meta = _parse_simple_yaml(fm_match.group(1))
        name = meta.get("name")
        if not name:
            # Fall back to filename without extension
            name = Path(fpath).stem

        agents[name] = {
            "skills": meta.get("skills", []),
            "max_active": meta.get("max_active", 3),
            "preferred_complexity": meta.get("complexity", []),
        }
    return agents


def merge_agent_profiles(data, project_root):
    """Merge .claude/agents/*.md profiles into data['config']['agents'].
    File-sourced agents take precedence. Agents defined only in config
    (no matching .md file) are preserved as fallback."""
    file_agents = parse_agent_files(project_root)
    if not file_agents:
        return  # No .claude/agents/ directory or no files — keep existing config

    config = data.setdefault("config", {})
    existing = config.get("agents", {})

    # File agents take precedence; preserve config-only agents as fallback
    merged = dict(existing)
    merged.update(file_agents)
    config["agents"] = merged


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


# ── Readiness Signal Engine ────────────────────────────────────────────────────

# Trust weights for each artifact/gate signal type
SIGNAL_TRUST = {
    "spec_written":    0.10,  # medium: intent artifact
    "file_created":    0.10,  # medium: code artifact exists
    "design_approved": 0.15,  # medium: design gate
    "pr_merged":       0.25,  # high: integration artifact
    "test_passed":     0.20,  # high: downstream gate passed
    "review_approved": 0.25,  # high: peer-review gate passed
}

VALID_SIGNAL_TYPES = set(SIGNAL_TRUST.keys())

# Status-based readiness baseline — how far along is an item in its lifecycle?
STATUS_READINESS = {
    "backlog":     0.05,
    "refined":     0.20,
    "ready":       0.35,
    "in-progress": 0.50,
    "code-review": 0.70,
    # "done" and "discarded" → 1.0, handled separately
}

# Readiness thresholds (configurable via config.readiness)
DEFAULT_READINESS_CONFIG = {
    "startable_threshold": 0.70,  # ≥70% → startable with known risk
    "ready_threshold":     0.90,  # ≥90% → fully ready, no risk flag
}


def compute_item_readiness(item, done_or_discarded=None):
    """Compute readiness score (0.0–1.0) for a single item.

    Combines a status-based baseline with observed artifact/gate signals.
    - done/discarded: 1.0 (complete — no longer a blocker)
    - other statuses: baseline + signal contributions, capped at 0.95
    Returns a dict: {score, status_contribution, signal_contribution, signals}.
    """
    if done_or_discarded is None:
        done_or_discarded = {"done", "discarded"}

    status = item.get("status", "backlog")
    signals = item.get("readiness_signals", [])

    if status in done_or_discarded:
        return {
            "score": 1.0,
            "status_contribution": 1.0,
            "signal_contribution": 0.0,
            "signals": signals,
        }

    status_contribution = STATUS_READINESS.get(status, 0.05)
    signal_contribution = sum(
        SIGNAL_TRUST.get(s.get("type", ""), 0.0) for s in signals
    )
    # Cap: an item can reach at most 0.95 readiness without being done
    headroom = max(0.0, 0.95 - status_contribution)
    capped_signal = min(signal_contribution, headroom)
    score = round(status_contribution + capped_signal, 2)

    return {
        "score": score,
        "status_contribution": round(status_contribution, 2),
        "signal_contribution": round(capped_signal, 2),
        "signals": signals,
    }


# ── Justification Engine (Tribunal) ──────────────────────────────────────────

# Statuses eligible for recommendation (available for pick-up)
RECOMMEND_ELIGIBLE = {"backlog", "refined", "ready"}

# Lens weights — how much each lens influences the final verdict
LENS_WEIGHTS = {
    "urgency": 1.0,
    "leverage": 1.2,
    "agent_fit": 0.8,
    "risk": 1.0,
    "momentum": 0.6,
    "strategic": 1.0,
}

# Default strategic config — current_focus is a list of tags/categories the team prioritizes
DEFAULT_STRATEGIC = {
    "current_focus": [],        # e.g., ["auth", "security"] — items matching these get a boost
    "high_priority_threshold": 8,  # priority_weight >= this counts as explicitly high priority
}


def compute_unblock_cascade(start_id, blocks_map):
    """Compute all items transitively unblocked if start_id completes."""
    visited = set()
    queue = list(blocks_map.get(start_id, []))
    while queue:
        nid = queue.pop(0)
        if nid in visited:
            continue
        visited.add(nid)
        queue.extend(blocks_map.get(nid, []))
    return visited


# ── Phase 3: Multi-Agent Coordination ─────────────────────────────────────────

def compute_critical_path(blocks_map, items_by_id, done_or_discarded):
    """Identify items on the critical path — those whose delay cascades to the most downstream work.

    Returns (critical_items_sorted, cascade_counts).
    critical_items_sorted: active item IDs ordered by cascade count, highest first.
    cascade_counts: {item_id: int} for all items.
    """
    cascade_counts = {iid: len(compute_unblock_cascade(iid, blocks_map)) for iid in items_by_id}
    active_with_cascade = [
        iid for iid, item in items_by_id.items()
        if item.get("status") not in done_or_discarded and cascade_counts.get(iid, 0) > 0
    ]
    critical_sorted = sorted(active_with_cascade, key=lambda iid: cascade_counts[iid], reverse=True)
    return critical_sorted, cascade_counts


def detect_conflicts(items):
    """Detect intent conflicts between in-progress items.

    Flags tag/area overlap between items worked on by different agents simultaneously.
    Returns list of {type, items, item_titles, shared_tags, description}.
    """
    conflicts = []
    in_progress = [i for i in items if i.get("status") == "in-progress"]
    if len(in_progress) < 2:
        return conflicts

    tag_to_items = {}
    for item in in_progress:
        for tag in item.get("tags", []):
            tag_to_items.setdefault(tag, []).append(item)

    reported_pairs = set()
    for tag_items in tag_to_items.values():
        if len(tag_items) < 2:
            continue
        for i in range(len(tag_items)):
            for j in range(i + 1, len(tag_items)):
                a, b = tag_items[i], tag_items[j]
                # Same agent on related items is expected, not a conflict
                if a.get("assigned_to") and a.get("assigned_to") == b.get("assigned_to"):
                    continue
                pair = tuple(sorted([a.get("id", ""), b.get("id", "")]))
                if pair in reported_pairs:
                    continue
                reported_pairs.add(pair)
                shared = sorted(set(a.get("tags", [])) & set(b.get("tags", [])))
                a_agent = a.get("assigned_to") or "unassigned"
                b_agent = b.get("assigned_to") or "unassigned"
                conflicts.append({
                    "type": "tag_overlap",
                    "items": [a.get("id"), b.get("id")],
                    "item_titles": [a.get("title", ""), b.get("title", "")],
                    "shared_tags": shared,
                    "description": (
                        f'"{a.get("title","")}" ({a_agent}) and '
                        f'"{b.get("title","")}" ({b_agent}) '
                        f'both touch [{", ".join(shared)}]'
                    ),
                })
    return conflicts


def compute_workload_rebalancing(items, agents_cfg):
    """Generate rebalancing suggestions when agents have uneven load.

    Returns list of {type, from_agent|agent, to_agent, description, transferable_items?}.
    """
    if not agents_cfg:
        return []

    in_progress_by_agent = {}
    for item in items:
        if item.get("status") == "in-progress" and item.get("assigned_to"):
            in_progress_by_agent.setdefault(item["assigned_to"], []).append(item)

    overloaded, underloaded = [], []
    for aname, aprofile in agents_cfg.items():
        max_active = aprofile.get("max_active", 3)
        current = in_progress_by_agent.get(aname, [])
        if len(current) >= max_active:
            overloaded.append({"agent": aname, "current": len(current),
                                "max": max_active, "items": current})
        elif len(current) == 0:
            underloaded.append({"agent": aname, "max": max_active,
                                 "skills": set(aprofile.get("skills", []))})

    suggestions = []
    mentioned_idle = set()
    for over in overloaded:
        matched = False
        for under in underloaded:
            transferable = [
                i.get("id") for i in over["items"]
                if set(i.get("tags", [])) & under["skills"]
            ]
            if transferable:
                suggestions.append({
                    "type": "rebalance",
                    "from_agent": over["agent"],
                    "to_agent": under["agent"],
                    "description": (
                        f'{over["agent"]} at capacity ({over["current"]}/{over["max"]}). '
                        f'{under["agent"]} is idle with matching skills.'
                    ),
                    "transferable_items": transferable[:2],
                })
                matched = True
        if not matched:
            for under in underloaded:
                if under["agent"] not in mentioned_idle:
                    mentioned_idle.add(under["agent"])
                    suggestions.append({
                        "type": "idle_agent",
                        "agent": under["agent"],
                        "description": f'{under["agent"]} is idle and available for new work.',
                    })
    return suggestions


def compute_dependency_graph(data):
    """Compute the full dependency graph with readiness, critical path, conflicts, and rebalancing.

    Returns {nodes, edges, critical_path, conflicts, rebalancing}.
    nodes: [{id, title, status, readiness, readiness_level, assigned_to, complexity, category,
             tags, is_critical_path, cascade_count}]
    edges: [{source, target, type, reason}]
    critical_path: [item_id, ...] — top 10 by cascade impact
    conflicts: [{type, items, item_titles, shared_tags, description}]
    rebalancing: [{type, ...}]
    """
    config = data.get("config", {})
    items = data.get("items", [])
    agents_cfg = config.get("agents", {})
    done_or_discarded = {"done", "discarded"}
    items_by_id = {i.get("id", ""): i for i in items}
    blocks_map, _ = resolve_blocks(items)

    critical_items, cascade_counts = compute_critical_path(blocks_map, items_by_id, done_or_discarded)
    critical_set = set(critical_items[:5])
    conflicts = detect_conflicts(items)
    rebalancing = compute_workload_rebalancing(items, agents_cfg)

    nodes = []
    for item in items:
        iid = item.get("id", "")
        rd = compute_item_readiness(item, done_or_discarded)
        rs = rd["score"]
        nodes.append({
            "id": iid,
            "title": item.get("title", ""),
            "status": item.get("status", "backlog"),
            "readiness": round(rs, 2),
            "readiness_level": "ready" if rs >= 0.9 else "startable" if rs >= 0.7 else "not_ready",
            "assigned_to": item.get("assigned_to"),
            "complexity": item.get("complexity"),
            "category": item.get("category"),
            "tags": item.get("tags", []),
            "is_critical_path": iid in critical_set,
            "cascade_count": cascade_counts.get(iid, 0),
        })

    edges = []
    seen_edges = set()
    for item in items:
        iid = item.get("id", "")
        for link in item.get("links", []):
            target = link.get("item_id")
            ltype = link.get("type")
            if not target or not ltype:
                continue
            key = (iid, target, ltype)
            if key in seen_edges:
                continue
            seen_edges.add(key)
            edges.append({
                "source": iid,
                "target": target,
                "type": ltype,
                "reason": link.get("reason", ""),
            })

    return {
        "nodes": nodes,
        "edges": edges,
        "critical_path": critical_items[:10],
        "conflicts": conflicts,
        "rebalancing": rebalancing,
    }


def compute_pulse(data, agent_name=None, backlog_path=None):
    """Compute a proactive push pulse for an agent.

    Bundles: tribunal recommendation + startable items + conflicts + rebalancing +
    active agents + policy intelligence.
    Single call replaces /api/recommend + /api/agents + /api/policies + /api/policies/log.
    """
    items = data.get("items", [])
    config = data.get("config", {})
    agents_cfg = config.get("agents", {})
    done_or_discarded = {"done", "discarded"}

    # Build ONCE here — pass to everything below
    blocks_map, blocked_by = resolve_blocks(items)
    items_by_id = {i.get("id"): i for i in items}
    
    recommendation = evaluate_tribunal(data, agent=agent_name,
                                       blocks_map=blocks_map, blocked_by=blocked_by)
    conflicts = detect_conflicts(items)
    rebalancing = compute_workload_rebalancing(items, agents_cfg)

    active_agents = []
    for aname, aprofile in agents_cfg.items():
        max_active = aprofile.get("max_active", 3)
        in_prog = [
            {"id": i.get("id"), "title": i.get("title", "")}
            for i in items
            if i.get("assigned_to") == aname and i.get("status") == "in-progress"
        ]
        active_agents.append({
            "name": aname,
            "items_in_progress": in_prog,
            "current_load": len(in_prog),
            "max_active": max_active,
            "load_pct": round(len(in_prog) / max(max_active, 1) * 100),
        })
    
    startable = []
    for item in items:
        if item.get("status") in done_or_discarded or item.get("status") == "in-progress":
            continue
        rd = compute_item_readiness(item, done_or_discarded)
        if rd["score"] < 0.70:
            continue
        if agent_name:
            profile = agents_cfg.get(agent_name, {})
            if compute_agent_affinity(item, agent_name, profile, items_by_id, {}) < 0:
                continue
        startable.append({
            "id": item.get("id"),
            "title": item.get("title", ""),
            "status": item.get("status"),
            "readiness": round(rd["score"], 2),
            "readiness_level": "ready" if rd["score"] >= 0.90 else "startable",
        })

    result = {
        "agent": agent_name,
        "recommendation": recommendation,
        "startable_items": startable[:5],
        "conflicts": conflicts,
        "rebalancing": rebalancing,
        "active_agents": active_agents,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    if backlog_path:
        result["policies"] = compute_pulse_policy_summary(backlog_path, recommendation)

    return result


def evaluate_lens_urgency(item, breakdown, scoring_cfg):
    """Urgency: time-sensitivity, criticality, priority weight."""
    score = 0.0
    reasons = []

    if breakdown.get("critical_bug", 0) > 0:
        score += 10.0
        pw = item.get("priority_weight") or 0
        reasons.append(f"Critical bug (priority {pw}/10)")

    pw = item.get("priority_weight")
    if pw is not None:
        if pw >= 8:
            score += pw
            reasons.append(f"High priority ({pw}/10)")
        elif pw >= 5:
            score += pw * 0.5

    freshness = breakdown.get("freshness", 0)
    if freshness < -0.2:
        score += abs(freshness) * 3
        decay_days = scoring_cfg.get("freshness_decay_days", 14)
        days_approx = int(abs(freshness) / 0.5 * decay_days)
        reasons.append(f"Going stale (~{days_approx}d without activity)")

    return {
        "lens": "urgency",
        "item_id": item.get("id"),
        "score": round(score, 2),
        "argument": "; ".join(reasons) if reasons else None,
    }


def evaluate_lens_leverage(item, breakdown, blocks_map):
    """Leverage: how much downstream work completing this unblocks."""
    iid = item.get("id")
    direct_blocks = blocks_map.get(iid, [])
    score = 0.0
    reasons = []

    if direct_blocks:
        cascade = compute_unblock_cascade(iid, blocks_map)
        score += len(direct_blocks) * 3.0
        reasons.append(f"Directly unblocks {len(direct_blocks)} item(s)")
        if len(cascade) > len(direct_blocks):
            score += (len(cascade) - len(direct_blocks)) * 1.5
            reasons.append(f"{len(cascade)} items in full unblock cascade")

    return {
        "lens": "leverage",
        "item_id": item.get("id"),
        "score": round(score, 2),
        "argument": "; ".join(reasons) if reasons else None,
    }


def evaluate_lens_agent_fit(item, agent_name, agents_cfg, items_by_id):
    """Agent fit: how well matched to the best available agent."""
    score = 0.0
    reasons = []
    best_agent = None

    if agent_name:
        profile = agents_cfg.get(agent_name, {})
        affinity = compute_agent_affinity(item, agent_name, profile, items_by_id, {})
        if affinity > 0:
            score += affinity * 1.5
            item_tags = set(item.get("tags", []))
            agent_skills = set(profile.get("skills", []))
            overlap = item_tags & agent_skills
            if overlap:
                reasons.append(f"Skill match: {', '.join(sorted(overlap))}")
            complexity = item.get("complexity")
            if complexity in profile.get("preferred_complexity", []):
                reasons.append(f"Preferred complexity ({complexity})")
            best_agent = agent_name
        elif affinity <= -5:
            reasons.append(f"{agent_name} at max capacity")
        else:
            reasons.append(f"Low affinity for {agent_name}")
    else:
        best_affinity = -float("inf")
        for aname, aprofile in agents_cfg.items():
            aff = compute_agent_affinity(item, aname, aprofile, items_by_id, {})
            if aff > best_affinity:
                best_affinity = aff
                best_agent = aname
        if best_affinity > 0:
            score += best_affinity * 1.5
            profile = agents_cfg.get(best_agent, {})
            item_tags = set(item.get("tags", []))
            agent_skills = set(profile.get("skills", []))
            overlap = item_tags & agent_skills
            if overlap:
                reasons.append(f"Best agent: {best_agent} (skills: {', '.join(sorted(overlap))})")
            else:
                reasons.append(f"Best agent: {best_agent}")
        else:
            reasons.append("No agent with positive affinity")
            best_agent = None

    return {
        "lens": "agent_fit",
        "item_id": item.get("id"),
        "score": round(score, 2),
        "argument": "; ".join(reasons) if reasons else None,
        "recommended_agent": best_agent,
    }


def evaluate_lens_risk(item, breakdown, blocks_map, items_by_id):
    """Risk: what happens if this item is delayed further."""
    iid = item.get("id")
    score = 0.0
    reasons = []

    direct_blocks = blocks_map.get(iid, [])
    if direct_blocks:
        score += len(direct_blocks) * 2.5
        blocked_titles = [items_by_id.get(bid, {}).get("title", bid) for bid in direct_blocks[:3]]
        reasons.append(f"Blocking: {', '.join(blocked_titles)}")

    reopens = item.get("reopen_count", 0)
    if reopens >= 2:
        score += reopens * 2.0
        reasons.append(f"Reopened {reopens}x — unstable area, needs focused attention")

    skips = item.get("skip_count", 0)
    if skips >= 3:
        score += skips * 1.0
        reasons.append(f"Skipped {skips}x — risk of permanent neglect")

    return {
        "lens": "risk",
        "item_id": item.get("id"),
        "score": round(score, 2),
        "argument": "; ".join(reasons) if reasons else None,
    }


def evaluate_lens_momentum(item, breakdown):
    """Momentum: items already in motion or with recent activity."""
    score = 0.0
    reasons = []

    status = item.get("status", "backlog")
    progression = {"backlog": 0, "refined": 1, "ready": 2}
    progress = progression.get(status, 0)

    if progress >= 2:
        score += 4.0
        reasons.append("In ready — cleared for work")
    elif progress == 1:
        score += 2.0
        reasons.append("Refined — one step from ready")

    freshness = breakdown.get("freshness", 0)
    if freshness > 0.3:
        score += freshness * 2.0
        reasons.append("Recently active")

    history_len = len(item.get("lane_history", []))
    if history_len >= 3:
        score += min(history_len * 0.5, 3.0)
        reasons.append(f"Work invested ({history_len} lane transitions)")

    return {
        "lens": "momentum",
        "item_id": item.get("id"),
        "score": round(score, 2),
        "argument": "; ".join(reasons) if reasons else None,
    }


def evaluate_lens_strategic(item, strategic_cfg):
    """Strategic: alignment with declared business priorities and focus areas.

    This is a tribunal-only lens — it does NOT modify the raw priority score.
    Keeps raw scores deterministic and debuggable; strategic influence is visible
    only in tribunal justifications and can be tuned independently.
    """
    score = 0.0
    reasons = []

    current_focus = strategic_cfg.get("current_focus", [])
    high_priority_threshold = strategic_cfg.get("high_priority_threshold", 8)

    # Signal 1: Human explicitly marked high priority
    pw = item.get("priority_weight")
    if pw is not None and pw >= high_priority_threshold:
        score += 5.0
        reasons.append(f"Explicitly high priority ({pw}/10)")

    # Signal 2: Tags matching declared focus areas
    if current_focus:
        item_tags = set(item.get("tags", []))
        focus_set = set(current_focus)
        focus_match = item_tags & focus_set
        if focus_match:
            score += len(focus_match) * 3.0
            reasons.append(f"Matches current focus: {', '.join(sorted(focus_match))}")

    # Signal 3: Category alignment (e.g., bugs during a stability/security focus)
    if current_focus:
        category = item.get("category", "")
        category_focus_map = {
            "bug": {"stability", "quality", "reliability", "security"},
            "debt": {"stability", "quality", "reliability", "maintainability"},
        }
        mapped_themes = category_focus_map.get(category, set())
        theme_match = mapped_themes & focus_set
        if theme_match:
            score += 2.0
            reasons.append(f"{category.title()} aligns with focus: {', '.join(sorted(theme_match))}")

    return {
        "lens": "strategic",
        "item_id": item.get("id"),
        "score": round(score, 2),
        "argument": "; ".join(reasons) if reasons else None,
    }


def evaluate_tribunal(data, agent=None, blocks_map=None, blocked_by=None):
    """Run the tribunal: every lens evaluates every candidate, then aggregate.

    Returns structured verdict with justification and counterfactuals.
    """
    config = data.get("config", {})
    scoring_cfg = {**DEFAULT_SCORING, **config.get("scoring", {})}
    strategic_cfg = {**DEFAULT_STRATEGIC, **config.get("strategic", {})}
    agents_cfg = config.get("agents", {})
    items = data.get("items", [])

    # Only resolve if caller didn't pre-build
    if blocks_map is None or blocked_by is None:
        blocks_map, blocked_by = resolve_blocks(items)

    score_results = compute_scores(data, blocks_map=blocks_map, blocked_by=blocked_by)
    breakdowns_by_id = {r["id"]: r["score_breakdown"] for r in score_results}
    scores_by_id = {r["id"]: r["score"] for r in score_results}
    readiness_by_id = {r["id"]: r.get("readiness", {}) for r in score_results}

    candidates = [i for i in items if i.get("status") in RECOMMEND_ELIGIBLE]
    if not candidates:
        return {"picked": None, "shadow_ranking": [], "lenses": [], "candidates_evaluated": 0}

    
    items_by_id = {i.get("id"): i for i in items}

    # Evaluate every lens for every candidate
    evaluations = {}
    for item in candidates:
        iid = item.get("id")
        breakdown = breakdowns_by_id.get(iid, {})
        evaluations[iid] = {
            "urgency": evaluate_lens_urgency(item, breakdown, scoring_cfg),
            "leverage": evaluate_lens_leverage(item, breakdown, blocks_map),
            "agent_fit": evaluate_lens_agent_fit(item, agent, agents_cfg, items_by_id),
            "risk": evaluate_lens_risk(item, breakdown, blocks_map, items_by_id),
            "momentum": evaluate_lens_momentum(item, breakdown),
            "strategic": evaluate_lens_strategic(item, strategic_cfg),
        }

    # Compute weighted tribunal score per candidate
    tribunal_scores = {}
    for iid, evals in evaluations.items():
        total = sum(e["score"] * LENS_WEIGHTS.get(name, 1.0) for name, e in evals.items())
        tribunal_scores[iid] = round(total, 2)

    ranked = sorted(tribunal_scores.items(), key=lambda x: x[1], reverse=True)
    winner_id = ranked[0][0]
    winner_item = items_by_id[winner_id]
    winner_evals = evaluations[winner_id]

    # Confidence from margin to runner-up
    if len(ranked) >= 2:
        margin = ranked[0][1] - ranked[1][1]
        confidence = "high" if margin > 5 else ("medium" if margin > 2 else "low")
    else:
        confidence = "high"

    # Supporting lenses (non-zero, with arguments)
    supporting = [
        {"lens": name, "argument": e["argument"],
         "weight": round(e["score"] * LENS_WEIGHTS.get(name, 1.0), 2)}
        for name, e in winner_evals.items()
        if e["score"] > 0 and e["argument"]
    ]
    supporting.sort(key=lambda x: x["weight"], reverse=True)

    reasoning = ". ".join(s["argument"] for s in supporting[:3]) if supporting else "Highest overall score"

    agent_fit_result = winner_evals.get("agent_fit", {})
    model_routing = {**DEFAULT_MODEL_ROUTING, **config.get("model_routing", {})}
    rec_model = model_routing.get(winner_item.get("complexity") or "medium", "sonnet")

    status_note = None
    ws = winner_item.get("status", "backlog")
    if ws == "backlog":
        status_note = "Needs refinement before starting"
    elif ws == "refined":
        status_note = "Refined — move to ready before starting"

    winner_readiness = readiness_by_id.get(winner_id, {})
    # If winner has partially-ready blockers, surface as a status note
    winner_blockers = winner_readiness.get("blockers") or []
    if winner_blockers:
        min_r = min(b["readiness"] for b in winner_blockers)
        readiness_cfg = {**DEFAULT_READINESS_CONFIG, **config.get("readiness", {})}
        startable_threshold = readiness_cfg.get("startable_threshold", 0.70)
        ready_threshold = readiness_cfg.get("ready_threshold", 0.90)
        if min_r >= startable_threshold:
            status_note = f"Startable with risk — weakest blocker at {int(min_r * 100)}% readiness"
        else:
            status_note = f"Blocked — weakest blocker at {int(min_r * 100)}% readiness"

    picked = {
        "item_id": winner_id,
        "title": winner_item.get("title", ""),
        "status": ws,
        "score": scores_by_id.get(winner_id, 0),
        "tribunal_score": ranked[0][1],
        "reasoning": reasoning,
        "confidence": confidence,
        "recommended_agent": agent_fit_result.get("recommended_agent"),
        "recommended_model": rec_model,
        "supporting_lenses": supporting,
        "status_note": status_note,
        "readiness": winner_readiness,
    }

    # Shadow ranking — runners-up with "why not" explanations
    shadow = []
    for rid, rscore in ranked[1:5]:
        runner = items_by_id[rid]
        runner_evals = evaluations[rid]

        biggest_gap_lens = max(
            LENS_WEIGHTS,
            key=lambda ln: (winner_evals[ln]["score"] - runner_evals[ln]["score"]) * LENS_WEIGHTS[ln]
        )

        lost_reasons = []
        winner_arg = winner_evals[biggest_gap_lens].get("argument")
        if winner_arg:
            lost_reasons.append(f"Winner stronger on {biggest_gap_lens}: {winner_arg}")

        if breakdowns_by_id.get(rid, {}).get("blocked_penalty", 0) < 0:
            r_ctx = readiness_by_id.get(rid, {})
            blocker_list = r_ctx.get("blockers") or []
            if blocker_list:
                min_r = min(b["readiness"] for b in blocker_list)
                lost_reasons.append(
                    f"Blocked — weakest blocker at {int(min_r * 100)}% readiness"
                )
            else:
                lost_reasons.append("Blocked by incomplete dependency")

        if runner.get("status") != "ready":
            lost_reasons.append(f"Not ready (status: {runner.get('status')})")

        shadow.append({
            "item_id": rid,
            "title": runner.get("title", ""),
            "score": scores_by_id.get(rid, 0),
            "tribunal_score": rscore,
            "lost_on_lens": biggest_gap_lens,
            "lost_reason": "; ".join(lost_reasons) if lost_reasons else "Lower weighted tribunal score",
        })

    # Lens detail for the winner
    lenses = [
        {"lens": name, "argued_for": e["item_id"], "argument": e["argument"],
         "score": e["score"],
         "weighted_score": round(e["score"] * LENS_WEIGHTS.get(name, 1.0), 2)}
        for name, e in winner_evals.items()
    ]

    return {
        "picked": picked,
        "shadow_ranking": shadow,
        "lenses": lenses,
        "candidates_evaluated": len(candidates),
    }


# ── Decision storage ─────────────────────────────────────────────────────────

def generate_decision_id():
    """Generate an 8-char alphanumeric ID for decisions."""
    import random
    import string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


def get_decisions_path(backlog_path):
    """Decisions file lives alongside backlog.json."""
    return os.path.join(os.path.dirname(os.path.abspath(backlog_path)), "decisions.json")


def read_decisions(filepath):
    """Read decisions log."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"decisions": []}


def store_decision(filepath, tribunal_result, agent=None):
    """Store a decision from a tribunal evaluation. Returns decision ID."""
    decisions = read_decisions(filepath)
    decision_id = generate_decision_id()
    decision = {
        "id": decision_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "agent": agent,
        "picked": tribunal_result["picked"],
        "shadow_ranking": tribunal_result["shadow_ranking"],
        "lenses": tribunal_result["lenses"],
        "candidates_evaluated": tribunal_result["candidates_evaluated"],
        "outcome": None,
    }
    decisions["decisions"].append(decision)
    # Cap at 100 decisions
    if len(decisions["decisions"]) > 100:
        decisions["decisions"] = decisions["decisions"][-100:]
    atomic_write(filepath, decisions)
    return decision_id


def record_decision_outcome(filepath, item_id):
    """When an item completes, record the outcome on its most recent decision."""
    decisions = read_decisions(filepath)
    for d in reversed(decisions["decisions"]):
        if d.get("picked") and d["picked"].get("item_id") == item_id and d.get("outcome") is None:
            d["outcome"] = {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "result": "completed",
            }
            atomic_write(filepath, decisions)
            return True
    return False


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


def compute_agent_affinity(item, agent_name, agent_profile, items_by_id, in_progress_per_agent):
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
    #items_by_id = {i.get("id"): i for i in all_items}
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
    in_progress_count = in_progress_per_agent.get(agent_name, 0)

    if in_progress_count >= max_active:
        affinity -= 5

    return affinity


def compute_scores(data, blocks_map=None, blocked_by=None):
    """Compute scores for all items using the Work Intelligence Engine formula.

    Returns list of {id, title, status, score, score_breakdown, readiness,
    recommended_agent, recommended_model}.
    """
    config = data.get("config", {})
    scoring = {**DEFAULT_SCORING, **config.get("scoring", {})}
    readiness_cfg = {**DEFAULT_READINESS_CONFIG, **config.get("readiness", {})}
    agents_cfg = config.get("agents", {})
    model_routing = {**DEFAULT_MODEL_ROUTING, **config.get("model_routing", {})}
    items = data.get("items", [])
    total = len(items)
    if total == 0:
        return []

    if blocks_map is None or blocked_by is None:
        blocks_map, blocked_by = resolve_blocks(items)
    done_or_discarded = {"done", "discarded"}
    items_by_id = {i.get("id"): i for i in items}
    in_progress_per_agent = {
        aname: sum(
            1 for i in items
            if i.get("assigned_to") == aname and i.get("status") == "in-progress"
        )
        for aname in agents_cfg
    }
    startable_threshold = readiness_cfg.get("startable_threshold", 0.70)
    ready_threshold = readiness_cfg.get("ready_threshold", 0.90)

    in_progress_per_agent = {
        aname: sum(
            1 for i in items
            if i.get("assigned_to") == aname and i.get("status") == "in-progress"
        )
        for aname in agents_cfg
    }

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

        # 5. Blocked penalty — dynamic based on blocker readiness (replaces binary -3.0)
        blockers = blocked_by.get(iid, [])
        active_blockers = [
            b for b in blockers
            if items_by_id.get(b, {}).get("status") not in done_or_discarded
        ]
        if active_blockers:
            blocker_readiness_map = {
                b: compute_item_readiness(items_by_id[b], done_or_discarded)["score"]
                for b in active_blockers if b in items_by_id
            }
            min_readiness = min(blocker_readiness_map.values()) if blocker_readiness_map else 0.0
            # Penalty scales from full (blocker at 0%) to zero (blocker fully ready)
            breakdown["blocked_penalty"] = round(
                scoring["blocked_penalty"] * (1.0 - min_readiness), 2
            )
        else:
            blocker_readiness_map = {} 
            min_readiness = 1.0
            breakdown["blocked_penalty"] = 0.0

        # 6. Quick win bonus — only if fully unblocked (no active blockers or all blockers near done)
        is_quick = complexity == "low" and min_readiness >= ready_threshold
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

        # Compute this item's own readiness (for reporting on its own completeness)
        item_readiness = compute_item_readiness(item, done_or_discarded)
        rs = item_readiness["score"]
        readiness_level = (
            "ready" if rs >= ready_threshold
            else ("startable" if rs >= startable_threshold else "not_ready")
        )

        # Blocker readiness context (for blocked items)
        blocker_details = None
        if active_blockers:
            blocker_details = [
                {
                    "blocker_id": b,
                    "readiness": blocker_readiness_map.get(b, 0.0),
                }
                for b in active_blockers
            ]

        # Recommended agent
        best_agent = None
        best_affinity = -float("inf")
        for aname, aprofile in agents_cfg.items():
            aff = compute_agent_affinity(item, aname, aprofile, items_by_id, in_progress_per_agent)
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
            "readiness": {
                "score": item_readiness["score"],
                "level": readiness_level,
                "status_contribution": item_readiness["status_contribution"],
                "signal_contribution": item_readiness["signal_contribution"],
                "signals": item_readiness["signals"],
                "blockers": blocker_details,
            },
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


# ── Phase 4: Natural Language Rule Engine ─────────────────────────────────────

# Structured action types the LLM may return for a firing policy
VALID_POLICY_ACTION_TYPES = {"reprioritize", "reassign", "escalate", "block", "notify", "skip_force"}

# Pairs of action types that are mutually contradictory on the same item
_CONTRADICTORY_PAIRS = {
    tuple(sorted(["escalate", "skip_force"])),
    tuple(sorted(["escalate", "block"])),
    tuple(sorted(["reprioritize", "skip_force"])),
}

# Default policies seeded on first use — express common best-practice rules
DEFAULT_POLICIES = [
    {
        "name": "Jump critical unassigned bugs",
        "description": (
            "If a bug has priority_weight >= 9 and is not assigned to any agent and "
            "has been in backlog or ready for more than 4 hours, escalate it to the top."
        ),
        "priority": 10,
    },
    {
        "name": "Force neglected items",
        "description": (
            "If an item has been skipped 5 or more times without being picked up, "
            "reprioritize it to priority_weight 8 to force a decision: kill it or start it."
        ),
        "priority": 8,
    },
    {
        "name": "Flag concurrent high-complexity work",
        "description": (
            "If two high-complexity items are both in-progress and assigned to the same agent, "
            "notify with a warning that the agent may be overloaded."
        ),
        "priority": 6,
    },
    {
        "name": "Surface stale ready items",
        "description": (
            "If an item has been in 'ready' status for more than 7 days without being picked up, "
            "notify with a warning that it may need to be re-evaluated or discarded."
        ),
        "priority": 5,
    },
]


def get_policies_path(backlog_path):
    """Policies file lives alongside backlog.json."""
    return os.path.join(os.path.dirname(os.path.abspath(backlog_path)), "policies.json")


def get_policy_log_path(backlog_path):
    """Policy audit log lives alongside backlog.json."""
    return os.path.join(os.path.dirname(os.path.abspath(backlog_path)), "policy_log.json")


def generate_policy_id():
    """Generate an 8-char alphanumeric ID for policies and log entries."""
    import random
    import string
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))


def read_policies(filepath):
    """Read policies.json. Seeds default policies if file does not exist."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        now = datetime.now(timezone.utc).isoformat()
        policies = []
        for tmpl in DEFAULT_POLICIES:
            policies.append({
                "id": generate_policy_id(),
                "name": tmpl["name"],
                "description": tmpl["description"],
                "priority": tmpl["priority"],
                "active": True,
                "created_at": now,
                "fire_count": 0,
                "last_fired": None,
            })
        return {"policies": policies}
    except json.JSONDecodeError:
        return {"policies": []}


def save_policies(filepath, data):
    """Atomic write to policies.json."""
    atomic_write(filepath, data)


def read_policy_log(filepath):
    """Read policy_log.json."""
    try:
        with open(filepath, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"entries": []}


def append_policy_log_entry(filepath, entry):
    """Append an entry to policy_log.json, capped at 200 entries."""
    log = read_policy_log(filepath)
    log["entries"].append(entry)
    if len(log["entries"]) > 200:
        log["entries"] = log["entries"][-200:]
    atomic_write(filepath, log)


def build_policy_context(data):
    """Build a compact context snapshot for LLM policy evaluation."""
    score_results = compute_scores(data)
    scores_by_id = {r["id"]: r["score"] for r in score_results}
    readiness_by_id = {r["id"]: r.get("readiness", {}) for r in score_results}
    blocks_map, blocked_by = resolve_blocks(data.get("items", []))

    config = data.get("config", {})
    agents_cfg = config.get("agents", {})
    thresholds = {**DEFAULT_THRESHOLDS, **config.get("thresholds", {})}

    items_ctx = []
    for item in data.get("items", []):
        iid = item.get("id")
        r = readiness_by_id.get(iid, {})
        items_ctx.append({
            "id": iid,
            "title": item.get("title", ""),
            "status": item.get("status", "backlog"),
            "category": item.get("category"),
            "complexity": item.get("complexity"),
            "priority_weight": item.get("priority_weight"),
            "assigned_to": item.get("assigned_to"),
            "tags": item.get("tags", []),
            "score": round(scores_by_id.get(iid, 0), 2),
            "readiness": round(r.get("score", 0), 2),
            "readiness_level": r.get("level", "not_ready"),
            "blocks_count": len(blocks_map.get(iid, [])),
            "blocked_by_count": len(blocked_by.get(iid, [])),
            "skip_count": item.get("skip_count", 0),
            "reopen_count": item.get("reopen_count", 0),
            "created_at": item.get("created_at", ""),
            "updated_at": item.get("updated_at", ""),
        })

    agents_ctx = {}
    for aname, aprofile in agents_cfg.items():
        in_progress_count = sum(
            1 for i in data.get("items", [])
            if i.get("assigned_to") == aname and i.get("status") == "in-progress"
        )
        agents_ctx[aname] = {
            "current_load": in_progress_count,
            "max_active": aprofile.get("max_active", thresholds.get("max_active_per_agent", 3)),
            "skills": aprofile.get("skills", []),
        }

    return {
        "items": items_ctx,
        "agents": agents_ctx,
        "current_time": datetime.now(timezone.utc).isoformat(),
    }


def _call_llm(model, messages, max_tokens=512):
    """Low-level Claude API call. Returns response text or raises."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic SDK not installed")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
    return response.content[0].text.strip()


def _extract_json(text):
    """Extract JSON from an LLM response that may be wrapped in code fences."""
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    return json.loads(text)


def evaluate_policy_with_llm(policy, context):
    """Evaluate a single policy against the current backlog context using Claude.

    Returns: {"fires": bool, "reasoning": str, "actions": [ActionObject]}
    Falls back to fires=False with an explanation if the SDK is unavailable.
    """
    items_lines = "\n".join(
        f"  - [{i['status']}] {i['title']} (id={i['id']}, score={i['score']}, "
        f"category={i['category']}, priority={i['priority_weight']}, "
        f"assigned={i['assigned_to']}, blocks={i['blocks_count']}, "
        f"skip={i['skip_count']}, reopen={i['reopen_count']}, "
        f"readiness={i['readiness']}, created={i['created_at'][:10] if i['created_at'] else 'unknown'})"
        for i in context["items"]
    )
    agents_lines = "\n".join(
        f"  - {name}: load={info['current_load']}/{info['max_active']}, skills={info['skills']}"
        for name, info in context["agents"].items()
    ) if context["agents"] else "  (no configured agents)"

    prompt = (
        "You are the Flow Work Intelligence rule engine. Evaluate if a policy fires.\n\n"
        f"POLICY:\nName: {policy['name']}\nDescription: {policy['description']}\n\n"
        f"CURRENT STATE (as of {context['current_time']}):\nItems:\n"
        f"{items_lines if items_lines else '  (no items)'}\n\nAgents:\n{agents_lines}\n\n"
        "Determine if the policy condition is clearly met by the current data.\n\n"
        "If the policy fires, produce one or more structured actions:\n"
        '- reprioritize: {"type":"reprioritize","item_id":"<id>","priority_weight":<1-10>,"reason":"<one sentence>"}\n'
        '- reassign: {"type":"reassign","item_id":"<id>","agent":"<name>","reason":"<one sentence>"}\n'
        '- escalate: {"type":"escalate","item_id":"<id>","reason":"<one sentence>"}\n'
        '- block: {"type":"block","item_id":"<id>","reason":"<one sentence>"}\n'
        '- notify: {"type":"notify","item_id":"<id or null>","message":"<alert>","severity":"info|warning|critical"}\n'
        '- skip_force: {"type":"skip_force","item_id":"<id>","reason":"<one sentence>"}\n\n'
        "Rules: Only fire if the condition is clearly met. Never guess. "
        "item_id must be a real ID from the list above.\n\n"
        "Respond ONLY with valid JSON:\n"
        '{"fires":true|false,"reasoning":"1-3 sentences","actions":[/* empty if fires=false */]}'
    )

    try:
        text = _call_llm("claude-haiku-4-5-20251001", [{"role": "user", "content": prompt}], max_tokens=512)
        result = _extract_json(text)
        if not isinstance(result.get("fires"), bool):
            return {"fires": False, "reasoning": "Invalid LLM response", "actions": []}
        if not isinstance(result.get("actions"), list):
            result["actions"] = []
        # Validate action types
        result["actions"] = [
            a for a in result["actions"]
            if isinstance(a, dict) and a.get("type") in VALID_POLICY_ACTION_TYPES
        ]
        return result
    except RuntimeError as e:
        return {"fires": False, "reasoning": str(e), "actions": []}
    except Exception as e:
        return {"fires": False, "reasoning": f"Evaluation error: {e}", "actions": []}


def detect_policy_conflicts(fired_policies):
    """Find contradictory actions across fired policies targeting the same item.

    Returns {"conflicts": [...], "clean": [action_entry, ...]}.
    """
    by_item = {}
    for fp in fired_policies:
        for action in fp["actions"]:
            iid = action.get("item_id") or "__global__"
            by_item.setdefault(iid, []).append({
                "action": action,
                "policy_id": fp["policy_id"],
                "policy_name": fp["policy_name"],
            })

    conflicts = []
    clean = []
    for iid, entries in by_item.items():
        types = [e["action"]["type"] for e in entries]
        has_conflict = any(
            tuple(sorted([types[i], types[j]])) in _CONTRADICTORY_PAIRS
            for i in range(len(types))
            for j in range(i + 1, len(types))
        )
        if has_conflict:
            conflicts.append({
                "item_id": iid,
                "actions": [e["action"] for e in entries],
                "policy_names": [e["policy_name"] for e in entries],
                "entries": entries,
            })
        else:
            clean.extend(entries)
    return {"conflicts": conflicts, "clean": clean}


def resolve_conflicts_with_llm(conflicts, context):
    """For each conflict, call Claude to adjudicate which action wins.
    Falls back to first-policy-wins if LLM is unavailable.

    Returns list of {"action": ..., "resolution_reasoning": ..., "conflict_item_id": ...}.
    """
    resolved = []
    for conflict in conflicts:
        iid = conflict["item_id"]
        item = next((i for i in context["items"] if i.get("id") == iid), {})
        actions_text = "\n".join(
            f"  Policy '{e['policy_name']}': {json.dumps(e['action'])}"
            for e in conflict["entries"]
        )
        prompt = (
            "Two policies conflict on the same item. Adjudicate which action should win.\n\n"
            f"Item: {json.dumps(item)}\n\nConflicting actions:\n{actions_text}\n\n"
            "Choose ONE winning action and explain why. Respond ONLY with JSON:\n"
            '{"winning_action":{/* the winning action object */},'
            '"reasoning":"one sentence explaining why this action wins"}'
        )
        try:
            text = _call_llm("claude-haiku-4-5-20251001", [{"role": "user", "content": prompt}], max_tokens=256)
            result = _extract_json(text)
            resolved.append({
                "action": result.get("winning_action", conflict["entries"][0]["action"]),
                "resolution_reasoning": result.get("reasoning", "LLM adjudication"),
                "conflict_item_id": iid,
            })
        except Exception:
            resolved.append({
                "action": conflict["entries"][0]["action"],
                "resolution_reasoning": "Auto-resolved: first policy wins",
                "conflict_item_id": iid,
            })
    return resolved


def execute_policy_actions(action_entries, data):
    """Execute action entries against backlog data in place.

    action_entries: list of {"action": {...}, "policy_name": ..., "policy_id": ...}
    Returns list of execution result dicts.
    """
    items_by_id = {i.get("id"): i for i in data.get("items", [])}
    now = datetime.now(timezone.utc).isoformat()
    results = []

    for entry in action_entries:
        action = entry["action"]
        atype = action.get("type")
        iid = action.get("item_id")
        item = items_by_id.get(iid) if iid and iid != "__global__" else None

        try:
            if atype == "reprioritize" and item is not None:
                old_pw = item.get("priority_weight")
                item["priority_weight"] = action.get("priority_weight")
                item["updated_at"] = now
                results.append({"action": action, "status": "executed",
                                 "detail": f"priority_weight {old_pw} → {action.get('priority_weight')}"})

            elif atype == "reassign" and item is not None:
                old_agent = item.get("assigned_to")
                item["assigned_to"] = action.get("agent")
                item["updated_at"] = now
                results.append({"action": action, "status": "executed",
                                 "detail": f"assigned_to {old_agent} → {action.get('agent')}"})

            elif atype == "escalate" and item is not None:
                old_pw = item.get("priority_weight")
                item["priority_weight"] = max(item.get("priority_weight") or 0, 9)
                if not item.get("category"):
                    item["category"] = "bug"
                item["updated_at"] = now
                results.append({"action": action, "status": "executed",
                                 "detail": f"escalated priority_weight {old_pw} → {item['priority_weight']}"})

            elif atype == "block" and item is not None:
                thread = {
                    "topic": f"Policy block: {action.get('reason', 'Policy rule triggered')}",
                    "resolved": False,
                    "waiting_on": "user",
                    "created_at": now,
                    "thread": [{"role": "agent",
                                 "message": f"Blocked by policy '{entry.get('policy_name','')}': "
                                            f"{action.get('reason','')}",
                                 "at": now}],
                }
                item.setdefault("threads", []).append(thread)
                item["updated_at"] = now
                results.append({"action": action, "status": "executed", "detail": "block thread added"})

            elif atype == "notify":
                results.append({"action": action, "status": "notified",
                                 "detail": action.get("message", "")})

            elif atype == "skip_force" and item is not None:
                item["skip_count"] = item.get("skip_count", 0) + 1
                item["updated_at"] = now
                results.append({"action": action, "status": "executed",
                                 "detail": f"skip_count → {item['skip_count']}"})

            else:
                results.append({"action": action, "status": "skipped",
                                 "detail": "item not found or unknown action type"})
        except Exception as exc:
            results.append({"action": action, "status": "error", "detail": str(exc)})

    return results


def run_policy_engine(data, backlog_path, trigger_event=None):
    """Full policy evaluation pipeline.

    1. Load active policies
    2. Build context snapshot
    3. Evaluate each policy with LLM
    4. Detect + resolve conflicts
    5. Execute non-conflicting actions (mutates data in place)
    6. Update fire counts + log

    Returns summary dict.
    """
    policies_file = get_policies_path(backlog_path)
    policies_data = read_policies(policies_file)
    active = [p for p in policies_data.get("policies", []) if p.get("active", True)]
    if not active:
        return {"fires": 0, "actions_executed": 0, "notifications": [], "log_ids": []}

    context = build_policy_context(data)
    fired = []
    log_entries = []
    now = datetime.now(timezone.utc).isoformat()

    for policy in sorted(active, key=lambda p: p.get("priority", 5), reverse=True):
        result = evaluate_policy_with_llm(policy, context)
        entry = {
            "id": generate_policy_id(),
            "policy_id": policy["id"],
            "policy_name": policy["name"],
            "timestamp": now,
            "trigger_event": trigger_event,
            "fired": result.get("fires", False),
            "reasoning": result.get("reasoning", ""),
            "actions_proposed": result.get("actions", []),
            "conflict_with": None,
            "resolution_reasoning": None,
            "actions_executed": [],
            "notifications": [],
        }
        if result.get("fires"):
            fired.append({
                "policy_id": policy["id"],
                "policy_name": policy["name"],
                "actions": result.get("actions", []),
            })
        log_entries.append(entry)

    if not fired:
        log_path = get_policy_log_path(backlog_path)
        for entry in log_entries:
            append_policy_log_entry(log_path, entry)
        return {"fires": 0, "actions_executed": 0, "notifications": [], "log_ids": [e["id"] for e in log_entries]}

    conflict_result = detect_policy_conflicts(fired)
    clean_actions = conflict_result["clean"]
    conflicts = conflict_result["conflicts"]

    resolved_actions = []
    if conflicts:
        resolved = resolve_conflicts_with_llm(conflicts, context)
        for res in resolved:
            resolved_actions.append({
                "action": res["action"],
                "policy_name": "conflict-resolved",
                "policy_id": "conflict-resolved",
            })
            # Annotate log entries involved in this conflict
            conflict_names = set()
            for c in conflicts:
                if c["item_id"] == res.get("conflict_item_id"):
                    conflict_names = set(c["policy_names"])
            for entry in log_entries:
                if entry["policy_name"] in conflict_names and entry["fired"]:
                    entry["conflict_with"] = list(conflict_names - {entry["policy_name"]})
                    entry["resolution_reasoning"] = res.get("resolution_reasoning")

    all_actions = list(clean_actions) + resolved_actions
    execution_results = execute_policy_actions(all_actions, data)

    notifications = [r["action"].get("message", "") for r in execution_results
                     if r["action"].get("type") == "notify"]
    executed_count = sum(1 for r in execution_results if r["status"] == "executed")

    # Update policy fire counts
    now_ts = datetime.now(timezone.utc).isoformat()
    for fp in fired:
        for policy in policies_data.get("policies", []):
            if policy["id"] == fp["policy_id"]:
                policy["fire_count"] = policy.get("fire_count", 0) + 1
                policy["last_fired"] = now_ts
    save_policies(policies_file, policies_data)

    # Annotate log entries with execution results
    for entry in log_entries:
        if entry["fired"]:
            entry["actions_executed"] = [
                {"action": r["action"], "status": r["status"], "detail": r.get("detail", "")}
                for r in execution_results if r["status"] == "executed"
            ]
            entry["notifications"] = notifications

    log_path = get_policy_log_path(backlog_path)
    for entry in log_entries:
        append_policy_log_entry(log_path, entry)

    return {
        "fires": len(fired),
        "actions_executed": executed_count,
        "notifications": notifications,
        "log_ids": [e["id"] for e in log_entries],
    }


def compute_policy_suggestions(data, backlog_path):
    """Analyse backlog patterns and suggest new natural language policies via Claude."""
    context = build_policy_context(data)
    policies_file = get_policies_path(backlog_path)
    policies_data = read_policies(policies_file)
    existing_descriptions = [p["description"] for p in policies_data.get("policies", [])]

    patterns = []
    items = context["items"]

    high_skip = [i for i in items if i.get("skip_count", 0) >= 3]
    if high_skip:
        patterns.append(f"{len(high_skip)} item(s) skipped 3+ times: "
                        f"{', '.join(i['title'] for i in high_skip[:3])}")

    critical_unassigned = [
        i for i in items
        if i.get("category") == "bug" and (i.get("priority_weight") or 0) >= 9
        and not i.get("assigned_to")
    ]
    if critical_unassigned:
        patterns.append(f"{len(critical_unassigned)} critical bug(s) unassigned")

    heavily_blocked = [i for i in items if i.get("blocked_by_count", 0) >= 2]
    if heavily_blocked:
        patterns.append(f"{len(heavily_blocked)} item(s) blocked by 2+ dependencies")

    for aname, ainfo in context["agents"].items():
        if ainfo["current_load"] >= ainfo["max_active"]:
            patterns.append(f"Agent {aname} at max capacity "
                            f"({ainfo['current_load']}/{ainfo['max_active']})")

    if not patterns:
        patterns.append("No significant patterns detected — suggest general best-practice rules")

    existing_text = "\n".join(f"- {d}" for d in existing_descriptions) if existing_descriptions else "None"
    patterns_text = "\n".join(f"- {p}" for p in patterns)

    prompt = (
        "You are the Flow Work Intelligence advisor. Suggest natural language policies "
        "for the rule engine based on observed backlog patterns.\n\n"
        f"Patterns observed:\n{patterns_text}\n\n"
        f"Existing policies (do not suggest duplicates):\n{existing_text}\n\n"
        "Suggest 2-4 natural language policies. Each should be a clear, actionable rule "
        "that Flow can evaluate with LLM reasoning. Examples:\n"
        "- If a bug is blocking more than 2 active items and was filed in the last 6 hours, jump it to the top.\n"
        "- Never assign two high-complexity items to the same agent simultaneously.\n\n"
        "Respond ONLY with a JSON array:\n"
        '[{"name":"Short name","description":"Full natural language rule","priority":5}]'
    )

    try:
        text = _call_llm("claude-sonnet-4-6", [{"role": "user", "content": prompt}], max_tokens=600)
        suggestions = _extract_json(text)
        if isinstance(suggestions, list):
            return [s for s in suggestions if isinstance(s, dict) and "name" in s and "description" in s]
    except Exception:
        pass
    return []


def get_policy_influences_for_item(backlog_path, item_id):
    """Return recent policy actions that influenced this item (for tribunal enrichment)."""
    log_path = get_policy_log_path(backlog_path)
    log = read_policy_log(log_path)
    influences = []
    for entry in reversed(log.get("entries", [])[-50:]):
        if not entry.get("fired"):
            continue
        for action in entry.get("actions_proposed", []):
            if action.get("item_id") == item_id and action.get("type") in ("reprioritize", "escalate"):
                influences.append({
                    "policy": entry["policy_name"],
                    "action": action["type"],
                    "reason": action.get("reason", ""),
                    "at": entry["timestamp"],
                })
    return influences


def compute_policy_staleness(policies_data):
    """Flag policies that may be redundant or no longer relevant."""
    now = datetime.now(timezone.utc)
    stale = []
    for policy in policies_data.get("policies", []):
        if not policy.get("active"):
            continue
        created_at_str = policy.get("created_at", "")
        last_fired_str = policy.get("last_fired")
        fire_count = policy.get("fire_count", 0)

        warnings = []
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            age_days = (now - created_at).days
            if fire_count == 0 and age_days >= 14:
                warnings.append(f"Never fired in {age_days} days — may be redundant")
        except (ValueError, AttributeError):
            pass

        if last_fired_str:
            try:
                last_fired = datetime.fromisoformat(last_fired_str.replace("Z", "+00:00"))
                days_since = (now - last_fired).days
                if days_since >= 21:
                    warnings.append(f"Hasn't fired in {days_since} days — still relevant?")
            except (ValueError, AttributeError):
                pass

        if warnings:
            stale.append({"policy_id": policy["id"], "policy_name": policy["name"], "warnings": warnings})
    return stale


def compute_pulse_policy_summary(backlog_path, recommendation):
    """Build policy intelligence summary for the pulse response.

    Read-only — surfaces what the policy engine has already done so agents
    don't need separate calls to /api/policies, /api/policies/log, etc.
    """
    policies_file = get_policies_path(backlog_path)
    policies_data = read_policies(policies_file)
    active_policies = [p for p in policies_data.get("policies", []) if p.get("active", True)]
    stale = compute_policy_staleness(policies_data)

    # Recent fires from log
    log_path = get_policy_log_path(backlog_path)
    log = read_policy_log(log_path)
    recent_entries = log.get("entries", [])[-20:]
    recent_fires = []
    notifications = []
    for entry in reversed(recent_entries):
        if entry.get("fired"):
            recent_fires.append({
                "policy": entry["policy_name"],
                "reasoning": entry.get("reasoning", ""),
                "actions": [a.get("type") for a in entry.get("actions_proposed", [])],
                "at": entry["timestamp"],
            })
        for n in entry.get("notifications", []):
            if n:
                notifications.append({
                    "policy": entry["policy_name"],
                    "message": n,
                    "at": entry["timestamp"],
                })

    # Policy influences on the recommended item
    influences = []
    if recommendation.get("picked"):
        winner_id = recommendation["picked"].get("item_id")
        if winner_id:
            influences = get_policy_influences_for_item(backlog_path, winner_id)

    return {
        "active_count": len(active_policies),
        "recent_fires": recent_fires[:5],
        "notifications": notifications[:5],
        "influences_on_pick": influences,
        "stale_warnings": stale,
    }


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
        elif parsed.path == "/api/recommend":
            params = parse_qs(parsed.query)
            agent = params.get("agent", [None])[0]
            commit = params.get("commit", ["false"])[0].lower() == "true"
            self._serve_recommend(agent, commit)
        elif parsed.path == "/api/decisions":
            self._serve_decisions()
        elif parsed.path == "/api/agents":
            self._serve_agents()
        elif parsed.path == "/api/graph":
            self._serve_graph()
        elif parsed.path == "/api/pulse":
            params = parse_qs(parsed.query)
            agent = params.get("agent", [None])[0]
            self._serve_pulse(agent)
        elif parsed.path == "/api/user":
            self._serve_user()
        # ── Phase 4: Policy Engine ──────────────────────────
        elif parsed.path == "/api/policies":
            self._serve_policies()
        elif parsed.path == "/api/policies/log":
            params = parse_qs(parsed.query)
            limit = int(params.get("limit", [50])[0])
            self._serve_policy_log(limit)
        elif parsed.path == "/api/policies/evaluate":
            self._serve_policy_evaluate()
        elif parsed.path == "/api/policies/suggestions":
            self._serve_policy_suggestions()
        else:
            self.send_error(404)

    def do_PUT(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/backlog":
            self._save_backlog()
        elif parsed.path.startswith("/api/items/"):
            item_id = parsed.path.split("/api/items/")[1]
            self._update_item(item_id)
        # ── Phase 4: Policy Engine ──────────────────────────
        elif parsed.path.startswith("/api/policies/"):
            policy_id = parsed.path.split("/api/policies/")[1]
            self._update_policy(policy_id)
        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        # POST /api/items/<id>/signal
        if parsed.path.startswith("/api/items/") and parsed.path.endswith("/signal"):
            parts = parsed.path.split("/")
            # path: ['', 'api', 'items', '<id>', 'signal']
            if len(parts) == 5:
                item_id = parts[3]
                self._add_signal(item_id)
                return
        # POST /api/policies — create new policy
        if parsed.path == "/api/policies":
            self._create_policy()
            return
        self.send_error(404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/policies/"):
            policy_id = parsed.path.split("/api/policies/")[1]
            self._delete_policy(policy_id)
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

    def _serve_graph(self):
        """Dependency graph with readiness, critical path, conflicts, and rebalancing."""
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return
        graph = compute_dependency_graph(data)
        self._json_response(200, graph)

    def _serve_pulse(self, agent=None):
        """Proactive push pulse — recommendation + readiness + conflicts + coordination + policies."""
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return
        pulse = compute_pulse(data, agent_name=agent, backlog_path=self.backlog_file)
        self._json_response(200, pulse)

    def _serve_recommend(self, agent=None, commit=False):
        """Tribunal evaluation — returns justified recommendation.
        With ?commit=true, stores the decision for outcome tracking."""
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return
        result = evaluate_tribunal(data, agent=agent)
        # Enrich winner with any recent policy influences
        if result.get("picked"):
            winner_id = result["picked"].get("item_id")
            if winner_id:
                influences = get_policy_influences_for_item(self.backlog_file, winner_id)
                if influences:
                    result["picked"]["policy_influences"] = influences
        if commit and result.get("picked"):
            decisions_file = get_decisions_path(self.backlog_file)
            decision_id = store_decision(decisions_file, result, agent)
            result["decision_id"] = decision_id
        self._json_response(200, result)

    # ── Phase 4: Policy Engine handlers ───────────────────────────────────────

    def _serve_policies(self):
        """Return all policies with staleness analysis."""
        policies_file = get_policies_path(self.backlog_file)
        policies_data = read_policies(policies_file)
        stale = compute_policy_staleness(policies_data)
        stale_by_id = {s["policy_id"]: s["warnings"] for s in stale}
        for p in policies_data.get("policies", []):
            p["staleness_warnings"] = stale_by_id.get(p["id"], [])
        self._json_response(200, policies_data)

    def _create_policy(self):
        """POST /api/policies — create a new policy."""
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

        name = incoming.get("name", "").strip()
        description = incoming.get("description", "").strip()
        if not name or not description:
            self._json_error(400, "name and description are required")
            return

        now = datetime.now(timezone.utc).isoformat()
        policy = {
            "id": generate_policy_id(),
            "name": name,
            "description": description,
            "priority": int(incoming.get("priority", 5)),
            "active": bool(incoming.get("active", True)),
            "created_at": now,
            "fire_count": 0,
            "last_fired": None,
        }

        policies_file = get_policies_path(self.backlog_file)
        policies_data = read_policies(policies_file)
        policies_data["policies"].append(policy)
        save_policies(policies_file, policies_data)
        self._json_response(201, {"status": "ok", "policy": policy})

    def _update_policy(self, policy_id):
        """PUT /api/policies/<id> — update name, description, priority, active."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            self._json_error(413, "Request body too large")
            return
        body = self.rfile.read(content_length)
        try:
            updates = json.loads(body)
        except json.JSONDecodeError as e:
            self._json_error(400, f"Invalid JSON: {e}")
            return

        policies_file = get_policies_path(self.backlog_file)
        policies_data = read_policies(policies_file)
        found = False
        for policy in policies_data.get("policies", []):
            if policy["id"] == policy_id:
                for field in ("name", "description", "priority", "active"):
                    if field in updates:
                        policy[field] = updates[field]
                found = True
                break

        if not found:
            self._json_error(404, f"Policy {policy_id} not found")
            return

        save_policies(policies_file, policies_data)
        self._json_response(200, {"status": "ok"})

    def _delete_policy(self, policy_id):
        """DELETE /api/policies/<id> — remove a policy."""
        policies_file = get_policies_path(self.backlog_file)
        policies_data = read_policies(policies_file)
        before = len(policies_data.get("policies", []))
        policies_data["policies"] = [
            p for p in policies_data.get("policies", []) if p["id"] != policy_id
        ]
        if len(policies_data["policies"]) == before:
            self._json_error(404, f"Policy {policy_id} not found")
            return
        save_policies(policies_file, policies_data)
        self._json_response(200, {"status": "ok"})

    def _serve_policy_log(self, limit=50):
        """GET /api/policies/log — return recent policy fire history."""
        log_path = get_policy_log_path(self.backlog_file)
        log = read_policy_log(log_path)
        entries = log.get("entries", [])
        # Return most recent first
        self._json_response(200, {"entries": list(reversed(entries[-limit:]))})

    def _serve_policy_evaluate(self):
        """GET /api/policies/evaluate — manually trigger policy evaluation and return results."""
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return

        with write_lock:
            try:
                data = read_backlog(self.backlog_file)
            except ValueError as e:
                self._json_error(500, str(e))
                return
            result = run_policy_engine(data, self.backlog_file, trigger_event="manual")
            if result["actions_executed"] > 0:
                data["version"] = data.get("version", 0) + 1
                atomic_write(self.backlog_file, data)

        self._json_response(200, result)

    def _serve_policy_suggestions(self):
        """GET /api/policies/suggestions — LLM-generated rule suggestions based on patterns."""
        try:
            data = read_backlog(self.backlog_file)
        except ValueError as e:
            self._json_error(500, str(e))
            return
        suggestions = compute_policy_suggestions(data, self.backlog_file)
        self._json_response(200, {"suggestions": suggestions})

    def _trigger_policy_engine_background(self, trigger_event=None):
        """Fire policy evaluation in a background thread after a write completes."""
        backlog_file = self.backlog_file

        def run():
            try:
                with write_lock:
                    data = read_backlog(backlog_file)
                    result = run_policy_engine(data, backlog_file, trigger_event=trigger_event)
                    if result["actions_executed"] > 0:
                        data["version"] = data.get("version", 0) + 1
                        atomic_write(backlog_file, data)
            except Exception:
                pass  # Policy engine failures must never affect core backlog

        t = threading.Thread(target=run, daemon=True)
        t.start()

    def _serve_decisions(self):
        """Return stored decisions for review."""
        decisions_file = get_decisions_path(self.backlog_file)
        decisions = read_decisions(decisions_file)
        self._json_response(200, decisions)

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
            changed_ids = set()
            for item in incoming.get("items", []):
                old_item = current_items.get(item.get("id"))
                if old_item and item.get("status") != old_item.get("status"):
                    ok, err = validate_lane_transition(old_item, item.get("status"), statuses)
                    if not ok:
                        self._json_error(422, err)
                        return
                    # Server-side enforcement: append lane_history + set watermark
                    enforce_lane_history(old_item, item, statuses)
                    changed_ids.add(item.get("id"))
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

            # Record outcomes for items that just moved to done
            decisions_file = get_decisions_path(self.backlog_file)
            current_items_map = {i.get("id"): i for i in current.get("items", [])}
            for item in incoming.get("items", []):
                iid = item.get("id")
                old = current_items_map.get(iid)
                if old and old.get("status") != "done" and item.get("status") == "done":
                    record_decision_outcome(decisions_file, iid)

        response = {"status": "ok", "version": incoming["version"]}
        # Return authoritative item data for items that had status changes so
        # the client can sync lane_history / gate_from without a second round-trip
        if changed_ids:
            response["updated_items"] = [
                i for i in incoming.get("items", []) if i.get("id") in changed_ids
            ]
        if events:
            response["_events"] = events
        self._json_response(200, response)
        # Fire policy engine asynchronously — must not block the write response
        trigger = events[0]["type"] if events else "write"
        self._trigger_policy_engine_background(trigger_event=trigger)

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

            # Record outcome if item just moved to done
            if old_snapshot.get("status") != "done" and data["items"][found_idx].get("status") == "done":
                decisions_file = get_decisions_path(self.backlog_file)
                record_decision_outcome(decisions_file, item_id)

        response = {"status": "ok", "version": data["version"]}
        if events:
            response["_events"] = events
        self._json_response(200, response)
        # Fire policy engine asynchronously
        trigger = events[0]["type"] if events else "item_update"
        self._trigger_policy_engine_background(trigger_event=trigger)

    def _add_signal(self, item_id):
        """Append a readiness signal to an item's readiness_signals array.

        Body: {"type": "pr_merged", "source": "agent-a", "description": "optional"}
        Valid types: pr_merged, test_passed, review_approved, file_created, spec_written, design_approved
        """
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            self._json_error(413, "Request body too large")
            return
        body = self.rfile.read(content_length)
        try:
            signal_data = json.loads(body)
        except json.JSONDecodeError as e:
            self._json_error(400, f"Invalid JSON: {e}")
            return

        signal_type = signal_data.get("type", "")
        if signal_type not in VALID_SIGNAL_TYPES:
            self._json_error(400, f"Invalid signal type '{signal_type}'. "
                                  f"Valid types: {', '.join(sorted(VALID_SIGNAL_TYPES))}")
            return

        signal = {
            "type": signal_type,
            "source": signal_data.get("source", "unknown"),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if signal_data.get("description"):
            signal["description"] = signal_data["description"]

        with write_lock:
            try:
                data = read_backlog(self.backlog_file)
            except ValueError as e:
                self._json_error(500, str(e))
                return

            found = False
            for item in data.get("items", []):
                if item.get("id") == item_id:
                    if "readiness_signals" not in item:
                        item["readiness_signals"] = []
                    item["readiness_signals"].append(signal)
                    item["updated_at"] = datetime.now(timezone.utc).isoformat()
                    found = True
                    break

            if not found:
                self._json_error(404, f"Item {item_id} not found")
                return

            data["version"] = data.get("version", 0) + 1
            atomic_write(self.backlog_file, data)

        self._json_response(201, {"status": "ok", "signal": signal, "version": data["version"]})

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

    # Report agent file discovery
    project_root = str(Path(BacklogHandler.backlog_file).parent)
    file_agents = parse_agent_files(project_root)

    print(f"Backlog board: {url}")
    print(f"Reading from:  {BacklogHandler.backlog_file}")
    if file_agents:
        print(f"Agent files:   .claude/agents/ ({len(file_agents)} agents: {', '.join(file_agents.keys())})")
    else:
        print(f"Agent files:   none found (will use config.agents fallback)")
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
