"""The ASGI middleware ``HareFastAPI`` adds: a request's own state (its count of repeated queries,
its writes) and, when
``atomic_requests`` is on, a transaction around each request."""

from __future__ import annotations

__all__ = []
