# Models registered at runtime

```python
Hare.register_live_models(
    models: Iterable[type[Model]],
    app_label: str,
    connection_alias: str = "default",
    managed: bool | None = None,
) -> None
Hare.unregister_live_models(models: Iterable[type[Model]]) -> None
```

Both take a collection of model classes - one model is a list of one. Registers `Model` subclasses
**after** `Hare.init()` has already run, for a class you only have at
runtime (e.g. built from live database introspection) rather than declared up front in the config's `apps`.
Requires an active `HareContext` (`ConfigurationError` otherwise) and a `connection_alias` that's
actually configured (`ConfigurationError` naming it if not); also raises `ConfigurationError` if the
model's table name collides with an already-registered one. `app_label` can be any string — it
doesn't need to be one of your configured apps.

Registration is atomic: if it fails (an unresolvable relation target, a backward-relation name
that collides with a field of the target, …) the model is not left in the registry, no target
keeps a backward relation it added, and its `managed`/connection settings are restored — fix the
class and register it again. Registering a *different* class under a name already registered in
`app_label` raises `ConfigurationError` — call `unregister_live_models()` on the old one first;
registering the same class again is a no-op.

`register_live_models()` registers several models in one step — required for models whose
relations point at each other (A → B and B → A), which can't be registered one at a time (the
first one's target doesn't exist yet). It is atomic for the whole batch: if any model fails, none
of them stays registered. `unregister_live_models()` removes such a group together — relations
between the removed models don't block it.

Almost always used with `managed=False` — otherwise this model becomes something
`makemigrations`/`hare drift` also tries to manage, fighting whatever actually owns its table.
`managed` is written to [`Meta.managed`](meta-options.md) *before* relations are initialised;
`None` (the default) keeps whatever the model's own `Meta.managed` says.

`unregister_live_models()` reverses a registration: the model leaves the registry, the backward
relations (reverse FK/O2O accessors, generated M2M fields) it added to the models its own relation
fields point at are removed, and every cache built for it or for those targets is dropped. Its
FK shadow columns are reset too, so a new class with the same name — or the same class — can be
registered again afterwards. The unregistered class can't run queries (`ConfigurationError`).
Unregistering a model that another registered model still points at (FK/O2O/M2M) raises
`ConfigurationError` naming the referencing fields and changes nothing — unregister the
referencing models first. A self-referential relation doesn't block.

```python
from hare.inspectdb import ModelFactory, SchemaIntrospector

table_info = await SchemaIntrospector.inspect_table(connection, "orders")
Order = ModelFactory.from_table_info(table_info, connection.dialect.name, app_label="live")
Hare.register_live_models([Order], app_label="live", connection_alias="default", managed=False)
...
Hare.unregister_live_models([Order])
```

To read several tables at once, `SchemaIntrospector.inspect_tables(connection, ["orders", "customers"])`
returns one `TableInfo` per name, in the same order - on Postgres with a fixed number of catalog
queries, however many tables there are.

## Building a model from an introspected table {: #building-a-model-from-an-introspected-table }

```python
ModelFactory.from_table_info(
    table_info: TableInfo,
    dialect: str,
    *,
    app_label: str,
    fk_target_overrides: dict[str, str] | None = None,
    skip_m2m_through_tables: bool = True,
) -> type[Model]
```

Builds the model class for one table directly with `type()` — the same class you'd get by
executing the source [`hare inspectdb`](../migrations/inspectdb.md) generates for it: both go through
one model state (`hare.inspectdb.InspectedModelBuilder` turns the table into the same `ModelState`
a declared model has), so fields, composite primary keys, indexes and constraints can't drift apart. The class isn't registered — pass it to
`register_live_models()`.

- `app_label` — the label FK/composite-FK targets are qualified with (`"<app_label>.<TargetClass>"`);
  normally the label you register the model under. Target tables must be registered first.
- `fk_target_overrides` — `{target_table: "app.Model"}`, replaces the whole reference for that
  target table, for pointing an FK at an already-registered model whose class name doesn't follow
  inspectdb's naming.
- `skip_m2m_through_tables` — a table made only of foreign-key columns looks like a
  `ManyToManyField` through table; by default it's refused with `ManyToManyThroughTableSkippedError`
  (where the generator emits a `# NOTE` comment instead). `False` builds a plain model for it.

A table with neither a primary key nor a unique index covering every column becomes a model with
`Meta.primary_key = None` (see [Meta options](meta-options.md#primary_key)).
