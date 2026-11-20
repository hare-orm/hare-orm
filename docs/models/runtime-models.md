# Models registered at runtime

Registration and removal are coordinated by `LiveModelRegistration`. The registry binds
many-to-many declarations through `ManyToManyLinking` and checks table and schema object
name collisions through `ModelNameChecks`. Failed registration leaves the published graph intact;
registration and removal publish a new graph while existing queries and instances keep their binding.

A model class built while the application runs — from a table read off the database, say — is registered
after `Hare.init()` and removed again without restarting.

```python
from hare.inspectdb import DatabaseCatalog, ModelFactory

table_info = await DatabaseCatalog.inspect_table(connection, "loans")
Loan = ModelFactory.from_table_info(table_info, connection.dialect.name, app_label="live")
Hare.register_live_models([Loan], app_label="live", connection_alias="default", managed=False)
...
Hare.unregister_live_models([Loan])
```

## <a id="registering"></a>Registering: `register_live_models()`

```python
class Hare:
    @classmethod
    def register_live_models(
        cls,
        models: Iterable[type[Model]],
        app_label: str,
        connection_alias: str = "default",
        *,
        managed: bool | None = None,
    ) -> None: ...
```

Registers `Model` subclasses **after** `Hare.init()` has already run, for a class you only have at
runtime (built from live database introspection, say) rather than declared up front in the config's
`apps`. It takes a collection of model classes — one model is a list of one. It requires an active
`HareContext` (`ConfigurationError` otherwise) and a `connection_alias` that's configured
(`ConfigurationError` naming it if not); it also raises `ConfigurationError` if the model's table name
collides with an already-registered one. `app_label` can be any non-empty string without a dot — it
doesn't need to be one of the configured apps; an empty one or one with a dot (a model's label is
`<app>.<Model>`) raises `ConfigurationError`.

Calling `context.register_live_models()` explicitly binds models using that context's connections,
even while another active context is current. The caller’s active context is restored after
both successful and failed registration.

Registration is atomic: if it fails (an unresolvable relation target, a backward-relation name that
collides with a field of the target, …) the model is not left in the registry, no target keeps a backward
relation it added, and the published graph retains its `managed`/connection settings — fix the class and
register it
again. Registering a *different* class under a name already registered in `app_label` raises
`ConfigurationError` — call `unregister_live_models()` on the old one first; registering the same class
again does nothing.

Several models registered in one call are one step — what models whose relations point at each other (A →
B and B → A) need, as they can't be registered one at a time (the first one's target doesn't exist yet).
The whole batch is atomic: if any model fails, none of them stays registered.

A live model almost always has `managed=False` — otherwise `makemigrations`/`hare drift` also manage it,
fighting whatever owns its table. `managed` is applied to the new binding
*before* relations are initialized; `None` (the default) keeps the model's own `Meta.managed`.

Registration prepares a complete independent graph and publishes it only after validation.
A failed candidate leaves the previous registry and graph untouched. Class declarations and
relation descriptors are not rewritten. Queries and instances already created keep their previous
graph after successful registration. Preparation is synchronous, including user binding hooks;
side effects those hooks cause outside the registry are outside this guarantee. Automatic
through-table names are checked within each connection and schema using dialect identifier rules.

## <a id="unregistering"></a>Unregistering: `unregister_live_models()`

```python
class Hare:
    @classmethod
    def unregister_live_models(cls, models: Iterable[ModelReference]) -> None: ...
```

A `ModelReference` is a model class, an `"app.Model"` label or a `swappable()` reference
(`Hare.unregister_live_models(["content.Notice"])`).
Passing a class requires that exact registered class. A stale or never-registered class with
the same label raises `ConfigurationError` and leaves the replacement registered; a string label
continues to select the class currently registered under that label.
Publishes a new graph without these models or their generated reverse relations. The previous
graph and its caches remain available to queries and instances that already use it; those objects
keep working until their originating context is closed, subject to any external schema changes.
New registry lookups cannot find removed models. A class manager produces an unbound template
when its class is absent; execution in that registry raises `ConfigurationError`. The same class,
or another class under the freed label, can be registered afterwards.

Unregistering a model that another registered model still points at (FK/O2O/M2M) raises
`ConfigurationError` naming the referencing fields and changes nothing — unregister the referencing
models first, or together: relations between the models removed in one call don't block, and neither does
a self-referential relation.

## <a id="reading-a-table"></a>Reading a table: `DatabaseCatalog`

```python
class DatabaseCatalog:
    @staticmethod
    async def inspect_table(
        connection: DatabaseClient, table: str, schema: str | None = None
    ) -> TableInfo: ...

    @staticmethod
    async def inspect_tables(
        connection: DatabaseClient, tables: list[str], schema: str | None = None
    ) -> list[TableInfo]: ...
```

Reads a table's columns, keys, indexes and constraints as a `TableInfo`; `inspect_tables()` returns one
per name, in the same order — on PostgreSQL with a fixed number of catalog queries, however many tables
there are. `schema` is the connection's default one when `None`, and ignored by a database without
schemas. A table that doesn't exist raises `TableNotFoundError`, and a dialect without an introspector
`UnsupportedDialectError`. The catalog reader checks requested tables directly; no preliminary
table-list query is needed. A missing schema raises `SchemaNotFoundError`.

## <a id="building-a-model-from-an-introspected-table"></a>Building a model from an introspected table

```python
class ModelFactory:
    @staticmethod
    def from_table_info(
        table_info: TableInfo,
        dialect: str,
        *,
        app_label: str,
        foreign_key_target_overrides: dict[str, str] | None = None,
        skip_many_to_many_through_tables: bool = True,
        target_tables: tuple[TableInfo, ...] = (),
        target_models: dict[str, type[Model]] | None = None,
        apps: Apps | None = None,
    ) -> type[Model]: ...
```

Builds the model class for one table directly with `type()` — the same class you'd get by executing the
source [`hare inspectdb`](../migrations/inspectdb.md) generates for it: both go through one model state
(`hare.inspectdb.InspectedModelBuilder` turns the table into the same `ModelState` a declared model has),
so fields, composite primary keys, indexes and constraints can't drift apart. The class isn't registered
— pass it to `register_live_models()`.

- `app_label` — the label FK/composite-FK targets are qualified with (`"<app_label>.<TargetClass>"`);
  normally the label you register the model under. Target models must be registered earlier or in the
  same `register_live_models()` call.
- `foreign_key_target_overrides` — `{target_table: "app.Model"}`, replaces the whole reference for that
  target table, for pointing an FK at an already-registered model whose class name doesn't follow
  inspectdb's naming.
- `skip_many_to_many_through_tables` — a table made only of foreign-key columns looks like a
  `ManyToManyField` through table; by default it's refused with `ManyToManyThroughTableSkippedError`
  (the single-table source builder emits a `# NOTE` comment instead). `False` builds a plain model for it.
  This check uses only the table's shape; module generation also checks the related tables before
  reconstructing many-to-many relations.
- `target_tables` supplies related tables' catalog metadata. The factory uses it to match referenced
  columns to model attributes and avoid collisions in reverse relation names.
- `target_models` supplies existing target classes keyed by full model references such as
  `"catalog.Author"`. Pass `apps` to use their bindings in a particular registry. By default, the
  factory looks up those bindings in the active context.

A table without a primary key becomes a model with `Meta.primary_key = None` (see
[Models without a primary key](meta-options.md#primary_key)).
