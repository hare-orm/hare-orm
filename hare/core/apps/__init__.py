"""The registry of the models of every app, and how it is filled: models discovered, their relations
linked, swappable models resolved, table names checked."""

from __future__ import annotations

from hare.core.apps.apps import Apps

__all__ = ["Apps"]
