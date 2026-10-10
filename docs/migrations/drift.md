# Detecting drift

`hare drift [app_labels...] [--connection ALIAS] [--schema SCHEMA]` compares the live schema of a
database with the current models: it prints every difference — the operation that would remove it,
an untracked table or column, a column whose type the models can't express — and exits with code 1
when there is one, 0 when there is none. Without `--connection` it checks the first configured
connection; without app labels, every app on it.

```bash
hare drift --connection default
```

## <a id="from-code"></a>Calling it from code

The command is a thin wrapper around a public library function — call it directly (e.g. from a
health-check endpoint or a CI gate) instead of shelling out:

```python
from hare.migrations.drift import detect_drift_for_alias

async def detect_drift_for_alias(
    apps: Apps,
    apps_config: dict[str, dict[str, Any]],
    connection_alias: str,
    *,
    schema: str | None = None,
    app_labels: list[str] | None = None,
) -> DriftResult

result = await detect_drift_for_alias(Hare.apps, apps_config, "default")
if result.has_drift:
    ...
```

It checks every app on connection `connection_alias`: an app from `apps_config` whose `default_connection` is
`connection_alias`, plus an app registered only at runtime (e.g. via `Hare.register_live_models()`) whose models
are bound to `connection_alias`. `app_labels` narrows the check to those apps. A `Meta.managed = False` model
(including a live-registered one) is never diffed, but its table counts as known, so it is not
reported as untracked. A model without `Meta.schema` is always looked up in the connection's
default schema (PostgreSQL `current_schema()`, i.e. the first existing schema on the `search_path`) —
where `migrate` creates its table; a model with `Meta.schema` is looked up there. `schema` only
picks the schema swept for untracked tables (the default schema when `None`) and is ignored on
a database without schemas (SQLite, ClickHouse). Schemas/extensions that only models of apps outside the checked ones need are never
reported.
Raises `ConfigurationError` for an unknown alias, an unknown entry of `app_labels`, or an alias no
app uses.

## <a id="what-is-compared"></a>What is compared

Besides columns, indexes, constraints and triggers, it compares the connection's dialect entry of
[`Meta.table_options`](../models/meta-options.md#table_options) with the table's storage. For a
[partitioned table](../models/meta-options.md#partitioning) it reads the strategy, the key and every
partition with its bound off the catalog: a partition added or dropped by hand is reported as a
`RemovePartition`/`AddPartition`, another strategy or key as an `AlterModelOptions`. The storage
parameters are those of the partitions — one they don't share is reported, naming each partition's
value. The partitions themselves are never listed as tables of their own, by `drift` or by
`inspectdb`, which writes the table's `partitioning=` into `Meta.table_options`.

Inspect `DriftResult.operations`/`.untracked_tables`/`.untracked_columns`/`.mismatched_columns`, or
just its `.has_drift` property.

Every column is compared with its field: its type, nullability and a declared `db_default` for a
plain field; nullability, index, `FOREIGN KEY` constraint (present, pointing at the target's table,
with the field's `ON DELETE` action) and `db_default` for a foreign key. A difference comes back as
the `AlterField` that would restore the field — for another type, from the field `inspectdb` would
reconstruct the column as (a `CharField`/`DecimalField` that differs only in size keeps its class).
Types are compared by their normalized PostgreSQL names (`varchar`/`character varying`,
`int4`/`integer`, `timestamptz`/`timestamp with time zone`, ...), on SQLite by type affinity — a
`VARCHAR(50)` and a `VARCHAR(10)` column are the same there. A default is compared by its text with
case, whitespace, parentheses and type casts left out (`'a'::text` equals `'a'`, `CURRENT_TIMESTAMP`
equals `Now()`); a default of a type with no normalized form (a date, a JSON document) is not
compared. A type or default neither side can normalize counts as unchanged, so a schema `migrate`
or `generate_schemas()` just created never shows drift. A foreign key's key column of another type
than the target's key has no `AlterField` to describe it; it is reported as a `ColumnMismatch`
(`app_label`, `model_name`, `table`, `column`, `detail` — e.g. "type is bigint, the model expects
integer") in `.mismatched_columns`, as is a column of a type `inspectdb` has no field for.

A `ManyToManyField` whose hare-managed through table is missing, lacks one of its key columns, or
lacks its `UNIQUE` key is reported as an `AddField` for that field; one whose through table lacks a
key index its `db_index` declares, and a `ForeignKeyField` whose key column(s) lack their own
index, as an `AlterField`. A partial index's condition is
compared by meaning, not by the database's own wording of the predicate; a condition the
introspector can't parse back is taken to be the declared one of the same-named `PartialIndex`
over the same fields.

A unique index in the database is matched to the declared `UniqueConstraint` (with or without
`condition`) or `Index(unique=True)` it implements — by name first, then by columns — so neither a
single-column one nor one SQLite backs with its own `sqlite_autoindex_*` shows up as a
`unique=True` field or a second constraint; a `UniqueConstraint`'s `deferrable`/
`initially_deferred` are compared too (PostgreSQL). An unnamed index — an expression index included —
is matched under the name hare generates for it. For a trigger whose timing, `for_each`, events,
language and deferral match the declared one of the same name, the declared `when` text is used
when both have one (PostgreSQL rewrites it) and the declared `body` when the two are equal once
stripped of surrounding whitespace; any other difference is reported.

The lower-level function it wraps takes an already-built state:

```python
async def detect_drift(
    connection: DatabaseClient,
    new_state: State,
    target_labels: list[str],
    schema: str | None = None,
    unmanaged_model_states: list[ModelState] | None = None,
) -> DriftResult
```
