"""HareContext - all the ORM state of one execution context: connections, the model registry, the init
state, the timezone and the routers.
"""

from __future__ import annotations

import contextvars
import importlib
import logging
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.classes.deferred_import import DeferredImport
from hare.core.config import ConfigSecrets, HareConfig
from hare.core.connections.connection_handler import ConnectionHandler
from hare.core.constants import DEFAULT_CONNECTION_NAME, INITIALIZATION_RELEASE_FAILED
from hare.core.log import logger
from hare.core.resource_completion import ResourceCompletion
from hare.core.routing.connection_router import ConnectionRouter
from hare.dialects.base.constants import SLOW_QUERY_THRESHOLD_MS
from hare.exceptions import ConfigurationError
from hare.instrumentation.constants import OBSERVER_SHUTDOWN_WAIT_TIMEOUT_SECONDS
from hare.instrumentation.observers.observer import ObserverCallback
from hare.instrumentation.observers.observer_set import ObserverSet
from hare.instrumentation.observers.observers import Observers
from hare.time.constants import DEFAULT_TIMEZONE
from hare.time.zone_names import ZoneNames

if TYPE_CHECKING:
    from types import TracebackType

    from hare.core.apps import Apps
    from hare.core.apps.declarations import ModelReference
    from hare.core.apps.live_model_registration import LiveModelRegistration
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


TCallback = TypeVar("TCallback", bound=ObserverCallback)


