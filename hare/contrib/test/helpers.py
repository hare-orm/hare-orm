from __future__ import annotations

import inspect
import typing
import warnings
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from functools import partial, wraps
from typing import TYPE_CHECKING, ParamSpec, TypeVar, cast
from unittest import SkipTest

from hare import Hare
from hare.contrib.test.constants import MEMORY_SQLITE
from hare.contrib.test.query_counter import QueryCounter
from hare.core.config import HareConfig
from hare.core.context import HareContext
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.exceptions import ConfigurationError
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.utils.constants import DEFAULT_TIMEZONE
from hare.warnings import HareLoopSwitchWarning

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.models import Model

T = TypeVar("T")
P = ParamSpec("P")
AsyncFunc = Callable[P, Coroutine[None, None, T]]
AsyncFuncDeco = Callable[..., AsyncFunc[..., typing.Any]]
ModulesConfigType = str | list[str]
FT = Callable[..., typing.Any]


async def truncate_all_models(context: HareContext | None = None) -> None:
    """Deletes every row of every registered model's table - not of an unmanaged model or a swapped
    one. Each connection's dialect empties its tables (``Dialect.clear_tables()``), given in
    foreign-key order and schema-qualified.

    Args:
        context: Empty this context's models on its default connection, instead of the current
            context's models each on its own connection.

    Raises:
        ConfigurationError: The models aren't loaded.
    """
    apps = context.apps if context is not None else Hare.apps
    if not apps:
        raise ConfigurationError("apps are not loaded")
    connection = context.db() if context is not None else None

    models = list(apps.get_models_iterable())

    if not models:
        return

    # Models can belong to different connections (multi-DB setups) - each connection needs its
    # own dialect check and its own TRUNCATE/DELETE statements, run against that connection.
    models_by_connection: dict[DatabaseClient, list[type[Model]]] = {}
    for model in models:
        # A Meta.managed = False model's table isn't hare's - it may be a view, or not exist - and a
        # swapped model has none.
        if model._meta.managed is False or model._meta.swapped is not None:
            continue
        models_by_connection.setdefault(connection or model._meta.db, []).append(model)

    for db, connection_models in models_by_connection.items():

        def quote_table(schema: str | None, table: str) -> str:
            # A dialect without schemas ignores Meta.schema in its DDL, so here too.
            if schema and db.dialect.supports_schemas:
                return f"{db.dialect.quote_identifier(schema)}.{db.dialect.quote_identifier(table)}"
            return db.dialect.quote_identifier(table)

        # Auto-created M2M through tables aren't registered models - each is created by the model
        # declaring its relation, so it's collected from that side only. They reference the
        # models, so they go first.
        auto_through_tables: dict[tuple[str | None, str], None] = {}
        for model in connection_models:
            for m2m_field_name in sorted(model._meta.m2m_fields):
                m2m_field = cast("ManyToManyFieldInstance[typing.Any]", model._meta.fields_map[m2m_field_name])
                if m2m_field._generated or m2m_field.through_model is not None or not m2m_field.through:
                    continue
                auto_through_tables[(m2m_field.through_schema, m2m_field.through)] = None
        quoted_tables = [
            *(quote_table(schema, table) for schema, table in auto_through_tables),
            *(
                quote_table(model._meta.schema, model._meta.db_table)
                for model in topological_sort_models(connection_models)
            ),
        ]
        await db.dialect.clear_tables(db, list(dict.fromkeys(quoted_tables)))


