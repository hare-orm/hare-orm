"""HareContext - all the ORM state of one execution context: connections, the model registry, the init
state, the timezone and the routers.
"""

from __future__ import annotations

import contextvars
import importlib
import logging
import math
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from types import ModuleType
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.core.caches import Caches
from hare.core.config import ConfigSecrets, HareConfig
from hare.core.connection_handler import ConnectionHandler
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.core.log import logger
from hare.core.router import ConnectionRouter
from hare.dialects.base.constants import MAX_SLOW_QUERY_THRESHOLD_MS, SLOW_QUERY_THRESHOLD_MS
from hare.exceptions import ConfigurationError
from hare.instrumentation.constants import OBSERVER_SHUTDOWN_WAIT_TIMEOUT_SECONDS
from hare.instrumentation.observer import ObserverCallback
from hare.instrumentation.observer_set import ObserverSet
from hare.instrumentation.observers import Observers
from hare.utils import Timezone
from hare.utils.constants import DEFAULT_TIMEZONE

if TYPE_CHECKING:
    from hare.core.apps import Apps
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
        async with HareContext() as ctx:
            await ctx.init(HareConfig.from_db_url("sqlite://:memory:", {"models":
            ["myapp.models"]}))
            await ctx.generate_schemas()
            user = await User.objects.create(name="test")
    """

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
    def get_current(cls, use_fallback: bool = True) -> HareContext | None:
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

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Makes the previous context the current one of the task again."""
        if self._token is not None:
            HareContext.current_context.reset(self._token)
            self._token = None

    def __init__(self) -> None:
        self._connections: ConnectionHandler | None = None
        self._apps: Apps | None = None
        self._inited: bool = False
        self._token = None
        self._table_name_generator: Callable[[type[Model]], str] | None = None
        self._default_connection: str | None = None
        # Timezone settings
        self._use_tz: bool = True
        self._timezone: str = DEFAULT_TIMEZONE
        # Routers
        self._routers: list[type] = []
        self._router: ConnectionRouter | None = None
        self.observers = ObserverSet()

    def observe(
        self, event_type: type, callback: TCallback, *, models: Iterable[type[Model]] | None = None
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
            QueryError, TypeError: See ``Observers.observe()``.
        """
        self.observers.add(event_type, callback, models)
        return callback

    def unobserve(
        self, event_type: type, callback: ObserverCallback, *, models: Iterable[type[Model]] | None = None
    ) -> None:
        """Stops an observer of this context - see ``Observers.unobserve()``.

        Args:
            event_type: The event type.
            callback: The observer.
            models: The models it stops observing.

        Raises:
            QueryError, TypeError: See ``Observers.unobserve()``.
        """
        self.observers.remove(event_type, callback, models)

    @property
    def connections(self) -> ConnectionHandler:
        """
        Get the ConnectionHandler for this context.

        Creates a new ConnectionHandler on first access (lazy initialization).
        The handler uses instance-level storage for true isolation between contexts.

        Returns:
            The ConnectionHandler instance owned by this context.
        """
        if self._connections is None:
            # ConnectionHandler always uses instance storage for isolation
            self._connections = ConnectionHandler()
        return self._connections

    @property
    def apps(self) -> Apps | None:
        """
        Get the Apps registry for this context.

        Returns:
            The Apps instance if initialized, None otherwise.
        """
        return self._apps

    @property
    def router(self) -> ConnectionRouter:
        """The context's ConnectionRouter, created on first access."""
        if self._router is None:
            self._router = ConnectionRouter()
        return self._router

    @property
    def inited(self) -> bool:
        """
        Check if this context has been initialized.

        Returns:
            True if init() has been called successfully, False otherwise.
        """
        return self._inited

    @property
    def default_connection(self) -> str | None:
        """
        Get the default connection name for this context.

        Returns:
            The default connection name if one is configured, None otherwise.
            A default is automatically set when there's only one connection
            or when a connection is named "default".
        """
        return self._default_connection

    @property
    def use_tz(self) -> bool:
        """
        Check if timezone-aware datetimes are enabled.

        Returns:
            True if datetime fields are timezone-aware, False otherwise.
        """
        return self._use_tz

    @property
    def timezone(self) -> str:
        """
        Get the timezone configured for this context.

        Returns:
            The timezone string (e.g., "UTC", "America/New_York").
        """
        return self._timezone

    @property
    def routers(self) -> list[type]:
        """
        Get the database routers for this context.

        Returns:
            List of router classes configured for this context.
        """
        return self._routers

    async def init(
        self,
        config: Mapping[str, Any] | HareConfig | str | None = None,
        *,
        _create_db: bool = False,
        use_tz: bool = True,
        timezone: str = DEFAULT_TIMEZONE,
        routers: list[str | type] | None = None,
        table_name_generator: Callable[[type[Model]], str] | None = None,
        connect: bool = True,
        slow_query_threshold_ms: float = SLOW_QUERY_THRESHOLD_MS,
        _enable_global_fallback: bool = False,
    ) -> None:
        """Initializes this context. Works without ``Hare.init()``.

        Args:
            config: Anything ``HareConfig.load()`` takes.
            _create_db: Create the database if it doesn't exist.
            use_tz: Make datetime fields timezone-aware.
            timezone: The timezone, "UTC" by default.
            routers: Router paths or classes.
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
        # Genuinely circular, not just careless: hare.core.apps -> hare.models -> hare.transactions
        # -> hare.core.context -> hare.core.apps - a top-level import here breaks the chain at import time.
        from hare.core.apps import Apps

        typed_config = HareConfig.load(config if config is not None else {})
        config_dict = typed_config.to_dict()
        connections_config = config_dict["connections"]
        apps_config = config_dict["apps"]

        if not isinstance(use_tz, bool):
            raise ConfigurationError(f"use_tz must be a bool, got {use_tz!r}")
        if table_name_generator is not None and not callable(table_name_generator):
            raise ConfigurationError(f"table_name_generator must be callable, got {table_name_generator!r}")
        effective_use_tz = typed_config.use_tz if typed_config.use_tz is not None else use_tz
        effective_timezone = typed_config.timezone if typed_config.timezone is not None else timezone
        effective_routers = typed_config.routers if typed_config.routers is not None else routers
        Timezone.validate(effective_timezone)

        if not connect and _create_db:
            raise ConfigurationError("connect=False cannot be used with _create_db=True")
        slow_query_threshold_ms = HareContext.get_validated_slow_query_threshold(slow_query_threshold_ms)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Hare-ORM startup\n    connections: %s\n    apps: %s",
                ConfigSecrets.get_masked_connections(connections_config),
                apps_config,
            )
        if _enable_global_fallback:
            HareContext.check_global_available(self)

        # A re-init must not destroy a working context before the new arguments are known to be
        # valid: a new ConnectionHandler is built and validated, and on failure the old one is put
        # back and the new one closed. It is assigned before Apps() is built - model._meta.db
        # resolves through the current context. Routers are resolved first and put in place last.
        router_classes = self._get_router_classes(effective_routers)
        previous_connections = self._connections
        previous_apps = self._apps
        new_connections = ConnectionHandler()
        self._connections = new_connections
        # The models' connections are read through the current context - this one, while it is
        # set up, even when init() is called before the context is entered.
        setup_token = HareContext.current_context.set(self) if HareContext.current_context.get() is not self else None
        try:
            try:
                if connect:
                    await new_connections._init(connections_config, _create_db)
                else:
                    new_connections._init_config(connections_config)

                self._apps = Apps(
                    apps_config,
                    new_connections,
                    table_name_generator,
                    validate_connections=connect,
                    swappable=typed_config.swappable,
                )
                # router.init_routers() replaces the routers only once every one is instantiated.
                self._init_routers(router_classes)
            except BaseException:
                self._connections = previous_connections
                self._apps = previous_apps
                if previous_apps is not None:
                    # Apps() binds the model classes themselves (their connection, tables and
                    # relations) - bound back to the previous registry so they run on it again, not
                    # on the configuration this call failed to set up.
                    with suppress(Exception):
                        previous_apps.bind_models()
                if new_connections._db_config is not None:
                    with suppress(Exception):
                        await new_connections.close_all(discard=True)
                raise
        finally:
            if setup_token is not None:
                HareContext.current_context.reset(setup_token)

        self._table_name_generator = table_name_generator
        Observers.slow_query_threshold_ms = slow_query_threshold_ms
        self._init_timezone(effective_use_tz, effective_timezone)

        connection_names = list(typed_config.connections.keys())
        if len(connection_names) == 1:
            self._default_connection = connection_names[0]
        elif DEFAULT_CONNECTION_NAME in connection_names:
            self._default_connection = DEFAULT_CONNECTION_NAME
        else:
            self._default_connection = None

        self._inited = True

        if _enable_global_fallback:
            HareContext.set_global(self)

        # Everything new is in place - the old connections are closed last; a failure still reaches
        # the caller.
        if previous_connections is not None and previous_connections._db_config is not None:
            await previous_connections.close_all(discard=True)

    def _init_timezone(self, use_tz: bool, timezone: str) -> None:
        """Initialize timezone settings for this context."""
        self._use_tz = use_tz
        self._timezone = timezone

    @staticmethod
    def _get_router_classes(routers: list[str | type] | None) -> list[type]:
        """The router classes ``routers`` names - each an import path or the class itself.

        Args:
            routers: The configured routers, None for none.

        Returns:
            The classes, in order.

        Raises:
            ConfigurationError: ``routers`` is a single path or class instead of a list, a path
                can't be imported, or an item is neither a path nor a class.
        """
        if isinstance(routers, (str, type)):
            raise ConfigurationError(
                f"routers must be a list of router paths or classes - write [{routers!r}] instead"
            )
        router_classes: list[type] = []
        for router in routers or []:
            if isinstance(router, str):
                try:
                    module_name, class_name = router.rsplit(".", 1)
                    router_classes.append(getattr(importlib.import_module(module_name), class_name))
                except Exception:
                    raise ConfigurationError(f"Can't import router from `{router}`")
            elif isinstance(router, type):
                router_classes.append(router)
            else:
                raise ConfigurationError("Router must be either str or type")
        return router_classes

    def _init_routers(self, router_classes: list[type]) -> None:
        """Puts ``router_classes`` in place as this context's routers - all of them, or none when
        one can't be instantiated.

        Args:
            router_classes: The router classes (``_get_router_classes()``).
        """
        self.router.init_routers(router_classes)
        self._routers = router_classes

    async def generate_schemas(self, safe: bool = True) -> None:
        """
        Generate database schemas for all models in this context.

        Args:
            safe: When True, creates tables only if they don't already exist.

        Raises:
            ConfigurationError: If context has not been initialized.
        """
        if not self._inited:
            raise ConfigurationError("Context not initialized. Call init() before generating schemas.")
        for connection in self.connections.all():
            await connection.generate_schema(safe)

    def get_model(self, app_label: str, model_name: str) -> type[Model]:
        """
        Retrieve a model by app label and model name.

        Args:
            app_label: The app label (e.g., "models").
            model_name: The model class name (e.g., "User").

        Returns:
            The model class.

        Raises:
            ConfigurationError: If context not initialized or model not found.
        """
        if self._apps is None:
            raise ConfigurationError("Context not initialized. Call init() before accessing models.")
        return self._apps.get_model(app_label, model_name)

    def db(self, connection_name: str | None = None) -> DatabaseClient:
        """
        Get a database connection by name.

        Args:
            connection_name: The connection alias. If None, uses the default connection.
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

        if connection_name is None:
            if self._default_connection is None:
                raise ConfigurationError(
                    "No default connection configured. Either use a single connection, "
                    "name one 'default', or specify connection_name explicitly."
                )
            connection_name = self._default_connection

        return self.connections.get(connection_name)

    async def close_connections(self) -> None:
        """Closes this context's connections, clears the global fallback if it is this context, and
        marks the context as not inited.

        Raises:
            Exception: What ``close_all()`` raised - after the context forgot its connections.
        """
        try:
            if self._connections is not None and self._connections._db_config is not None:
                await self._connections.close_all(discard=True)
        finally:
            self._connections = None
            # Clear global context if this context was set as the global fallback
            HareContext.clear_global_if(self)
            self._inited = False
        # Background observers still flushing a metric or trace finish before the loop stops - what
        # one raised is logged already and never aborts a clean shutdown; one that never finishes
        # is cancelled with a warning.
        try:
            await Observers.wait_for_pending(timeout_seconds=OBSERVER_SHUTDOWN_WAIT_TIMEOUT_SECONDS)
        except Exception:
            logger.exception("Observer failure surfaced during shutdown")

    @classmethod
    def bind_models(
        cls,
        config: Mapping[str, Any] | HareConfig | str,
        table_name_generator: Callable[[type[Model]], str] | None = None,
    ) -> None:
        """Binds the models of a configuration without connecting to a database - their relations,
        swappable models, filters and orderings - and leaves no context current. Synchronous: a
        framework building its request handlers' signatures before the application starts reads
        the filters (``Model._meta.get_lookup_info()`` and the like) from them; ``init()`` later
        sets up the connections as usual.

        Args:
            config: The configuration, as for ``init()``.
            table_name_generator: The table name generator, as for ``init()``.

        Raises:
            ConfigurationError: The configuration is wrong.
        """
        from hare.core.apps import Apps

        typed_config = HareConfig.load(config)
        if table_name_generator is not None and not callable(table_name_generator):
            raise ConfigurationError(f"table_name_generator must be callable, got {table_name_generator!r}")
        config_dict = typed_config.to_dict()
        with cls() as context:
            context.connections._init_config(config_dict["connections"])
            Apps(
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
                f'Swappable setting "{setting}" is only known after Hare.init() - initialize Hare '
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
        app_label, model_name = self.swappable_label(setting).split(".", 1)
        return self.get_model(app_label, model_name)

    def register_live_models(
        self,
        models: Iterable[type[Model]],
        app_label: str,
        connection_alias: str = DEFAULT_CONNECTION_NAME,
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
            ConfigurationError: ``connection_alias`` is not a configured connection, two of
                ``models`` share a name, a different class with the same name is already registered
                under ``app_label``, a model belongs to another app, or a relation can't be set up.
        """
        from hare.core.apps import Apps

        if connection_alias not in self.connections.db_config:
            raise ConfigurationError(f'Unknown connection "{connection_alias}"')
        if self._apps is None:
            self._apps = Apps({}, self.connections, self._table_name_generator)
        apps = self._apps
        models = list(dict.fromkeys(models))
        registered_models = apps.apps.get(app_label, {})
        seen_model_names: set[str] = set()
        for model in models:
            if model.__name__ in seen_model_names:
                raise ConfigurationError(f'Two models named "{model.__name__}" were passed for app "{app_label}".')
            seen_model_names.add(model.__name__)
            registered_model = registered_models.get(model.__name__)
            if registered_model is not None and registered_model is not model:
                raise ConfigurationError(
                    f'A different model named "{app_label}.{model.__name__}" is already registered - '
                    "call unregister_live_models() on it first."
                )
            if model._meta.app and model._meta.app != app_label:
                raise ConfigurationError(
                    f'Model "{model.__name__}" belongs to app "{model._meta.app}" - it can\'t be '
                    f'registered under "{app_label}".'
                )
        new_models = [model for model in models if registered_models.get(model.__name__) is not model]
        # A model whose relations were already wired (e.g. by another context) gets nothing
        # added by this call beyond its registry entry, so a rollback must not unwire it.
        fresh_models = [model for model in new_models if not model._meta._inited and not model._meta._fk_o2o_inited]
        previous_state_by_model = {
            model: (model._meta.app, model._meta.managed, model._meta.default_connection, model._meta.db_table)
            for model in models
        }
        module = ModuleType(f"hare.register_live_models.{app_label}")
        module.__models__ = models  # type: ignore[attr-defined]
        # Like loading the configured apps: the models are registered first (their relations
        # set up later), then bound to the connection, then the relations are set up - so the
        # table-name collision check sees the models' real connection.
        try:
            if managed is not None:
                for model in models:
                    model._meta.managed = managed
            apps.init_app(app_label, [module], _init_relations=False)
            for model in models:
                model._meta.default_connection = connection_alias
            apps.check_cross_connection_constraints()
            apps._init_relations()
            apps._build_initial_querysets()
        except BaseException:
            apps.discard_models(fresh_models)
            for model, (app, model_managed, default_connection, db_table) in previous_state_by_model.items():
                if model in new_models:
                    apps.drop_registry_entry(model)
                    model._meta.app = app
                if model in fresh_models:
                    model._meta.db_table = db_table
                model._meta.managed = model_managed
                model._meta.default_connection = default_connection
            raise
        # The targets just gained backward relations - anything cached for them before is stale.
        Caches.forget_model_caches({target for model in models for target in Apps.get_relation_targets(model)})

    def unregister_live_models(self, models: Iterable[type[Model]]) -> None:
        """Reverses ``register_live_models()``: removes the models from the registry together with
        the backward relations they added and every cache built for them - relations between the
        models themselves don't block it. A model with the same name, or the same class, can be
        registered again afterwards; an unregistered class runs no queries until then.

        Args:
            models: Models registered in this context.

        Raises:
            ConfigurationError: One of ``models`` isn't registered, or a model outside ``models``
                still has a relation pointing at one of them.
        """
        models = list(models)
        if self._apps is None:
            names = ", ".join(model.__name__ for model in models)
            raise ConfigurationError(f'Model(s) "{names}" not registered.')
        self._apps.remove_models(models)

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
            connections.discard(connection.connection_name)
        if self._apps is not None:
            for model in self._apps.get_models_iterable():
                model._meta.default_connection = None
            self._apps.clear()
            self._apps = None

    @staticmethod
    def get_validated_slow_query_threshold(raw_threshold: Any) -> float:
        """Validates ``init(slow_query_threshold_ms=...)``.

        Args:
            raw_threshold: The threshold in milliseconds.

        Returns:
            The threshold as a float.

        Raises:
            ConfigurationError: It is not a finite number in ``[0, MAX_SLOW_QUERY_THRESHOLD_MS]``.
        """
        if isinstance(raw_threshold, bool) or not isinstance(raw_threshold, int | float):
            raise ConfigurationError(
                f"slow_query_threshold_ms must be a number of milliseconds, got {raw_threshold!r}"
            )
        threshold = float(raw_threshold)
        if not math.isfinite(threshold) or not 0 <= threshold <= MAX_SLOW_QUERY_THRESHOLD_MS:
            raise ConfigurationError(
                f"slow_query_threshold_ms must be between 0 and {MAX_SLOW_QUERY_THRESHOLD_MS:g}, got {raw_threshold!r}"
            )
        return threshold

    async def __aenter__(self) -> HareContext:
        """
        Enter the async context manager and set this context as current.

        Returns:
            This context instance.
        """
        self.__enter__()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Closes the connections and restores the previous context - the restore runs even when
        closing raises.
        """
        try:
            await self.close_connections()
        finally:
            self._apps = None
            self._router = None
            self._inited = False
            self.observers.clear()
            self.__exit__(exc_type, exc_val, exc_tb)
            # Model classes hold their connection, table names, relations and base queries as plain
            # attributes - the restored context binds its models again.
            previous = HareContext.current_context.get()
            if previous is not None and previous._apps is not None and previous._inited:
                # Best effort: a stale previous context must not make the cleanup raise.
                with suppress(Exception):
                    previous._apps.bind_models()


__all__ = [
    "HareContext",
]
