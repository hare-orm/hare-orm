# Configuration and `Hare.init()`

How an application starts hare and stops it: `Hare.init()` and the `config` dict it takes, the
`HareContext` a process or a test runs in, querysets made before `init()`, binding the models without a
database, closing the connections, and `Hare.run_async()` for a script.

## <a id="hare-init"></a>`Hare.init()`

Every hare-orm application starts by calling `Hare.init()`, which registers your models, sets up
the database connections, and returns a `HareContext` you should keep around for the app's
lifetime. A connection (or its pool) is opened on its first query, not by `init()`.

```python
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
) -> HareContext
```

There is one way to say where the configuration is — the `config` argument — and it takes every
form (`HareConfig.load(source)` is what reads it, for `Hare.init()` and the CLI alike):

```python
await Hare.init({"connections": {...}, "apps": {...}})            # a dict - see the shape below
await Hare.init(HareConfig(connections=..., apps=...))            # the typed equivalent
await Hare.init("config/hare.yml")                                # a .json/.yml/.yaml file of the same shape
await Hare.init("settings.HARE_ORM")                              # module.VARIABLE holding a dict or a HareConfig
await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["my_app.models"]}))  # one URL
```

`HareConfig.from_db_url(db_url, modules)` builds the configuration of one connection (`"default"`)
from a URL and `{app_label: [module paths]}`. A source that can't be read — a missing file, a module
or variable that doesn't exist, a variable holding something else — raises `ConfigurationError`.

Keyword arguments are checked too: `use_timezone` must be a `bool`, `table_name_generator` a callable,
`slow_query_threshold_ms` a finite number from `0` to `86400000`. The process-wide settings
(`slow_query_threshold_ms`, the class-level `table_name_generator`) change only once `init()`
succeeds — a failed `init()` leaves them as they were.

