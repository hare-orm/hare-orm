# Models registered at runtime

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

Registration is atomic: if it fails (an unresolvable relation target, a backward-relation name that
collides with a field of the target, …) the model is not left in the registry, no target keeps a backward
relation it added, and its `managed`/connection settings are restored — fix the class and register it
again. Registering a *different* class under a name already registered in `app_label` raises
`ConfigurationError` — call `unregister_live_models()` on the old one first; registering the same class
again does nothing.

Several models registered in one call are one step — what models whose relations point at each other (A →
B and B → A) need, as they can't be registered one at a time (the first one's target doesn't exist yet).
The whole batch is atomic: if any model fails, none of them stays registered.

A live model almost always has `managed=False` — otherwise `makemigrations`/`hare drift` also manage it,
fighting whatever owns its table. `managed` is written to [`Meta.managed`](meta-options.md#managed)
*before* relations are initialized; `None` (the default) keeps the model's own `Meta.managed`.

## <a id="unregistering"></a>Unregistering: `unregister_live_models()`

```python
class Hare:
    @classmethod
    def unregister_live_models(cls, models: Iterable[type[Model] | str]) -> None: ...
```

A model is given by its class or its `"app.Model"` label (`Hare.unregister_live_models(["content.Notice"])`).
Reverses a registration: the model leaves the registry, the backward relations (reverse FK/O2O accessors,
generated M2M fields) it added to the models its own relation fields point at are removed, and every
cache built for it or for those targets is dropped. Its FK shadow columns are reset too, so a new class
with the same name — or the same class — can be registered again afterwards. The unregistered class can't
run queries (`ConfigurationError`).

Unregistering a model that another registered model still points at (FK/O2O/M2M) raises
`ConfigurationError` naming the referencing fields and changes nothing — unregister the referencing
models first, or together: relations between the models removed in one call don't block, and neither does
a self-referential relation.

## <a id="reading-a-table"></a>Reading a table: `DatabaseCatalog`

```python
class DatabaseCatalog:
    @staticmethod
    async def inspect_table(
        connection: DatabaseClient, table: str, schema: str | None = None, *, verify_exists: bool = True
    ) -> TableInfo: ...

    @staticmethod
    async def inspect_tables(
        connection: DatabaseClient, tables: list[str], schema: str | None = None, *, verify_exists: bool = True
    ) -> list[TableInfo]: ...
```

Reads a table's columns, keys, indexes and constraints as a `TableInfo`; `inspect_tables()` returns one
per name, in the same order — on PostgreSQL with a fixed number of catalog queries, however many tables
there are. `schema` is the connection's default one when `None`, and ignored by a database without
schemas. A table that doesn't exist raises `TableNotFoundError`, and a dialect without an introspector
`UnsupportedDialectError`. `verify_exists=False` skips the query listing the tables, for a caller that
has just matched the names against such a list itself.

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
    ) -> type[Model]: ...
```

Builds the model class for one table directly with `type()` — the same class you'd get by executing the
source [`hare inspectdb`](../migrations/inspectdb.md) generates for it: both go through one model state
(`hare.inspectdb.InspectedModelBuilder` turns the table into the same `ModelState` a declared model has),
so fields, composite primary keys, indexes and constraints can't drift apart. The class isn't registered
— pass it to `register_live_models()`.

- `app_label` — the label FK/composite-FK targets are qualified with (`"<app_label>.<TargetClass>"`);
  normally the label you register the model under. Target tables must be registered first.
- `foreign_key_target_overrides` — `{target_table: "app.Model"}`, replaces the whole reference for that
  target table, for pointing an FK at an already-registered model whose class name doesn't follow
  inspectdb's naming.
- `skip_many_to_many_through_tables` — a table made only of foreign-key columns looks like a
  `ManyToManyField` through table; by default it's refused with `ManyToManyThroughTableSkippedError`
  (where the generator emits a `# NOTE` comment instead). `False` builds a plain model for it.

A table without a primary key becomes a model with `Meta.primary_key = None` (see
[Models without a primary key](meta-options.md#primary_key)).
