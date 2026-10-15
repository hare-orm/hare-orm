from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from hare.classes.class_property import classproperty
from hare.core.config import HareConfig
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.core.hare_context import HareContext
from hare.core.hare_meta import HareMeta
from hare.dialects.base.constants import SLOW_QUERY_THRESHOLD_MS
from hare.exceptions import ConfigurationError
from hare.time.constants import DEFAULT_TIMEZONE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.apps import Apps
    from hare.models import Model


class Hare(metaclass=HareMeta):
    """The default Hare context: ``Hare.init()`` sets a ``HareContext`` up and makes it the
    current one of the task; every other method acts on the current context - see
    ``HareContext`` for what each does."""

    @classmethod
    def get_context(cls) -> HareContext:
        """The current context.

        Raises:
            ConfigurationError: There is none - ``Hare.init()`` hasn't run.
        """
        context = HareContext.get_current()
        if context is None:
            raise ConfigurationError(
                "Hare ORM is not initialized. Call Hare.init() first "
                "or use 'async with HareContext()' for explicit context management."
            )
        return context

    @classproperty
    def apps(cls) -> Apps | None:
        """The models of the current context, None without one."""
        context = HareContext.get_current()
        return context.apps if context else None

    @classmethod
    def is_inited(cls) -> bool:
        """Whether the current context is set up."""
        context = HareContext.get_current()
        return context.inited if context else False

    @classmethod
    async def init(
        cls,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        connect: bool = True,
        _create_db: bool = False,
        use_timezone: bool = True,
        timezone: str = DEFAULT_TIMEZONE,
        routers: list[str | type] | None = None,
        table_name_generator: Callable[[type[Model]], str] | None = None,
        slow_query_threshold_ms: float = SLOW_QUERY_THRESHOLD_MS,
        _enable_global_fallback: bool = False,
    ) -> HareContext:
        """Sets up the current context of the task - a new one when the task has none - with
        ``HareContext.init()``.

        Returns:
            The context - for several ``asyncio.run()`` calls, keep it and enter it with ``with``.

        Raises:
            ConfigurationError: See ``HareContext.init()``; a context this call created is left
                again.
        """
        # The global fallback context is for running queries, not for setting up: an application
        # sets up a context of its own even while another one's is the global one.
        context = HareContext.get_current(use_fallback=False)
        newly_entered = context is None
        if context is None:
            context = HareContext()
            context.__enter__()
        try:
            await context.init(
                config,
                connect=connect,
                _create_db=_create_db,
                use_timezone=use_timezone,
                timezone=timezone,
                routers=routers,
                table_name_generator=table_name_generator,
                slow_query_threshold_ms=slow_query_threshold_ms,
                _enable_global_fallback=_enable_global_fallback,
            )
        except BaseException:
            if newly_entered:
                context.__exit__(None, None, None)
            raise
        return context

    @classmethod
    def bind_models(
        cls,
        config: Mapping[str, Any] | HareConfig | str,
        table_name_generator: Callable[[type[Model]], str] | None = None,
    ) -> Apps:
        """Binds a configuration's models without connecting - see ``HareContext.bind_models()``."""
        return HareContext.bind_models(config, table_name_generator)

    @classmethod
    def swappable_label(cls, setting: str) -> str:
        """See ``HareContext.swappable_label()``.

        Raises:
            ConfigurationError: Hare isn't initialized, or the setting is unknown.
        """
        context = HareContext.get_current()
        if context is None:
            raise ConfigurationError(
                f'Swappable setting "{setting}" is only known after Hare.init() - initialize Hare '
                "before importing migration files that use swappable_dependency()"
            )
        return context.swappable_label(setting)

    @classmethod
    def get_swappable_model(cls, setting: str) -> type[Model]:
        """See ``HareContext.get_swappable_model()``.

        Raises:
            ConfigurationError: Hare isn't initialized, or the setting is unknown.
        """
        return cls.get_context().get_swappable_model(setting)

    @classmethod
    def register_live_models(
        cls,
        models: Iterable[type[Model]],
        app_label: str,
        connection_alias: str = DEFAULT_CONNECTION_NAME,
        *,
        managed: bool | None = None,
    ) -> None:
        """See ``HareContext.register_live_models()``.

        Raises:
            ConfigurationError: No context is current, or the models can't be registered.
        """
        cls.get_context().register_live_models(models, app_label, connection_alias, managed=managed)

    @classmethod
    def unregister_live_models(cls, models: Iterable[type[Model]]) -> None:
        """See ``HareContext.unregister_live_models()``.

        Raises:
            ConfigurationError: No context is current, or the models can't be unregistered.
        """
        cls.get_context().unregister_live_models(models)

    @classmethod
    async def generate_schemas(cls, *, safe: bool = True) -> None:
        """See ``HareContext.generate_schemas()``.

        Raises:
            ConfigurationError: Hare isn't initialized.
        """
        await cls.get_context().generate_schemas(safe=safe)

    @classmethod
    async def close_connections(cls) -> None:
        """Closes the current context's connections - see ``HareContext.close_connections()``;
        nothing without a context."""
        context = HareContext.get_current()
        if context is not None:
            await context.close_connections()

    @classmethod
    async def _drop_databases(cls) -> None:
        """Drops the current context's databases - see ``HareContext.drop_databases()``."""
        await cls.get_context().drop_databases()

    @staticmethod
    def run_async(coro: Coroutine[Any, Any, Any]) -> None:
        """Runs ``coro`` and closes the connections afterwards - for simple scripts.

        Example:
            ::

                async def main():
                    await Hare.init(
                        HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["app.models"]})
                    )
                    ...


                Hare.run_async(main())
        """

        async def main() -> None:
            try:
                await coro
            finally:
                context = HareContext.get_current()
                if context is not None:
                    await context.connections.close_all(discard=True)

        # Imported here, not with hare - anyio takes milliseconds to import and only this runner uses it.
        from anyio import from_thread

        with from_thread.start_blocking_portal() as portal:
            portal.call(main)