| Parameter | Meaning |
|---|---|
| `config` | A dict, a `HareConfig`, a file path or `"module.VARIABLE"` — see above; the shape is below. |
| `connect` | `False` sets the models and connections up without connecting to any database — the connection names are still checked against the configuration. For tools that need the models and their SQL but no database: generating migrations, rendering DDL. |
| `_create_db` | Create the database itself first (handy in tests). |
| `use_timezone` / `timezone` | Timezone handling for `DatetimeField`. |
| `routers` | A list of router classes or dotted paths — see [Routers](multiple-databases.md#routers). |
| `table_name_generator` | `Callable[[type[Model]], str]` to override the default table-naming scheme. A generated name is recomputed by every `init()` (a later `init()` with another generator, or none, renames the model's table); an explicit `Meta.table` always wins. Names derived from the table when relations were first set up — an auto-created M2M through table, its columns, a default `related_name` — keep that first value. |
| `slow_query_threshold_ms` | Queries at or above this duration get a DEBUG-level "Slow query" log line. Defaults to 500ms — see [Slow-query logging](../observability/observers.md#slow-query-logging). |
| `_enable_global_fallback` | Lets code running outside the task that called `init()` (e.g. a background task started by an ASGI lifespan) still find the current context. The Litestar, FastAPI and Robyn integrations turn it on themselves. |

## <a id="the-config-dict"></a>The `config` dict

```python
config = {
    "connections": {
        "default": "postgresql://postgres:qwerty123@localhost:5432/my_db",  # a bare DB URL string...
        "reporting": {  # ...or the explicit dict form
            "credentials": {
                "host": "localhost", "port": "5432",
                "user": "postgres", "password": "qwerty123", "database": "reporting",
            },
        },
    },
    "apps": {
        "my_app": {
            "models": ["my_app.models"],
            "default_connection": "default",   # optional, defaults to "default"
            "migrations": "my_app.migrations",  # optional, used by the CLI
        },
    },
    "routers": ["my_app.routers.ReportingRouter"],
    "use_timezone": True,
    "timezone": "UTC",
    "swappable": {"USER_MODEL": "my_app.User"},  # optional, see below
    "migrations": {"lock_timeout": 5},  # optional, see below
}

context = await Hare.init(config=config)
```

A typed equivalent, `hare.core.config.HareConfig`, is available if you prefer to construct config
programmatically: `HareConfig(connections=..., apps=..., routers=None, read_your_writes_seconds=None,
use_timezone=None, timezone=None, cli=None, swappable=None, migrations=None)` (`read_your_writes_seconds` — see
[Reading your own writes](multiple-databases.md#read-your-writes)),
with `ConnectionConfig(engine=None, credentials={}, db_url=None)`, `DBUrlConfig(url)`,
`AppConfig(models=[...], default_connection=None, migrations=None)` and `CliConfig(commands=[...])`
(the `cli` section). A connection may also be given
as `{"db_url": "..."}` (what `ConnectionConfig(db_url=...)` serializes to). `models` must be a
list/tuple of module paths — a bare string raises `ConfigurationError` instead of being read
character by character.

`migrations` takes `lock_timeout`: seconds a migration's statement may wait for a lock another
session holds before `migrate` fails — see [Lock timeout](../migrations/migrations.md#lock-timeout);
and `safety`, the migration safety check's settings: `{"large_table_rows": 100000}` — the rows from
which `checkmigrations` counts a table as large, a whole number from 0 to 10<sup>12</sup> — see
[Checking migrations](../migrations/zero-downtime.md#checking-migrations). Typed:
`MigrationsConfig(lock_timeout=None, safety=None)` and `MigrationSafetyConfig(large_table_rows=100000)`.

The keys of a config mapping are the fields of these classes: the top level takes the fields of
`HareConfig`, a connection written as a mapping those of `ConnectionConfig`, an app those of
`AppConfig`, the `cli` section those of `CliConfig`. Any other key raises `ConfigurationError`
naming the closest known key — a misspelt `default_conection` would otherwise leave the app's models
on the default connection without a word.

`repr()` of these config objects (and the connection config hare-orm writes to its DEBUG log at
`init()`) masks every secret as `***`: the password in a DB URL, a secret query parameter
(`sslpassword=...`) and every credential whose name contains `password`, `passwd`, `pwd`, `secret`
or `token`. No part of the secret is shown.

An unknown `engine` raises `ConfigurationError`, as does a connection parameter the backend doesn't
know: a misspelled credential (`comand_timeout`) is rejected by both PostgreSQL drivers — the Rust
driver accepts only its own parameters, asyncpg only what `asyncpg.connect()`/`create_pool()` take.

A relation with `db_constraint=True` (the default) between models whose apps live on different
connections raises `ConfigurationError` at `init()` — a database foreign key can't reference a
table in another database. Declare such a relation with `db_constraint=False`; hare-orm then runs
its `on_delete` itself.

`swappable` maps a setting name to the `"app_label.ModelName"` of the model that relations declared
with `swappable("USER_MODEL")` point at — the way a project replaces a package's default model with
its own (see [Swappable models](../models/relations.md#swappable-models)). A setting name
is an upper-case identifier (`USER_MODEL`) and its value names a configured app; `init()` also checks
that the model is registered, isn't abstract and isn't itself swapped for another one by its own
setting. Each mistake raises its own `ConfigurationError`. `Hare.swappable_label("USER_MODEL")`
returns the label a setting points at and `Hare.get_swappable_model("USER_MODEL")` the model; a
setting left out of the config points at the model declaring `Meta.swappable = "USER_MODEL"`.
A setting may instead hold a dict of branch name to label —
`{"COMMENT_TARGETS": {"post": "blog.Post", "photo": "media.Photo"}}` — the targets of a
[`GenericForeignKeyField(swappable("COMMENT_TARGETS"))`](../models/relations.md#generic-foreign-key-swappable);
each branch name is a Python identifier and each label is checked as above. Such a setting must be
configured, and only a generic foreign key takes it.

## <a id="hare-context"></a>`HareContext`

```python
context = await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["my_app.models"]}))

# later, on shutdown:
await context.close_connections()
```

`HareContext` is also a context manager, which is the more common shape for tests and scripts:

```python
async with HareContext() as context:
    await context.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["my_app.models"]}))
    await context.generate_schemas()
    ...
```

`Hare` is the context of the running task under a fixed name: `Hare.init()` sets up the task's
current `HareContext` (a new one when the task has none) and every other `Hare` method acts on the
current one. Everything is defined once, on `HareContext`: `.init(config, ...)`,
`.generate_schemas(safe=True)`, `.close_connections()`, `.connections` (a `ConnectionHandler`),
`.apps`, `.router`, `.routers`, `.inited`, `.default_connection`, `.use_timezone`, `.timezone`,
`.get_model(app_label, model_name)`, `.get_connection(connection_alias=None)`, `.bind_models(config, ...)`, `.swappable_label(setting)`,
`.get_swappable_model(setting)`, `.register_live_models(...)`, `.unregister_live_models(...)` and
`.observe(event_type, callback)`/`.unobserve(...)` (see
[Observing the ORM](../observability/observers.md#where-observers-live)).
`HareContext.get_current()` returns the current context or `None`, `HareContext.require_current()` the
current context or raises `ConfigurationError`.

Model classes are shared by every context that loads their module: a context set up inside
another one binds them (connection, table names, relations) to its own configuration, and leaving
it — `with` or `async with` — binds them back to the context current again.

## <a id="querysets-before-init"></a>Querysets built before `init()`

A queryset may be built before `Hare.init()` — at import time, as a module-level constant or a
class attribute:

```python
PUBLISHED = Book.objects.filter(published=True).order_by("-rating")   # no init() yet


async def list_published() -> list[Book]:
    return await PUBLISHED.limit(20)          # after init(): an ordinary queryset
```

Until the models are bound the chain can't be checked against them (relations aren't set up yet),
so each such queryset is remembered, and `init()` replays its calls right after binding the models.
From then on it is an ordinary, fully built queryset — nothing about it is lazy. A mistake in such
a chain (an unknown field, a wrong lookup) therefore raises from `Hare.init()` (or
`Hare.bind_models()`), with the same error the call would raise after `init()`. Running a query
before `init()` raises `ConfigurationError`.

## <a id="bind-models"></a>Binding the models without a database

```python
Hare.bind_models(config, table_name_generator=None)   # synchronous
```

Binds the models of a configuration — relations, swappable models, filters and orderings — without
connecting and without leaving a context current. It is for code that needs the models before the
application starts: a web framework building its handlers' signatures from
[`lookup_info`](../querying/describing-filters.md) at import time. `Hare.init()` later sets the
connections up as usual. `await Hare.init(config, connect=False)` is the asynchronous form that
does leave a context current — with models, routers and connection settings, but no connection made.

## <a id="closing-connections"></a>Closing connections

```python
await Hare.close_connections()
```

## <a id="other-hare-classmethods"></a>Other `Hare` classmethods

- `Hare.generate_schemas(safe=True)` — create tables for all registered models directly (no
  migrations) — handy for tests and prototyping, not for production.
- `connection.get_schema_sql(safe)` — returns the same DDL as a string without running
  it, if you just want to inspect/log/diff it.
- `Hare.is_inited()`, `Hare.apps` (the models of the current context, `None` without one),
  `Hare.get_context()` (the current `HareContext`; `ConfigurationError` without one).
- `Hare.swappable_label(setting)` / `Hare.get_swappable_model(setting)` — see
  [Swappable models](../models/relations.md#swappable-models).
- `Hare.register_live_models(models, app_label, connection_alias="default", *, managed=None)` /
  `Hare.unregister_live_models(models)` — see
  [Registering a model at runtime](../models/runtime-models.md).

To describe a model — its fields, relations, the filters and orderings each accepts — read
`Model._meta` and [`lookup_info`](../querying/describing-filters.md); a field's constructor arguments come
from `field.deconstruct()`.

> [!NOTE]
> A test helper, `hare_test_context(modules, db_url="sqlite+aiosqlite://:memory:", ...)`, wraps the whole
> init/generate-schemas/teardown cycle as an async context manager — use it in your own test
> fixtures instead of reimplementing this dance. See [Testing](../testing/setup.md) for this
> and the rest of `hare.contrib.test`.

## <a id="run-async"></a>Scripts: `Hare.run_async()`

For a one-off script (not a long-running app or a test), `Hare.run_async()` wraps `asyncio.run` and closes
connections on exit for you:

```python
from hare import Hare, HareConfig


async def main() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["app.models"]}))
    ...


Hare.run_async(main())
```
