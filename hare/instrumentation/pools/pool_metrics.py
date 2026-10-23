from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar


class PoolMetrics:
    """Whether the pools of connections measure how long each wait for a connection takes - the
    ``db.client.connection.wait_time`` metric and the ``AcquireWaitTime`` health criterion need it.
    Off by default: taking a connection then costs no call at all. Each ``enable()`` is matched by a
    ``disable()``; the measuring stops once every user disabled it.
    """

    #: Whether the waits are measured.
    enabled: ClassVar[bool] = False
    #: How many users enabled the measuring.
    user_count: ClassVar[int] = 0
    #: What each driver measuring outside Python (the Rust one) is told when the measuring starts or
    #: stops.
    switches: ClassVar[list[Callable[[bool], None]]] = []

    @classmethod
    def enable(cls) -> None:
        """Starts measuring - for one more user."""
        cls.user_count += 1
        cls.set_enabled(True)

    @classmethod
    def disable(cls) -> None:
        """Stops measuring for one user - for good once no user is left."""
        cls.user_count = max(0, cls.user_count - 1)
        cls.set_enabled(cls.user_count > 0)

    @classmethod
    def add_switch(cls, switch: Callable[[bool], None]) -> None:
        """Registers what a driver measuring outside Python is told - told the current state right away.

        Args:
            switch: Called with whether the waits are measured.
        """
        cls.switches.append(switch)
        switch(cls.enabled)

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        if enabled == cls.enabled:
            return
        cls.enabled = enabled
        for switch in cls.switches:
            switch(enabled)
