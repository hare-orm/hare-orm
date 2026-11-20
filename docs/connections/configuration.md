# Configuration and `Hare.init()`

How an application starts hare and stops it: `Hare.init()` and the `config` dict it takes, the
`HareContext` a process or a test runs in, querysets made before `init()`, binding the models without a
database, closing the connections, and `Hare.run_async()` for a script.

## <a id="hare-init"></a>`Hare.init()`

Every hare application starts by calling `Hare.init()`, which registers your models, sets up the database
connections, and returns a `HareContext` you should keep around for the app's lifetime. A connection (or
its pool) is opened on its first query, not by `init()`.

```python
class Hare:
    @classmethod
    async def init(
        cls,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        connect: bool = True,
        _create_db: bool = False,
        use_timezone: bool = True,
        timezone: str = "UTC",
        routers: list[str | Callable[[], object]] | None = None,
        table_name_generator: Callable[[type[Model]], str] | None = None,
        slow_query_threshold_ms: float = 500.0,
        _enable_global_fallback: bool = False,
    ) -> HareContext: ...
```

There is one way to say where the configuration is — the `config` argument — and it takes every form
(`HareConfig.load(source)` is what reads it, for `Hare.init()` and the CLI alike):

```python
await Hare.init({"connections": {...}, "apps": {...}})            # a dict - see the shape below
await Hare.init(HareConfig(connections=..., apps=...))            # the typed equivalent
await Hare.init("config/hare.yml")                                # a .json/.yml/.yaml file of the same shape
await Hare.init("settings.HARE_ORM")                              # module.VARIABLE holding a dict or a HareConfig
await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["library.models"]}))  # one URL
```

`HareConfig.from_db_url(db_url, modules)` builds the configuration of one connection (`"default"`) from a
URL and `{app_label: [module paths]}`. A source that can't be read — a missing file, a module or variable
that doesn't exist — raises `ConfigurationError`.

`table_name_generator` belongs to the context and its model registry. The context publishes it,
and updates the process-wide `slow_query_threshold_ms`, only after `init()` succeeds.

Each registry prepares a separate graph of bindings from Python class declarations. The same model
class can belong to concurrent contexts with different tables, applications and connections. The
class holds its declaration; prepared fields, relations and SQL metadata belong to the `ModelBinding`
returned by `context.apps.get_binding(Book)`. Leaving a nested context restores the task's current
context without replacing class attributes.

A query and an instance retain their originating graph and context. Executing an earlier query under
another current context uses its original application, including routing, encryption and timezone
settings. Explicitly bound queries and related instances from different graphs or contexts cannot be
combined. An unbound nested template takes the outer query's graph.

A database client belonging to another `HareContext` cannot be passed to that query or
instance: `ConfigurationError` is raised before SQL, even if both clients have the same alias.
Build the query through the other context's registry to use its database. Transactions,
independent clients and tenant clients retain the context of the connection they came from.

Fields and Meta options are captured when the Python class is created, including nested mutable schema
options. Later changes to the original Meta or option objects do not reconfigure existing or future
graphs. Callbacks retain their normal identity and execute under the selected application's settings.

Registry getters only read registered nodes. Preparation adds nodes explicitly and permits no database
execution through an unpublished graph, including objects retained by a failed extension hook.
An instance's `save()` rejects such a graph before awaiting defaults or preparing field values.
Every registry preparation, including unregistration, runs its callbacks in that registry's
application context and restores the caller's current context on success or failure.
Manager and request-schema caches follow their registry's published version; preparing a historical
registry does not invalidate another registry's entries.

A detached view or projection declaration needs an explicit database client to compile SQL. Bound
sources retain their originating settings and reject clients belonging to another application.
An offline client with no connection handler can compile detached declarations without an
application context.

Registration and unregistration atomically publish a complete new graph. Failure preserves the
previous graph. Existing queries and instances retain their earlier graph; external database schema
changes can still invalidate their SQL. Every relation target must belong to the selected registry:
a Python class reference does not borrow another application's configuration.

A `HareContext` is initialized once per lifetime. Create another context for a different configuration.
Queries and objects cannot perform database IO after their originating context has closed.

