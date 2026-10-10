"""The Hare context of a web application's lifetime - the same for every framework."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hare.contrib.application_lifecycle import ApplicationLifecycle
from hare.contrib.request_query.request_query import RequestQuery
from hare.core.hare import Hare

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.config import HareConfig


class HareLifecycle(ApplicationLifecycle):
    """Binds an application's models, opens its Hare context when it starts and closes it when it
    stops.

    Args:
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        atomic_requests: True for a transaction per request on the default connection, the
            connection names for one on each of them, False for none.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor a sequence of names.
    """

    def __init__(
        self, config: Mapping[str, Any] | HareConfig | str, *, atomic_requests: bool | Sequence[str] = False
    ) -> None:
        super().__init__(config, transactions=atomic_requests, transactions_option="atomic_requests")

    def bind_models(self) -> None:
        """Binds the configuration's models without connections - a framework building its
        handlers' signatures before the application starts reads the request queries' parameters
        from them (``Meta.filters``, descriptions).

        Raises:
            ConfigurationError: The configuration is wrong.
        """
        Hare.bind_models(self.config)

    def check_application(self) -> None:
        """Checks every request query.

        Raises:
            ConfigurationError: A request query is wrong.
        """
        RequestQuery.check_declarations()