def topological_sort_models(models: list[type[Model]]) -> list[type[Model]]:
    """Sorts models so that a model comes before the models it references - the order rows can be
    deleted in.
    """
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance

    model_set = set(models)
    # Build adjacency for delete order: parent -> children that must be deleted first
    # If Event has FK to Tournament, then Tournament depends on Event being deleted first
    deps: dict[type[Model], set[type[Model]]] = {m: set() for m in models}
    # Reverse of deps: for a model, which other models' dep-sets does it appear in.
    dependents: dict[type[Model], list[type[Model]]] = {m: [] for m in models}
    for model in models:
        for field in model._meta.fields_map.values():
            if isinstance(field, ForeignKeyFieldInstance):
                related = field.related_model
                if related in model_set and related is not model:
                    deps[related].add(model)
                    dependents[model].append(related)

    # Kahn's algorithm with a stack: models whose dependencies are emitted are emitted, walking only
    # each model's dependents.
    sorted_models: list[type[Model]] = []
    emitted: set[type[Model]] = set()
    stack = [model for model in models if not deps[model]]
    while stack:
        model = stack.pop()
        if model in emitted:
            continue
        emitted.add(model)
        sorted_models.append(model)
        for other in dependents[model]:
            deps[other].discard(model)
            if not deps[other] and other not in emitted:
                stack.append(other)

    # Append any remaining (circular deps — fallback)
    for model in models:
        if model not in emitted:
            sorted_models.append(model)

    return sorted_models


