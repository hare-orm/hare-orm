"""A lightweight override of the currently-active HareContext's use_timezone/timezone settings.

Timezone.get_use_timezone()/name() read whichever HareContext is currently active (a contextvar, so
naturally isolated per task) instead of a process-global os.environ var - tests that need a
specific timezone config no longer mutate os.environ, they enter a temporary child context with
different timezone settings instead. This reuses the current context's own connections/apps/
router (no new database connection, no schema regeneration) when one is active; with none
active, it's a bare timezone-only context suitable for tests that call Timezone.* directly
without ever touching a database.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from hare.core.hare_context import HareContext
from hare.time.constants import DEFAULT_TIMEZONE


@contextmanager
def override_timezone(use_timezone: bool, timezone: str = DEFAULT_TIMEZONE) -> Generator[None]:
    parent = HareContext.get_current()
    ctx = HareContext()
    if parent is not None:
        ctx._connections = parent._connections
        ctx._apps = parent._apps
        ctx._default_connection = parent._default_connection
        ctx._table_name_generator = parent._table_name_generator
        ctx._router = parent._router
        ctx._routers = parent._routers
        ctx._inited = parent._inited
    ctx._init_timezone(use_timezone, timezone)
    with ctx:
        yield
