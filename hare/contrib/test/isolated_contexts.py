from __future__ import annotations

import inspect
import typing
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from functools import partial, wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar, cast

from hare import Hare
from hare.contrib.test.constants import MEMORY_SQLITE
from hare.contrib.test.databases.temporary_databases import TemporaryDatabases
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.time.constants import DEFAULT_TIMEZONE

if TYPE_CHECKING:
    pass

T = TypeVar("T")
P = ParamSpec("P")
AsyncFunction = Callable[P, Coroutine[None, None, T]]
AsyncFunctionDecorator = Callable[..., AsyncFunction[..., typing.Any]]
ModulesConfigType = str | list[str]


@typing.overload
def init_memory_sqlite(models: ModulesConfigType | None = None) -> AsyncFunctionDecorator: ...


@typing.overload
def init_memory_sqlite(models: AsyncFunction[..., typing.Any]) -> AsyncFunction[..., typing.Any]: ...


def init_memory_sqlite(
    models: ModulesConfigType | AsyncFunction[..., typing.Any] | None = None,
) -> AsyncFunction[..., typing.Any] | AsyncFunctionDecorator:
    """Decorator for initializing Hare with an in-memory SQLite database.

    This is useful for simple scripts and examples that need a quick database setup.

    Args:
        models: List of modules to load models from. Defaults to ["__main__"].

    Example:
        from hare import Hare, fields, models
        from hare.contrib.test import init_memory_sqlite


        class MyModel(models.Model):
            id = fields.IntField(primary_key=True)
            name = fields.TextField()


        @init_memory_sqlite
        async def run():
            obj = await MyModel.objects.create(name="")
            assert obj.id == 1


        if __name__ == "__main__":
            Hare.run_async(run())

        Custom models example:

        @init_memory_sqlite(models=["app.models", "app.other_models"])
        async def run(): ...
    """

    def wrapper(function: AsyncFunction[..., typing.Any], model_modules: list[str]):
        @wraps(function)
        async def runner(*args: Any, **kwargs: Any) -> T:
            await Hare.init(HareConfig.from_db_url(MEMORY_SQLITE, {"models": model_modules}))
            await Hare.generate_schemas()
            return await function(*args, **kwargs)

        return runner

    default_models = ["__main__"]
    if inspect.iscoroutinefunction(models):
        return wrapper(models, default_models)
    if models is None:
        models = default_models
    elif isinstance(models, str):
        models = [models]
    else:
        models = cast("list[str]", models)
    return partial(wrapper, model_modules=models)


@asynccontextmanager
async def hare_test_context(
    modules: list[str],
    db_url: str = "sqlite+aiosqlite://:memory:",
    app_label: str = "models",
    *,
    connection_label: str | None = None,
    use_timezone: bool = True,
    timezone: str = DEFAULT_TIMEZONE,
    routers: list[str | type] | None = None,
    _create_db: bool = True,
    _generate_schemas: bool = True,
    _drop_db_on_exit: bool = True,
    reuse_databases: bool | None = None,
) -> AsyncGenerator[HareContext]:
    """An isolated test database: its own context, registry, database (created, and dropped on exit)
    and settings - safe under xdist.

    Example::

        @pytest_asyncio.fixture
        async def db():
            async with hare_test_context(["myapp.models"]) as context:
                yield context

    Args:
        modules: The module paths models are discovered in.
        db_url: The database URL - in-memory SQLite by default.
        app_label: The models' app label, "models" by default.
        connection_label: The connection alias, "default" by default.
        use_timezone: Make datetime fields timezone-aware.
        timezone: The timezone, "UTC" by default.
        routers: Router paths or classes.
        _create_db: False connects to an existing database another context created and keeps.
        _generate_schemas: False skips schema generation.
        _drop_db_on_exit: False leaves the database in place on exit - its owner drops it.
        reuse_databases: Whether a Postgres database name's "{}" is filled with a
            ``ReusableTestDatabases`` slot - reset and reused instead of created and dropped. None
            reads HARE_TEST_REUSE_DATABASES.

    Yields:
        The initialized context.
    """
    config = DbUrlConfigGenerator.build(db_url, app_modules={app_label: modules}, connection_label=connection_label)
    async with TemporaryDatabases(
        config,
        db_url,
        create_databases=_create_db,
        generate_schemas=_generate_schemas,
        drop_databases=_drop_db_on_exit,
        reuse_databases=reuse_databases,
        use_timezone=use_timezone,
        timezone=timezone,
        routers=routers,
    ) as context:
        yield context
