"""Shared exceptions for the backlog package.

CLI maps these to exit codes:
  GateViolationError  → exit 1
  ItemNotFoundError   → exit 1
  ConflictError       → exit 2

Server maps these to HTTP status codes:
  GateViolationError  → 422
  ItemNotFoundError   → 404
  ConflictError       → 409
"""


class GateViolationError(Exception):
    """Raised when a forward move is blocked by unsatisfied gate requirements."""


class ConflictError(Exception):
    """Raised when a write is rejected due to a version mismatch (optimistic locking)."""


class ItemNotFoundError(Exception):
    """Raised when a position number does not resolve to a backlog item."""
