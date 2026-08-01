"""Admission-rejection exceptions raised by `Scheduler` implementations.

Distinguished by type (not string matching) wherever they're caught, e.g.
`app/main.py`'s `proxy_handler`.
"""

from __future__ import annotations


class QueueFullError(Exception):
    """FR-033 — the backend's queue is already at `queue_max_size`."""


class QueueWaitExceededError(Exception):
    """FR-033a — projected wait already exceeds `queue_timeout_seconds`,
    independent of current queue depth."""
