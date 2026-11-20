"""Temporarily configure the selected test application without changing query ownership.

Tests run sequentially within their application. Existing bound queries must retain that
application; sharing its Apps facade through another HareContext would give cached managers
and retained querysets conflicting owners. Concurrent task-local zone overrides use the
public ZoneSettings.override() directly.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from hare.core.hare_context import HareContext
from hare.time.constants import DEFAULT_TIMEZONE
from hare.time.zone_settings import ZoneSettings


@contextmanager
def override_timezone(use_timezone: bool, timezone: str = DEFAULT_TIMEZONE) -> Generator[None]:
    context = HareContext.get_current()
    if context is None:
        with HareContext() as context:
            context._init_timezone(use_timezone, timezone)
            yield
        return
    previous = context._use_timezone, context._timezone
    try:
        context._init_timezone(use_timezone, timezone)
        with ZoneSettings.override(timezone):
            yield
    finally:
        context._init_timezone(*previous)
