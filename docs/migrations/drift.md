# Detecting drift

`hare drift [app_labels...] [--connection ALIAS] [--schema SCHEMA]` compares the live schema of a
database with the current models: it prints every difference — the operation that would remove it, an
untracked table or column, a column whose type the models can't express — and exits with code 1 when
there is one, 0 when there is none. Without `--connection` it checks the first configured connection;
without app labels, every app on it.

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
    journal_tables: Collection[str] = (),
) -> DriftResult: ...

result = await detect_drift_for_alias(Hare.apps, apps_config, "default")
if result.has_drift:
    print(result.report())
```

It checks every app on connection `connection_alias`: an app from `apps_config` whose
`default_connection` is `connection_alias`, plus an app registered only at runtime (e.g. via
`Hare.register_live_models()`) whose models are bound to `connection_alias`. `app_labels` narrows the
check to those apps. A `Meta.managed = False` model (including a live-registered one) is never diffed,
but its table counts as known, so it is not reported as untracked. A model without `Meta.schema` is
always looked up in the connection's default schema (PostgreSQL `current_schema()`, i.e. the first
existing schema on the `search_path`) — where `migrate` creates its table; a model with `Meta.schema` is
looked up there. `schema` only picks the schema swept for untracked tables (the default schema when
`None`) and is ignored on a database without schemas (SQLite, ClickHouse). Schemas/extensions that only
models of apps outside the checked ones need are never reported. `journal_tables` names the tables of
migration journals besides `hare_migrations` — a `MigrationRecorder(table_name=...)` of migrations the
application runs itself ([Migrations without files](migrations-without-files.md)): they count as known,
not untracked (`hare drift --journal-table NAME` on the command line). Raises `ConfigurationError` for an
unknown alias, an unknown entry of `app_labels`, or an alias no app uses.

Inspect `DriftResult.operations`/`.untracked_tables`/`.untracked_columns`/`.mismatched_columns`, or just
its `.has_drift` property.

## <a id="what-is-compared"></a>What is compared

Table by table, it compares everything the models declare with what the database holds.

### <a id="tables"></a>Table options and partitions

Besides columns, indexes, constraints and triggers, it compares the connection's dialect entry of
[`Meta.table_options`](../models/meta-options.md#table_options) with the table's storage. For a
[partitioned table](../dialects/postgresql/indexes-and-tables.md#partitioning) it reads the strategy, the
key and every partition with its bound off the catalog: a partition added or dropped by hand is reported
as a `RemovePartition`/`AddPartition`, another strategy or key as an `AlterModelOptions`. The storage
parameters are those of the partitions — one they don't share is reported, naming each partition's value.
The partitions themselves are never listed as tables of their own, by `drift` or by `inspectdb`, which
writes the table's `partitioning=` into `Meta.table_options`.

### <a id="schema-objects"></a>Views, functions and access

Views, materialized views, functions, sequences, row level security, policies and grants a model declares
are read back too — see [Views, functions, sequences and
access](../models/schema-objects.md#in-migrations).

### <a id="comments"></a>Comments

On PostgreSQL and ClickHouse the comments are compared too: a column's with its field's `description` —
another one comes back as an `AlterField` — and the table's with `Meta.table_description`, as an
`AlterModelOptions`. SQLite keeps no column comments.

### <a id="columns"></a>Columns

Every column is compared with its field: its type, nullability and a declared `db_default` for a plain
field; nullability, index, `FOREIGN KEY` constraint (present, pointing at the target's table, with the
field's `ON DELETE` action) and `db_default` for a foreign key. A difference comes back as the
`AlterField` that would restore the field — for another type, from the field `inspectdb` would
reconstruct the column as (a `CharField`/`DecimalField` that differs only in size keeps its class). Types
are compared by their normalized PostgreSQL names (`varchar`/`character varying`, `int4`/`integer`,
`timestamptz`/`timestamp with time zone`, ...), on SQLite by type affinity — a `VARCHAR(50)` and a
`VARCHAR(10)` column are the same there. A column of a type of its own (a `NativeEnumField`'s `ENUM`, a
domain, an extension's type) changed to a built-in one by hand — an enum column turned into `text` — is
reported too, by the `AlterField` to the field the column reads as; an `ENUM` type no column is of any
more still exists and isn't created again. A default is compared by its text with case, whitespace,
parentheses and type casts left out (`'a'::text` equals `'a'`, SQLite's `CURRENT_TIMESTAMP` equals
`Now()`; on PostgreSQL `Now()` is `statement_timestamp()`, and `now()` — the start of the transaction —
differs from it); a default of a type with no normalized form (a date, a JSON document) is not compared.
A type or default neither side can normalize counts as unchanged, so a schema `migrate` or
`generate_schemas()` just created never shows drift. A foreign key's key column of another type than the
target's key has no `AlterField` to describe it; it is reported as a `ColumnMismatch` (`app_label`,
`model_name`, `table`, `column`, `detail` — e.g. "type is bigint, the model expects integer") in
`.mismatched_columns`, as is a column of a type `inspectdb` has no field for, and a foreign key with an
`ON UPDATE` action or a deferral its relation doesn't declare — any but the
`DEFERRABLE INITIALLY IMMEDIATE` of `on_delete=PROTECT` ("its foreign key is ON UPDATE CASCADE, the model
declares none"). So is a table whose `PRIMARY KEY` holds other columns than the model's primary key — a
dropped constraint ("is not in the table's PRIMARY KEY, the model's primary key holds it"), or on
ClickHouse a `PRIMARY KEY (id, v)` where the model's key is `id`.

### <a id="relations-indexes-triggers"></a>Relations, indexes and triggers

A `ManyToManyField` whose hare-managed through table is missing, lacks one of its key columns, or lacks
its `UNIQUE` key is reported as an `AddField` for that field; one whose through table lacks a key index
its `db_index` declares, or a `FOREIGN KEY` its `db_constraint=True` declares (or has one where
`db_constraint=False`), and a `ForeignKeyField` whose key column(s) lack their own index, as an
`AlterField`. A partial index's condition is compared by meaning, not by the database's own wording of
the predicate; a condition the introspector can't parse back is taken to be the declared one of the
same-named `PartialIndex` over the same fields.

A unique index in the database is matched to the declared `UniqueConstraint` (with or without
`condition`) or `Index(unique=True)` it implements — by name first, then by columns — so neither a
single-column one nor one SQLite backs with its own `sqlite_autoindex_*` shows up as a `unique=True`
field or a second constraint; a `UniqueConstraint`'s `deferrable`/ `initially_deferred` are compared too
(PostgreSQL). An unnamed index — an expression index included — is matched under the name hare generates
for it. For a trigger whose timing, `for_each`, events, language and deferral match the declared one of
the same name, the declared `when` text is used when both have one of the same literals (PostgreSQL
rewrites it) and the declared `body` when the two are equal once stripped of surrounding whitespace; any
other difference is reported — a trigger disabled by hand (`enabled`) too. A declared trigger the
database has in a form not read back (a constraint trigger `FROM` another table) comes back as an
`AlterTrigger`, which creates it again.

## <a id="detect-drift"></a>From an already-built state: `detect_drift()`

The lower-level function it wraps takes an already-built state:

```python
async def detect_drift(
    connection: DatabaseClient,
    new_state: State,
    target_labels: list[str],
    schema: str | None = None,
    unmanaged_model_states: list[ModelState] | None = None,
    journal_tables: Collection[str] = (),
) -> DriftResult: ...
```
