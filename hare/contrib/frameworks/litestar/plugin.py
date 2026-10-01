"""``HarePlugin`` - Hare in a Litestar application."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from litestar.plugins import InitPluginProtocol, SerializationPlugin

from hare.contrib.frameworks.lifecycle import HareLifecycle
from hare.contrib.frameworks.litestar.dependencies import RequestQueryDIPlugin
from hare.contrib.frameworks.litestar.dto import HareDTO
from hare.contrib.frameworks.litestar.exception_handlers import HareExceptionHandlers
from hare.contrib.frameworks.litestar.middleware.repeated_query_reset_middleware import RepeatedQueryResetMiddleware
from hare.contrib.frameworks.litestar.middleware.transaction_middleware import TransactionMiddleware
from hare.contrib.frameworks.transactions import RequestTransaction
from hare.core.model_cache import ModelCache
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from litestar import Litestar
    from litestar.config.app import AppConfig
    from litestar.dto.base_dto import AbstractDTO
    from litestar.types import Scope
    from litestar.typing import FieldDefinition

    from hare.core.config import HareConfig


class HarePlugin(InitPluginProtocol, RequestQueryDIPlugin, SerializationPlugin):
    """Connects Hare to a Litestar application::

        app = Litestar(route_handlers=[...], plugins=[HarePlugin(HARE_CONFIG, atomic_requests=True)])

    - opens a Hare context with the application and closes it with it - before the application's
      own lifespan managers, so theirs already have the database, and after them on shutdown; the
      context is visible to every request;
    - checks every request query once the models are set up, so a wrong one fails at startup;
    - gives each handler returning or taking hare models - a row, a list, a page of rows
      (``Page[Book]``) - a ``HareDTO`` of the model, unless the handler names its own
      ``return_dto``/``dto``;
    - lets handlers take request queries as dependencies - it is a ``RequestQueryDIPlugin`` itself.
      An application's plugins come before the ones Litestar adds, so it takes a request query
      before Litestar's own pydantic DI plugin would take it as a plain pydantic model (without
      its request and its parameters' defaults);
    - answers ``DoesNotExist`` with 404, ``IntegrityError`` with 409, ``InvalidRequestQuery`` with
      400 and ``RequestQueryForbidden`` with 403, unless the application answers them itself;
    - starts each request with an empty count of repeated queries (``RepeatedQueryDetector``);
    - with ``atomic_requests``, runs each HTTP request in a transaction (``TransactionMiddleware``),
      rolled back when the handler raises, even when an exception handler answers it.

    Args:
        config: The Hare configuration, as for ``Hare.init(config=...)``.
        atomic_requests: True for a transaction per request on the default connection, the
            connection names for one on each of them, False for none.

    Raises:
        ConfigurationError: ``atomic_requests`` is neither a bool nor a sequence of names.
    """

    #: The DTO of each model a handler returned or took, built once per model - weakly, and
    #: forgotten with the model's caches, so a live model registered again gets a DTO of its own
    #: and the unregistered class is freed.
    model_dtos: ClassVar[ModelCache[type[HareDTO[Any]]]] = ModelCache()

    def __init__(self, config: dict[str, Any] | HareConfig, *, atomic_requests: bool | Sequence[str] = False) -> None:
        self.lifecycle = HareLifecycle(config, atomic_requests=atomic_requests)

    def supports_type(self, field_definition: FieldDefinition) -> bool:
        return field_definition.is_subclass_of(Model)

    def create_dto_for_type(self, field_definition: FieldDefinition) -> type[AbstractDTO[Any]]:
        model_type = self.get_model_type(field_definition)
        if model_type is None:
            raise ConfigurationError(f"{field_definition.annotation!r} holds no hare model")
        dto = HarePlugin.model_dtos.get(model_type)
        if dto is None:
            dto = HareDTO[model_type]  # type: ignore[valid-type]
            HarePlugin.model_dtos[model_type] = dto
        return dto

    @classmethod
    def get_model_type(cls, field_definition: FieldDefinition) -> type[Model] | None:
        """The hare model an annotation holds - itself, or inside a union, a collection or a
        generic wrapper (``Page[Book]``).

        Args:
            field_definition: The annotation.

        Returns:
            The model, None when the annotation holds none.
        """
        if field_definition.is_subclass_of(Model):
            return field_definition.annotation
        for inner_type in field_definition.inner_types:
            model_type = cls.get_model_type(inner_type)
            if model_type is not None:
                return model_type
        return None

    def on_app_init(self, app_config: AppConfig) -> AppConfig:
        # Litestar builds the handlers' signatures - the parameters of each request query - right
        # after the plugins, long before the lifespan opens the connections.
        self.lifecycle.bind_models()
        app_config.lifespan.insert(0, self.lifespan)
        for exception_type, handler in HareExceptionHandlers.get_handlers().items():
            app_config.exception_handlers.setdefault(exception_type, handler)
        middleware: list[Any] = [RepeatedQueryResetMiddleware()]
        if self.lifecycle.transaction_connection_names:
            middleware.append(TransactionMiddleware(self.lifecycle.transaction_connection_names))
            app_config.after_exception.append(self.fail_request_transaction)
        app_config.middleware[:0] = middleware
        return app_config

    @staticmethod
    async def fail_request_transaction(error: Exception, scope: Scope) -> None:
        """Rolls back the transaction of a request whose handler raised, even when an exception
        handler answers it (``404`` for ``DoesNotExist``) - Litestar's ``after_exception`` hook.

        Args:
            error: The exception.
            scope: The request's scope.
        """
        RequestTransaction.fail_request(scope, error)

    @asynccontextmanager
    async def lifespan(self, app: Litestar) -> AsyncGenerator[None]:
        """Opens the Hare context for the application's lifetime.

        Args:
            app: The application.

        Raises:
            ConfigurationError: A request transaction names a connection the configuration lacks,
                or a request query is wrong.
        """
        async with self.lifecycle.running():
            yield
