"""Checks that the values a request gives a field's bound parameters leave any value between them:
``?published_at__gte=`` not after ``?published_at__lte=``, a ``range`` starting before it ends."""

from __future__ import annotations

__all__ = []