class HareContext:
    """All the ORM state of one execution context: its connections, model registry and init state - one
    per test, per xdist worker or per database configuration.

    A context is current in a task (``with``/``async with``, or ``Hare.init()``), and code there
    uses its connections without passing them; a global fallback context
    (``init(_enable_global_fallback=True)``) serves tasks without one. One instance must not be
    entered from several tasks at once.

    Example:
        async with HareContext() as context:
            await context.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models":
            ["myapp.models"]}))
            await context.generate_schemas()
            user = await User.objects.create(name="test")
    """

    live_model_registration: ClassVar[DeferredImport[type[LiveModelRegistration]]] = DeferredImport(
        "hare.core.apps.live_model_registration", "LiveModelRegistration"
    )

    #: The context current in this task - None outside one.
    current_context: ClassVar[contextvars.ContextVar[HareContext | None]] = contextvars.ContextVar(
        "hare_context", default=None
    )
    #: The context every task without a current one of its own uses - set by
    #: ``init(_enable_global_fallback=True)``; None by default.
    global_context: ClassVar[HareContext | None] = None

    #: The token restoring the previous current context - None while this one isn't entered.
    _token: contextvars.Token[HareContext | None] | None
    #: The observers of this context - they get the events while it is the current one.
    observers: ObserverSet

    @classmethod
    def get_current(cls, *, use_fallback: bool = True) -> HareContext | None:
        """The context current in this task, else the global fallback one.

        Args:
            use_fallback: False to ignore the global fallback context - ``Hare.init()`` sets up a
                context of its own then, even while another application's is the global one.

        Returns:
            The context, None without one.
        """
        context = cls.current_context.get()
        if context is not None:
            return context
        return cls.global_context if use_fallback else None

    @classmethod
    def require_current(cls) -> HareContext:
        """The context current in this task, else the global fallback one.

        Returns:
            The context.

        Raises:
            ConfigurationError: There is none.
        """
        context = cls.get_current()
        if context is None:
            raise ConfigurationError(
                "Hare ORM is not initialized. Call Hare.init() first "
                "or use 'async with HareContext()' for explicit context management."
            )
        return context

    @classmethod
    def set_global(cls, context: HareContext) -> None:
        """Makes ``context`` the global fallback context - the one every task without a current
        context of its own uses, e.g. the requests of an application whose lifespan runs in a task
        of its own.

        Args:
            context: The context.

        Raises:
            ConfigurationError: Another context is the global one already.
        """
        cls.check_global_available(context)
        cls.global_context = context

    @classmethod
    def check_global_available(cls, context: HareContext) -> None:
        """Checks that ``context`` can become the global fallback context.

        Args:
            context: The context.

        Raises:
            ConfigurationError: Another context is the global one already.
        """
        if cls.global_context is not None and cls.global_context is not context:
            raise ConfigurationError(
                "Global context fallback is already enabled by another Hare.init() call. "
                "Only one global context can be active at a time. "
                "Use explicit HareContext() for multiple isolated contexts, "
                "or set _enable_global_fallback=False for secondary apps."
            )

    @classmethod
    def clear_global_if(cls, context: HareContext) -> None:
        """Stops ``context`` being the global fallback context, if it is.

        Args:
            context: The context.
        """
        if cls.global_context is context:
            cls.global_context = None

    def __enter__(self) -> HareContext:
        """Makes this context the current one of the task.

        Returns:
            This context.

        Raises:
            ConfigurationError: This context is current in another scope already - one context
                can't be entered by two tasks at once; each concurrent scope needs its own.
        """
        if self._token is not None:
            raise ConfigurationError(
                "This HareContext instance is already active in another scope (entered via "
                "'with'/'async with' and not yet exited) - most likely shared across "
                "concurrently-running asyncio tasks. A single HareContext instance must not be "
                "entered concurrently; create a separate instance per concurrent scope."
            )
        self._token = HareContext.current_context.set(self)
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Makes the previous context the current one of the task again - and binds its models
        back when this context bound them to its own configuration."""
        if self._token is not None:
            HareContext.current_context.reset(self._token)
            self._token = None
            if self._models_bound:
                self.bind_current_models()

    def bind_current_models(self) -> None:
        """Binds the models of the context current now - the one this context was entered in, else
        the global fallback one - to it again. Model classes hold their connection, table names,
        relations and base queries as plain attributes, and this context bound them to its own
        configuration."""
        current = HareContext.get_current()
        if current is not None and current is not self and current._apps is not None and current._inited:
            # Best effort: a stale previous context must not make leaving this one raise.
            with suppress(Exception):
                current._apps.bind_models()

    def __init__(self) -> None:
        from hare.fields.encrypted.encryption_keyring import EncryptionBinding
        from hare.fields.encrypted.field_encryption import FieldEncryption

        self.identity = object()
        self.encryption_keyring = FieldEncryption.default_keyring
        self.encryption_binding = EncryptionBinding(self.identity, self.encryption_keyring)
        self._connections: ConnectionHandler | None = None
        self._apps: Apps | None = None
        # Whether this context bound the model classes to its configuration - kept after
        # drop_databases() forgets them, so leaving the context still binds the previous one's back.
        self._models_bound = False
        self._inited: bool = False
        self._token = None
        self._table_name_generator: Callable[[type[Model]], str] | None = None
        self._default_connection: str | None = None
        # Timezone settings
        self._use_timezone: bool = True
        self._timezone: str = DEFAULT_TIMEZONE
        # Routers
        self._routers: list[Callable[[], object]] = []
        self._router: ConnectionRouter | None = None
        self.observers = ObserverSet()

    def observe(
        self, event_type: type, callback: TCallback, *, models: Iterable[ModelReference] | None = None
    ) -> TCallback:
        """Adds an observer of ``event_type`` getting the events while this context is the current
        one - see ``Observers.observe()``. Leaving the context (``async with``) drops it.

        Args:
            event_type: The event type.
            callback: The observer.
            models: For ``RowsChanged``: the models whose changes it gets.

        Returns:
            The callback, to pass to ``unobserve()``.

        Raises:
            QueryError, ConfigurationError: See ``Observers.observe()``.
        """
        self.observers.add(event_type, callback, models)
        return callback

    def unobserve(
        self, event_type: type, callback: ObserverCallback, *, models: Iterable[ModelReference] | None = None
    ) -> None:
        """Stops an observer of this context - see ``Observers.unobserve()``.

        Args:
            event_type: The event type.
            callback: The observer.
            models: The models it stops observing.

        Raises:
            QueryError: See ``Observers.unobserve()``.
        """
        self.observers.remove(event_type, callback, models)

    @property
    def connections(self) -> ConnectionHandler:
        """The context's connection handler, created on first access - each context has its own, and
        each task sees the connections of its own transactions in it."""
        if self._connections is None:
            self._connections = ConnectionHandler()
        return self._connections

    @property
    def apps(self) -> Apps | None:
        """The context's app registry - None before ``init()``."""
        return self._apps

    @property
    def router(self) -> ConnectionRouter:
        """The context's ConnectionRouter, created on first access."""
        if self._router is None:
            self._router = ConnectionRouter()
        return self._router

    @property
    def inited(self) -> bool:
        """Whether ``init()`` has completed on this context."""
        return self._inited

    @property
    def default_connection(self) -> str | None:
        """The alias of the context's default connection - the only connection, or the one named
        ``"default"``; None when there is neither."""
        return self._default_connection

    @property
    def use_timezone(self) -> bool:
        """Whether datetime fields read and write timezone-aware values."""
        return self._use_timezone

    @property
    def timezone(self) -> str:
        """The name of the context's timezone (e.g., "UTC", "America/New_York")."""
        return self._timezone

    @property
    def routers(self) -> list[Callable[[], object]]:
        """The router factories configured for this context."""
        return self._routers

    async def init(
        self,
        config: Mapping[str, Any] | HareConfig | str | None = None,
        *,
        _create_db: bool = False,
        use_timezone: bool = True,
        timezone: str = DEFAULT_TIMEZONE,
        routers: list[str | Callable[[], object]] | None = None,
        table_name_generator: Callable[[type[Model]], str] | None = None,
        connect: bool = True,
        slow_query_threshold_ms: float = SLOW_QUERY_THRESHOLD_MS,
        _enable_global_fallback: bool = False,
    ) -> None:
        """Initializes this context. Works without ``Hare.init()``.

        Args:
            config: Anything ``HareConfig.load()`` takes.
            _create_db: Create the database if it doesn't exist.
            use_timezone: Make datetime fields timezone-aware.
            timezone: The timezone, "UTC" by default.
            routers: Router classes or zero-argument factories, directly or by import path.
            table_name_generator: A callable generating table names.
            connect: False sets the models and connections up without connecting - the connection
                names are still checked.
            slow_query_threshold_ms: Queries at or above this duration are logged at DEBUG.
            _enable_global_fallback: Make this context the global fallback for tasks without a
                current one.

        Raises:
            ConfigurationError: The configuration is invalid, or ``_enable_global_fallback`` is set
                while another context is the global one - checked before anything changes.
        """
        typed_config = HareConfig.load(config if config is not None else {})
        config_dict = typed_config.to_dict()
        connections_config = config_dict["connections"]
        apps_config = config_dict["apps"]

        effective_use_timezone = typed_config.use_timezone if typed_config.use_timezone is not None else use_timezone
        effective_timezone = typed_config.timezone if typed_config.timezone is not None else timezone
        effective_routers = typed_config.routers if typed_config.routers is not None else routers
        ZoneNames.validate(effective_timezone)

        if not connect and _create_db:
            raise ConfigurationError("connect=False cannot be used with _create_db=True")
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Hare-ORM startup\n    connections: %s\n    apps: %s",
                ConfigSecrets.get_masked_connections(connections_config),
                apps_config,
            )
        if _enable_global_fallback:
            HareContext.check_global_available(self)

        router_factories = self._get_router_factories(effective_routers)
        previous_connections = await self._set_up_registries(
            typed_config, router_factories, table_name_generator, connect=connect, create_db=_create_db
        )

        self._table_name_generator = table_name_generator
        Observers.slow_query_threshold_ms = slow_query_threshold_ms
        self._init_timezone(effective_use_timezone, effective_timezone)

        self._default_connection = HareContext.get_default_connection_alias(typed_config)

        self._inited = True

        if _enable_global_fallback:
            HareContext.set_global(self)

        # Everything new is in place - the old connections are closed last; a failure still reaches
        # the caller.
        if previous_connections is not None and previous_connections._db_config is not None:
            if self._connections is not None:
                previous_connections.hand_over(self._connections)
            await previous_connections.close_all(discard=True)

    async def _set_up_registries(
        self,
        typed_config: HareConfig,
        router_factories: list[Callable[[], object]],
        table_name_generator: Callable[[type[Model]], str] | None,
        *,
        connect: bool,
        create_db: bool,
    ) -> ConnectionHandler | None:
        """Builds the connections, the apps and the routers of this context. A re-init must not
        destroy a working context before the new arguments are known to be valid: a new
        ``ConnectionHandler`` is built and validated, and on failure the old one is put back and the
        new one closed. It is assigned before ``Apps()`` is built - ``model._meta.connection``
        resolves through the current context. Routers are resolved first and put in place last.

        Args:
            typed_config: The configuration.
            router_factories: The zero-argument router factories.
            table_name_generator: A callable generating table names.
            connect: Whether the connections are opened.
            create_db: Whether a missing database is created.

        Returns:
            The connections this context had before - closed by the caller once everything new is
            in place.
        """
        # Genuinely circular: hare.core.apps -> hare.models -> hare.transactions ->
        # hare.core.hare_context -> hare.core.apps.
        from hare.core.apps import Apps
        from hare.core.apps.model_binding_graph import ModelBindingGraph

        config_dict = typed_config.to_dict()
        connections_config = config_dict["connections"]
        apps_config = config_dict["apps"]
        previous_connections = self._connections
        previous_apps = self._apps
        previous_models_bound = self._models_bound
        previous_router = self._router
        previous_router_factories = self._routers
        new_connections = ConnectionHandler()
        self._connections = new_connections
        # The models' connections are read through the current context - this one, while it is
        # set up, even when init() is called before the context is entered.
        setup_token = HareContext.current_context.set(self) if HareContext.current_context.get() is not self else None
        try:
            try:
                if connect:
                    await new_connections._init(connections_config, create_db)
                else:
                    new_connections._init_config(connections_config)

                self._models_bound = True
                self._apps = Apps(
                    None,
                    new_connections,
                    table_name_generator,
                    validate_connections=connect,
                    swappable=typed_config.swappable,
                )
                self._apps._config = apps_config
                from hare.core.apps.model_discovery import ModelDiscovery

                with ModelBindingGraph(self._apps):
                    ModelDiscovery.load_from_config(self._apps)
                    # The labels observers name, looked up now that the models are bound.
                    for observer_set in (Observers.process_observers, self.observers):
                        observer_set.find_model_labels(self._apps)
                    prepared_router = ConnectionRouter()
                    prepared_router.init_routers(router_factories)
                    prepared_router.read_your_writes_seconds = typed_config.read_your_writes_seconds
                    self._router = prepared_router
                    self._routers = router_factories
            except BaseException as error:
                self._connections = previous_connections
                self._apps = previous_apps
                self._models_bound = previous_models_bound
                self._router = previous_router
                self._routers = previous_router_factories
                if new_connections._db_config is not None:
                    try:
                        await new_connections.close_all(discard=True)
                    except BaseException as release_error:
                        raise BaseExceptionGroup(INITIALIZATION_RELEASE_FAILED, [error, release_error]) from None
                raise
        finally:
            if setup_token is not None:
                HareContext.current_context.reset(setup_token)
        return previous_connections

    @staticmethod
    def get_default_connection_alias(typed_config: HareConfig) -> str | None:
        """The connection a model without ``default_connection`` uses - the only one, or the one
        named ``default``.

        Args:
            typed_config: The configuration.

        Returns:
            The connection's alias, None when neither is there.
        """
        connection_aliases = list(typed_config.connections.keys())
        if len(connection_aliases) == 1:
            return connection_aliases[0]
        if DEFAULT_CONNECTION_NAME in connection_aliases:
            return DEFAULT_CONNECTION_NAME
        return None

    def _init_timezone(self, use_timezone: bool, timezone: str) -> None:
        """Sets the context's timezone settings."""
        self._use_timezone = use_timezone
        self._timezone = timezone

    @staticmethod
    def _get_router_factories(routers: list[str | Callable[[], object]] | None) -> list[Callable[[], object]]:
        """The router factories ``routers`` names - each an import path, class or callable.

        Args:
            routers: The configured routers, None for none.

        Returns:
            The factories, in order.

        Raises:
            ConfigurationError: A path can't be imported.
        """
        router_factories: list[Callable[[], object]] = []
        for router in routers or []:
            if isinstance(router, str):
                try:
                    module_name, factory_name = router.rsplit(".", 1)
                    router_factories.append(getattr(importlib.import_module(module_name), factory_name))
                except Exception as error:
                    raise ConfigurationError(f"Can't import router from {router!r}: {error}") from error
            else:
                router_factories.append(router)
        return router_factories

    async def generate_schemas(self, *, safe: bool = True) -> None:
        """
        Creates the tables and schema objects of every model of the context.

        Args:
            safe: When True, creates tables only if they don't already exist.

        Raises:
            ConfigurationError: If context has not been initialized.
        """
        if not self._inited:
            raise ConfigurationError("Context not initialized. Call init() before generating schemas.")
        for connection in self.connections.all():
            await connection.generate_schema(safe)

    def get_model(self, reference: ModelReference) -> type[Model]:
        """The model a reference names - an ``"app.Model"`` label (``"models.User"``), a ``swappable()``
        setting or a class (the context's own class of the model).

        Args:
            reference: The reference.

        Returns:
            The model class.

        Raises:
            ConfigurationError: The context isn't initialized, or the reference names no model.
        """
        if self._apps is None:
            raise ConfigurationError("Context not initialized. Call init() before accessing models.")
        return self._apps.get_model(reference)

    def get_connection(self, connection_alias: str | None = None) -> DatabaseClient:
        """
        The connection of an alias.

        Args:
            connection_alias: The connection alias. If None, uses the default connection.
                            With a single connection, it becomes the default automatically.
                            With multiple connections, either specify explicitly or
                            configure one as "default".

        Returns:
            The database client for the specified connection.

        Raises:
            ConfigurationError: If context not initialized, connection not found,
                               or no default connection when multiple exist.
        """
        if not self._inited:
            raise ConfigurationError("Context not initialized. Call init() before accessing database.")

        if connection_alias is None:
            if self._default_connection is None:
                raise ConfigurationError(
                    "No default connection configured. Either use a single connection, "
                    "name one 'default', or specify connection_alias explicitly."
                )
            connection_alias = self._default_connection

        return self.connections.get(connection_alias)

    async def close_connections(self) -> None:
        """Closes this context's connections, clears the global fallback if it is this context, and
        marks the context as not inited. Once started, shutdown drains observers and releases
        pools before propagating caller cancellation.

        Raises:
            Exception: What ``close_all()`` raised - after the context forgot its connections.
        """
        await ResourceCompletion.wait(self._close_connections())

    async def _close_connections(self) -> None:
        """Finish observer delivery before releasing the resources it may still use."""
        # Observers may still use this context's pools; drain before discarding them.
        try:
            await self.observers.wait_for_pending(timeout_seconds=OBSERVER_SHUTDOWN_WAIT_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Observer failure surfaced during shutdown")
        try:
            if self._connections is not None and self._connections._db_config is not None:
                await self._connections.close_all(discard=True)
        finally:
            self._connections = None
            # Clear global context if this context was set as the global fallback
            HareContext.clear_global_if(self)
            self._inited = False

    @classmethod
    def bind_models(
        cls,
        config: Mapping[str, Any] | HareConfig | str,
        table_name_generator: Callable[[type[Model]], str] | None = None,
    ) -> Apps:
        """Binds the models of a configuration without connecting to a database - their relations,
        swappable models, filters and orderings - and leaves no context current. Synchronous: a
        framework building its request handlers' signatures before the application starts reads
        the filters (``Model._meta.get_lookup_info()`` and the like) from them; ``init()`` later
        sets up the connections as usual.

        Args:
            config: The configuration, as for ``init()``.
            table_name_generator: The table name generator, as for ``init()``.

        Returns:
            The bound models by app.

        Raises:
            ConfigurationError: The configuration is wrong.
        """
        from hare.core.apps import Apps

        typed_config = HareConfig.load(config)
        config_dict = typed_config.to_dict()
        with cls() as context:
            context.connections._init_config(config_dict["connections"])
            return Apps(
                config_dict["apps"],
                context.connections,
                table_name_generator,
                validate_connections=False,
                swappable=typed_config.swappable,
            )

    def swappable_label(self, setting: str) -> str:
        """The ``app_label.ModelName`` label a ``swappable`` config setting points at - the
        configured model, else the model declaring ``Meta.swappable = setting``.

        Args:
            setting: The setting name, e.g. ``"USER_MODEL"``.

        Raises:
            ConfigurationError: The context isn't set up, or the setting is neither configured nor
                declared by any model.
        """
        if self._apps is None:
            raise ConfigurationError(
                f"Swappable setting {setting!r} is only known after Hare.init() - initialize Hare "
                "before importing migration files that use swappable_dependency()"
            )
        return self._apps.get_swappable_label(setting)

    def get_swappable_model(self, setting: str) -> type[Model]:
        """The model a ``swappable`` config setting points at - see ``swappable_label()``.

        Args:
            setting: The setting name, e.g. ``"USER_MODEL"``.

        Raises:
            ConfigurationError: See ``swappable_label()``.
        """
        return self.get_model(self.swappable_label(setting))

    def register_live_models(
        self,
        models: Iterable[type[Model]],
        app_label: str,
        connection_alias: str = DEFAULT_CONNECTION_NAME,
        *,
        managed: bool | None = None,
    ) -> None:
        """Registers models built while the application runs - after ``init()``, e.g. from the
        introspection of a live database - under ``app_label`` on ``connection_alias``, binding
        them as ``init()`` binds the configured ones. Models whose relations point at each other
        are registered together. Atomic: when any model fails, none of them stays registered and
        no model keeps a backward relation added by them.

        Args:
            models: The model classes.
            app_label: The app label to register them under.
            connection_alias: The connection their queries use.
            managed: Written to each model's ``_meta.managed`` before its relations are set up -
                False keeps migrations and drift checks away from its table; None keeps what its
                ``Meta.managed`` declares.

        Raises:
            ConfigurationError: ``app_label`` is no name a model's label can start with, ``connection_alias``
                is not a configured connection, two of ``models`` share a name, a different class with
                the same name is already registered under ``app_label``, a model belongs to another app,
                or a relation can't be set up.
        """
        return self.live_model_registration.register_live_models(
            self, models, app_label, connection_alias, managed=managed
        )

    def unregister_live_models(self, models: Iterable[ModelReference]) -> None:
        """Reverses ``register_live_models()``: removes the models from the registry together with
        the backward relations they added and every cache built for them - relations between the
        models themselves don't block it. A model with the same name, or the same class, can be
        registered again afterwards; an unregistered class runs no queries until then.

        Args:
            models: Models registered in this context - classes, ``"app.Model"`` labels or
                ``swappable()`` settings.

        Raises:
            ConfigurationError: One of ``models`` isn't registered, or a model outside ``models``
                still has a relation pointing at one of them.
        """
        return self.live_model_registration.unregister_live_models(self, models)

    async def drop_databases(self) -> None:
        """Drops every database of this context's connections and forgets its models - for tests.

        Raises:
            ConfigurationError: The context isn't set up.
        """
        if not self._inited:
            raise ConfigurationError("Context not initialized. Call init() before dropping databases.")
        connections = self.connections
        await connections.close_all(discard=False)
        for connection in connections.all():
            await connection.db_delete()
            connections.discard(connection.connection_alias)
        if self._apps is not None:
            for model in self._apps.get_models_iterable():
                model._meta.default_connection = None
            self._apps.clear()
            self._apps = None

    async def __aenter__(self) -> HareContext:
        """
        Enter the async context manager and set this context as current.

        Returns:
            This context instance.
        """
        self.__enter__()
        return self

    async def __aexit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        exception_traceback: TracebackType | None,
    ) -> None:
        """Closes the connections and restores the previous context - the restore runs even when
        closing raises.
        """
        try:
            await self.close_connections()
        finally:
            self.__exit__(exception_type, exception, exception_traceback)
            self._apps = None
            self._models_bound = False
            self._router = None
            self._inited = False
            self.observers.clear()