| Argument | Meaning |
|---|---|
| `config` | A dict, a `HareConfig`, a file path or `"module.VARIABLE"` — see above; the shape is below. |
| `connect` | `False` sets the models and connections up without connecting to any database — the connection names are still checked against the configuration. For tools that need the models and their SQL but no database: generating migrations, rendering DDL. |
| `_create_db` | Create the database itself first (handy in tests). |
| `use_timezone` / `timezone` | Timezone handling for `DatetimeField`. |
| `routers` | Router classes or zero-argument factories, directly or by dotted path — see [Routers](multiple-databases.md#routers). |
| `table_name_generator` | `Callable[[type[Model]], str]` to override the default table-naming scheme — see [`table_name_generator`](#table-name-generator) |
| `slow_query_threshold_ms` | Queries at or above this duration get a DEBUG-level "Slow query" log line. Defaults to 500ms — see [Slow-query logging](../observability/observers.md#slow-query-logging). |
| `_enable_global_fallback` | Lets code running outside the task that called `init()` (e.g. a background task started by an ASGI lifespan) still find the current context. The Litestar, FastAPI and Robyn integrations turn it on themselves. |

### <a id="table-name-generator"></a>`table_name_generator`

`Callable[[type[Model]], str]` supplies table names for one registry. An explicit `Meta.table`
takes precedence. Another context may use another generator for the same classes; table names
and automatically derived relation names are prepared independently in each graph.

## <a id="the-config-dict"></a>The `config` dict

The configuration `Hare.init()` takes as a dict — its connections, its apps and the settings of the whole
context:

```python
config = {
    "connections": {
        "default": "postgresql://postgres:qwerty123@localhost:5432/library",  # a bare database URL string...
        "reporting": {  # ...or the explicit dict form - the engine is the scheme a URL would have
            "engine": "postgresql",
            "credentials": {
                "host": "localhost", "port": "5432",
                "user": "postgres", "password": "qwerty123", "database": "reporting",
            },
        },
    },
    "apps": {
        "library": {
            "models": ["library.models"],
            "default_connection": "default",   # optional, defaults to "default"
            "migrations": "library.migrations",  # optional, used by the CLI
        },
    },
    "routers": ["library.routers.ReportingRouter"],
    "use_timezone": True,
    "timezone": "UTC",
    "swappable": {"USER_MODEL": "library.Reader"},  # optional, see below
    "migrations": {"lock_timeout": 5},  # optional, see below
}

context = await Hare.init(config=config)
```

### <a id="config-classes"></a>`HareConfig` and the other configuration classes

```python
class HareConfig:
    def __init__(
        self,
        connections: dict[str, ConnectionConfig | DBUrlConfig],
        apps: dict[str, AppConfig],
        routers: list[str | Callable[[], object]] | None = None,
        read_your_writes_seconds: float | None = None,
        use_timezone: bool | None = None,
        timezone: str | None = None,
        cli: CliConfig | None = None,
        swappable: dict[str, str | dict[str, str]] | None = None,
        migrations: MigrationsConfig | None = None,
    ) -> None: ...

    @classmethod
    def from_db_url(cls, db_url: str, modules: Mapping[str, Iterable[str | ModuleType]]) -> Self: ...

    @classmethod
    def load(cls, source: Mapping[str, Any] | Self | str) -> Self: ...


class ConnectionConfig:
    def __init__(
        self, engine: str | None = None, credentials: dict[str, Any] = {}, db_url: str | None = None
    ) -> None: ...


class DBUrlConfig:
    def __init__(self, url: str) -> None: ...


class AppConfig:
    def __init__(
        self,
        models: Iterable[str | ModuleType],
        default_connection: str | None = None,
        migrations: str | None = None,
    ) -> None: ...


class CliConfig:
    def __init__(self, commands: list[str] = []) -> None: ...


class MigrationsConfig:
    def __init__(self, lock_timeout: float | None = None, safety: MigrationSafetyConfig | None = None) -> None: ...


class MigrationSafetyConfig:
    def __init__(self, large_table_rows: int = 100000) -> None: ...
```

The configuration can be built in code from these classes of `hare.core.config` instead of a dict.
`read_your_writes_seconds` — see [Reading your own writes](multiple-databases.md#read-your-writes); `cli`
holds extra `hare` subcommands. A connection may also be given as `{"db_url": "..."}` (what
`ConnectionConfig(db_url=...)` serializes to). `models` accepts an iterable of module paths or module
objects, for example a list or tuple; the iterable is consumed once.

### <a id="migrations-settings"></a>The `migrations` section

`migrations` takes `lock_timeout`: seconds a migration's statement may wait for a lock another session
holds before `migrate` fails — see [Lock timeout](../migrations/migrations.md#lock-timeout); and
`safety`, the migration safety check's settings: `{"large_table_rows": 100000}` — the rows from which
`checkmigrations` counts a table as large — see [Checking
migrations](../migrations/zero-downtime.md#checking-migrations).

### <a id="swappable-settings"></a>The `swappable` section

`swappable` maps a setting name to the `"app_label.ModelName"` of the model that relations declared with
`swappable("USER_MODEL")` point at — the way a project replaces a package's default model with its own
(see [Swappable models](../models/relations.md#swappable-models)). A setting name is an upper-case
identifier (`USER_MODEL`) and its value names a configured app; `init()` also checks that the model is
registered, isn't abstract and isn't itself swapped for another one by its own setting. Each mistake
raises its own `ConfigurationError`. `Hare.swappable_label("USER_MODEL")` returns the label a setting
points at and `Hare.get_swappable_model("USER_MODEL")` the model; a setting left out of the config points
at the model declaring `Meta.swappable = "USER_MODEL"`. A setting may instead hold a dict of branch name
to label — `{"COMMENT_TARGETS": {"post": "blog.Post", "photo": "media.Photo"}}` — the targets of a
[`GenericForeignKeyField(swappable("COMMENT_TARGETS"))`](../models/relations.md#generic-foreign-key-swappable);
each branch name is a Python identifier and each label is checked as above. Such a setting must be
configured, and only a generic foreign key takes it.

### <a id="config-checks"></a>Checks at `init()`

The keys of a config mapping are the fields of these classes: the top level takes the fields of
`HareConfig`, a connection written as a mapping those of `ConnectionConfig`, an app those of `AppConfig`,
the `cli` section those of `CliConfig`. Any other key raises `ConfigurationError` naming the closest
known key — a misspelt `default_conection` would otherwise leave the app's models on the default
connection without a word. The config, its `connections` and `apps` sections and each app use mapping
operations; each connection is a mapping or URL. Compatible mapping objects are accepted without a
class check. Invalid Python structures may raise their natural `TypeError` or `AttributeError`.

An unknown `engine` raises `ConfigurationError`, as does a connection parameter the database doesn't
know: a misspelled credential (`comand_timeout`) is rejected by both PostgreSQL drivers — `rust_pg`
accepts only its own parameters, asyncpg only what `asyncpg.connect()`/`create_pool()` take.

A relation with `db_constraint=True` (the default) between models whose apps live on different
connections raises `ConfigurationError` at `init()` — a database foreign key can't reference a table in
another database. Declare such a relation with `db_constraint=False`; hare then runs its `on_delete`
itself.

### <a id="masked-secrets"></a>Secrets in `repr()` and the log

`repr()` of these config objects (and the connection config hare writes to its DEBUG log at `init()`)
masks every secret as `***`: the password in a database URL, a secret query parameter (`sslpassword=...`)
and every credential whose name contains `password`, `passwd`, `pwd`, `secret` or `token`. No part of the
secret is shown.

## <a id="hare-context"></a>`HareContext`

```python
context = await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["library.models"]}))

# later, on shutdown:
await context.close_connections()
```

`HareContext` is also a context manager, which is the more common shape for tests and scripts:

```python
async with HareContext() as context:
    await context.init(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["library.models"]}))
    await context.generate_schemas()
    ...
```

`Hare` is the context of the running task under a fixed name: `Hare.init()` sets up the task's current
`HareContext` (a new one when the task has none) and every other `Hare` method acts on the current one.
Everything is defined once, on `HareContext`: `.init(config, ...)`, `.generate_schemas(safe=True)`,
`.close_connections()`, `.connections` (a `ConnectionHandler`), `.apps`, `.router`, `.routers`,
`.inited`, `.default_connection`, `.use_timezone`, `.timezone`, `.get_model("app.Model")` (a label, a
model class or a `swappable()` setting),
`.get_connection(connection_alias=None)`, `.bind_models(config, ...)`, `.swappable_label(setting)`,
`.get_swappable_model(setting)`, `.register_live_models(...)`, `.unregister_live_models(...)` and
`.observe(event_type, callback)`/`.unobserve(...)` (see [Observing the
ORM](../observability/observers.md#where-observers-live)). `HareContext.get_current()` returns the
current context or `None`, `HareContext.require_current()` the current context or raises
`ConfigurationError`.

Contexts that load a model's module share its class. Each context has its own bindings for connection,
table names and relations. Leaving a nested context restores the outer context as current. Queries
already bound to a registry and model instances retain their original bindings.

`Model.objects` selects the active registry when creating a query; `apps.get_queryset(Model)`
selects an explicit registry. The low-level `QuerySet(Model)` constructor creates an unbound
declaration even when an application is active. Use the manager or the registry factory to retain
a particular application before executing it.

## <a id="querysets-before-init"></a>Querysets built before initialization

A query made before registry preparation remains an unchanged template:

```python
PUBLISHED = Book.objects.filter(published=True).order_by("-rating")

async def list_published() -> list[Book]:
    return await PUBLISHED.limit(20)
```

Execution replays the chain into a local copy using the current application's graph. `PUBLISHED`
itself is never changed and can be reused independently by another application. Deferred calls are
validated when bound, rather than by `init()`. For early validation, start a query through
`apps.get_queryset(Book)` on an explicitly prepared registry. Executing a template without an
application raises `ConfigurationError`.

## <a id="bind-models"></a>Preparing a registry without a database

```python
apps = Hare.bind_models(CONFIG)
binding = apps.get_binding(Book)
info = binding.get_lookup_info("author__name__icontains")
queryset = apps.get_queryset(Book).order_by("title")
book = apps.new(Book, title="Example")
trusted = apps.construct(Book, id=1, title="Example")
```

`bind_models()` synchronously prepares fields, relations and query descriptions without opening
connections or changing the current context. Use the returned `Apps` explicitly. `apps.new()` uses
the ordinary constructor, including a custom `__init__`; `apps.construct()` uses the ordinary trusted
constructor without validation. These objects have no application for IO. Use an initialized context's
registry or explicitly call `context.attach(book)` before database operations.

Model entries may be module objects, including modules absent from `sys.modules`. Failed preparation
does not change published graphs or deferred templates.

## <a id="closing-connections"></a>Closing connections

`Hare.close_connections()` closes every connection of the current context and drops them; without a
context it does nothing. How long it waits for the queries still running depends on the database:

| Database | Behavior |
|---|---|
| PostgreSQL | Each pool (`asyncpg` and `rust_pg` alike) waits up to 10 seconds for the connections its queries and transactions hold or are opening, then drops them: the server rolls back the transactions still open and ends their sessions |
| SQLite | The connection waits up to 10 seconds for the transaction or query using it, then closes anyway |
| ClickHouse | `clickhouse_driver` waits up to 10 seconds for the statements handed over already; one still running ends on its own. `clickhouse_connect` closes its HTTP client once the requests already sent are answered — they end with their results |

PostgreSQL pool shutdown waits for its active connection tasks to stop. Once closing returns, a
transaction retained by the caller cannot commit through one of those connections.

A task still holding one of the dropped connection objects gets `DBConnectionError` on its next query instead
of opening a new pool; a query through a model looks its connection up again — inside a transaction too — and
gets `ConfigurationError`, as before `Hare.init()`. The context forgets its connections even when a close
fails. A single failure is raised afterwards; multiple failures are retained in a `BaseExceptionGroup`.
Observers still flushing a metric or a trace get up to 10 seconds to finish.
`close_all()` of a `ConnectionHandler` is the same close for one context — see
[Connections](connections.md#connections).

```python
await Hare.close_connections()
```

## <a id="other-hare-classmethods"></a>Other `Hare` classmethods

- `Hare.generate_schemas(safe=True)` — create tables for all registered models directly (no migrations) —
  handy for tests and prototyping, not for production. Tables are created in the order their foreign keys
  need; models whose foreign keys point at each other in a cycle get one table of the cycle created
  without the foreign key to a table created later, which is added by `ALTER TABLE ... ADD CONSTRAINT`
  once that table exists (with `safe=True`, only when missing). SQLite checks a reference only when a row
  is written, so there each table keeps its `REFERENCES`.
- `connection.get_schema_sql(safe)` — returns the same DDL as a string without running it, if you just
  want to inspect/log/diff it.
- `Hare.is_inited()`, `Hare.apps` (the models of the current context, `None` without one),
  `Hare.get_context()` (the current `HareContext`; `ConfigurationError` without one).
- `Hare.swappable_label(setting)` / `Hare.get_swappable_model(setting)` — see
  [Swappable models](../models/relations.md#swappable-models).
- `Hare.register_live_models(models, app_label, connection_alias="default", *, managed=None)` /
  `Hare.unregister_live_models(models)` — see
  [Registering a model at runtime](../models/runtime-models.md).

To describe a model — its fields, relations, the filters and orderings each accepts — read
`apps.get_binding(Model)`
and [`lookup_info`](../querying/describing-filters.md); a field's constructor arguments come from
`field.deconstruct()`.

> [!NOTE]
> A test helper, `hare_test_context(modules, db_url="sqlite+aiosqlite://:memory:", ...)`, wraps the whole
> init/generate-schemas/teardown cycle as an async context manager — use it in your own test fixtures
> instead of reimplementing this dance. See [Testing](../testing/setup.md) for this and the rest of
> `hare.contrib.test`.

## <a id="run-async"></a>Scripts: `Hare.run_async()`

For a one-off script, `Hare.run_async()` runs the coroutine through an AnyIO blocking portal on a
separate thread and event loop, and closes its context's connections on exit:

```python
from hare import Hare, HareConfig


async def main() -> None:
    await Hare.init(HareConfig.from_db_url("sqlite+aiosqlite://db.sqlite3", {"models": ["app.models"]}))
    ...


Hare.run_async(main())
```