def requires_features(connection_name: str | None = None, **conditions: typing.Any) -> Callable[[FT], FT]:
    """Skips a test unless the connection's features match.

    The database must be initialized before the decorated test runs.

    Args:
        connection_name: The connection to check - the current context's default connection
            (else its first configured one) when None.
        conditions: Values by name, all of which must match for the test to run - of the
            connection's ``Features`` (``supports_transactions``), else of its ``Dialect``
            (``supports_distinct_on``, ``supports_partial_indexes``), and ``dialect`` for the dialect's name.
            A test needing a capability names the capability, so any dialect having it runs the
            test; ``dialect`` is for a test of one dialect's own SQL or types.

    Example:
        @requires_features(dialect="sqlite")
        @pytest.mark.asyncio
        async def test_run_sqlite_only(db): ...

        Or to conditionally skip a class:

        @requires_features(supports_transactions=True)
        class TestTransactions:
            @pytest.mark.asyncio
            async def test_something(self, db): ...
    """

    def decorator(test_item: FT) -> FT:
        if not isinstance(test_item, type):

            def check_features() -> None:
                context = HareContext.require_current()
                db = context.db(connection_name or context.default_connection or context.connections.aliases()[0])
                for key, val in conditions.items():
                    if key == "dialect":
                        actual = db.dialect.name
                    elif hasattr(db.features, key):
                        actual = getattr(db.features, key)
                    else:
                        actual = getattr(db.dialect, key)
                    if actual != val:
                        raise SkipTest(f"{key} != {val}")

            if inspect.iscoroutinefunction(test_item):

                @wraps(test_item)
                async def skip_wrapper(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
                    check_features()
                    return await test_item(*args, **kwargs)

            else:

                @wraps(test_item)
                def skip_wrapper(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
                    check_features()
                    return test_item(*args, **kwargs)

            return cast("FT", skip_wrapper)

        # Assume a class is decorated
        funcs = {
            var: f for var in dir(test_item) if var.startswith("test_") and callable(f := getattr(test_item, var))
        }
        for name, func in funcs.items():
            setattr(
                test_item,
                name,
                requires_features(connection_name=connection_name, **conditions)(func),
            )

        return test_item

    return decorator


@typing.overload
def init_memory_sqlite(models: ModulesConfigType | None = None) -> AsyncFuncDeco: ...


@typing.overload
def init_memory_sqlite(models: AsyncFunc[..., typing.Any]) -> AsyncFunc[..., typing.Any]: ...


def init_memory_sqlite(
    models: ModulesConfigType | AsyncFunc[..., typing.Any] | None = None,
) -> AsyncFunc[..., typing.Any] | AsyncFuncDeco:
    """Decorator for initializing Hare with an in-memory SQLite database.

    This is useful for simple scripts and examples that need a quick database setup.

    Args:
        models: List of modules to load models from. Defaults to ["__main__"].

    Example:
        from hare import fields, models, run_async
        from hare.contrib.test import init_memory_sqlite


        class MyModel(models.Model):
            id = fields.IntField(primary_key=True)
            name = fields.TextField()


        @init_memory_sqlite
        async def run():
            obj = await MyModel.objects.create(name="")
            assert obj.id == 1


        if __name__ == "__main__":
            run_async(run())

        Custom models example:

        @init_memory_sqlite(models=["app.models", "app.other_models"])
        async def run(): ...
    """

    def wrapper(func: AsyncFunc[..., typing.Any], model_modules: list[str]):
        @wraps(func)
        async def runner(*args, **kwargs) -> T:
            await Hare.init(HareConfig.from_db_url(MEMORY_SQLITE, {"models": model_modules}))
            await Hare.generate_schemas()
            return await func(*args, **kwargs)

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
async def capture_queries(using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]:
    """Counts every query hare sends to ``using`` from the block and the tasks it starts - every
    ``QueryExecuted`` of the connection, COPY included.

    Args:
        using: A connection alias or client - the current context's default connection by default.

    Example::

        async with capture_queries() as counter:
            await Event.objects.all().select_related("tournament")
        assert counter.count == 1
    """
    db = cast("DatabaseClient", using if hasattr(using, "execute") else HareContext.require_current().db(using))
    counter = QueryCounter(db.connection_name)
    with Observers.observing(QueryExecuted, counter.record):
        yield counter


@asynccontextmanager
async def assert_num_queries(
    expected: int, *, using: str | DatabaseClient | None = None
) -> AsyncGenerator[QueryCounter]:
    """Asserts the block runs exactly ``expected`` queries against ``using`` - an N+1 regression fails
    the test. The failure lists every captured query's SQL.

    Args:
        expected: The number of queries.
        using: A connection alias or client - the current context's default connection by default.

    Example::

        async with assert_num_queries(1):
            await Event.objects.all().select_related("tournament")
    """
    async with capture_queries(using) as counter:
        yield counter
    if counter.count != expected:
        listed = "\n".join(f"  {i + 1}. {q}" for i, q in enumerate(counter.queries)) or "  (none)"
        plural = "y" if expected == 1 else "ies"
        raise AssertionError(f"Expected {expected} quer{plural}, executed {counter.count}:\n{listed}")


@asynccontextmanager
async def hare_test_context(
    modules: list[str],
    db_url: str = "sqlite://:memory:",
    app_label: str = "models",
    *,
    connection_label: str | None = None,
    use_tz: bool = True,
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
            async with hare_test_context(["myapp.models"]) as ctx:
                yield ctx

    Args:
        modules: The module paths models are discovered in.
        db_url: The database URL - in-memory SQLite by default.
        app_label: The models' app label, "models" by default.
        connection_label: The connection alias, "default" by default.
        use_tz: Make datetime fields timezone-aware.
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
    from hare.contrib.test.reusable_databases import ReusableTestDatabases

    is_reusing_databases = ReusableTestDatabases.is_enabled(reuse_databases)
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=HareLoopSwitchWarning)
        ctx = HareContext()
        async with ctx:
            with ReusableTestDatabases.track_new_leases() as own_lease_number_by_database_name:
                config = DbUrlConfigGenerator.build(
                    db_url,
                    app_modules={app_label: modules},
                    connection_label=connection_label,
                    testing=True,
                    reuse_databases=is_reusing_databases,
                )
            try:
                await ctx.init(
                    config=config,
                    _create_db=_create_db,
                    use_tz=use_tz,
                    timezone=timezone,
                    routers=routers,
                )
                try:
                    # Inside the try: a failed schema generation must still drop the database it
                    # was generating into.
                    if _generate_schemas:
                        await ctx.generate_schemas(safe=False)
                    yield ctx
                finally:
                    # A database this context created is dropped - unless the caller keeps it
                    # (_drop_db_on_exit=False).
                    await ctx.connections.close_all(discard=False)
                    if _drop_db_on_exit:
                        for conn in ctx.connections.all():
                            await conn.db_delete()
                            ctx.connections.discard(conn.connection_name)
            finally:
                # A reusable database whose db_delete() never ran (a failed init, an earlier
                # connection's failed db_delete()) goes back to the pool marked for a reset.
                if _drop_db_on_exit:
                    ReusableTestDatabases.release_leases(own_lease_number_by_database_name)
