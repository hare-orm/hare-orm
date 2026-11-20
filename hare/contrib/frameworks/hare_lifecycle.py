"""The Hare context of a web application's lifetime - the same for every framework."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.application_lifecycle import ApplicationLifecycle
from hare.contrib.request_query.request_query import RequestQuery
from hare.core.hare import Hare
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.apps import Apps
    from hare.core.config import HareConfig
    from hare.core.hare_context import HareContext


class HareLifecycle(ApplicationLifecycle):
    """Binds an application's models, opens its Hare context when it starts and closes it when it
    stops.

    Args:
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        context: An initialized context to borrow, without rebinding models or closing connections.
        atomic_requests: True for a transaction per request on the automatically selected connection, the
            connection aliases for one on each of them, False for none.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor an iterable of names.
    """

    def __init__(
        self,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        atomic_requests: bool | Iterable[str] = False,
        context: HareContext | None = None,
    ) -> None:
        super().__init__(config, transactions=atomic_requests, transactions_option="atomic_requests", context=context)
        self.model_apps: Apps | None = None

    def bind_models(self) -> Apps:
        """Binds the configuration's models without connections - a framework building its
        handlers' signatures before the application starts reads the request queries' parameters
        from them (``Meta.filters``, descriptions).

        Raises:
            ConfigurationError: The configuration is wrong.
        """
        if self.model_apps is None:
            self.model_apps = (
                self.provided_context.apps
                if self.provided_context is not None
                else Hare.bind_models(self.get_config())
            )
        if self.model_apps is None:
            raise ConfigurationError("The supplied application context has no model registry")
        return self.model_apps

    def get_model_apps(self) -> Apps:
        """Use the started registry for new schemas, otherwise the offline startup registry."""
        return self.context.apps if self.context is not None else self.bind_models()  # type: ignore[return-value]

    def check_application(self) -> None:
        """Checks every request query.

        Raises:
            ConfigurationError: A request query is wrong.
        """
        RequestQuery.check_declarations()
