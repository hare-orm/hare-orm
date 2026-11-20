# Detecting drift

`hare drift [app_labels...] [--connection ALIAS] [--schema SCHEMA]` compares the live schema of a
database with the current models: it prints every difference — the operation that would remove it, an
untracked table or column, a column no operation can restore — and exits with code 1 when there is one,
0 when there is none. Without `--connection` it checks the first configured connection;
without app labels, every app on it.

```bash
hare drift --connection default
```

For differences that Hare can express with migration operations, the command prints suggested Python
code by application. Review it before copying it into a migration and apply it only where the reported
physical state matches. When the database differs from the history, the suggestion can start with
`SeparateDatabaseAndState` to describe what is present on disk. The following regular operations
change that physical state to match the models. Reversing such a repair restores the observed state.
The command reports uncertain repairs separately, for example when rebuilding a table could drop a
column outside the model. It does not change the inspected tables or create migration files.

## <a id="from-code"></a>Calling it from code

The command is a thin wrapper around a public library function — call it directly (e.g. from a
health-check endpoint or a CI gate) instead of shelling out.

`detect_drift_for_alias()` uses the supplied registry's application settings. The lower-level
`detect_drift()` uses the `State`'s captured `MigrationEnvironment`, including its timezone when
rendering defaults. Another current `HareContext` does not change the expected schema; the caller's
context is restored after comparison, including on failure.

Reference signature:

```python
from collections.abc import Collection
from typing import Any

from hare.core.apps import Apps
from hare.migrations.drift import DriftResult

async def detect_drift_for_alias(
    apps: Apps,
    apps_config: dict[str, dict[str, Any]],
    connection_alias: str,
    *,
    schema: str | None = None,
    app_labels: list[str] | None = None,
    journal_tables: Collection[str] = (),
) -> DriftResult: ...
```

Example in an already initialized application. `apps_config` is the `apps` configuration mapping
used to initialize that application's model registry:

```python
from hare import Hare
from hare.migrations.drift import detect_drift_for_alias

result = await detect_drift_for_alias(Hare.apps, apps_config, "default")
if result.has_drift:
    for operation in result.operations:
        print(operation.describe())
    print(result.untracked_tables, result.untracked_columns, result.mismatched_columns, result.mismatched_objects)
```

It checks every app on connection `connection_alias`: an app from `apps_config` whose
`default_connection` is `connection_alias`, plus an app registered only at runtime (e.g. via
`Hare.register_live_models()`) whose models are bound to `connection_alias`. `app_labels` narrows the
check to those apps. Models bound to another connection do not make their table names known here,
even if the other database uses the same physical table name. A missing secondary table of a model
is reported as an object mismatch when its storage table still exists; drift does not propose
recreating that storage table and losing its rows.
A `Meta.managed = False` model (including a live-registered one) is never diffed,
but its table counts as known, so it is not reported as untracked; the shadow holds its table alone, for
the relations pointing at it — not its triggers, views or other objects (an `INSTEAD OF` trigger of a
model over a view would fail on a table). A non-primary-key identity column is kept as an ordinary
column in that shadow table, so a `to_field` relation can still target it. A model without `Meta.schema` is
always looked up in the connection's default schema (PostgreSQL `current_schema()`, i.e. the first
existing schema on the `search_path`) — where `migrate` creates its table; a model with `Meta.schema` is
looked up there — in its attached database on SQLite, its database of the server on ClickHouse.
`schema` only picks the schema swept for untracked tables (the default schema when `None`) — an attached
database on SQLite, a database of the server on ClickHouse. Schemas/extensions that only
models of apps outside the checked ones need are never reported. `journal_tables` names the tables of
migration journals besides `hare_migrations` — a `MigrationRecorder(table_name=...)` of migrations the
application runs itself ([Migrations without files](migrations-without-files.md)): they count as known,
not untracked (`hare drift --journal-table NAME` on the command line). Raises `ConfigurationError` for an
unknown alias, an unknown entry of `app_labels`, or an alias no app uses.

Inspect `DriftResult.operations_by_app` for the proposed migration operations by application, or
`DriftResult.operations` for a flat list. See `.untracked_tables`, `.untracked_columns`,
`.mismatched_columns`, `.mismatched_objects` and `.has_drift` for the comparison results.

## <a id="what-is-compared"></a>What is compared

The schema the models declare is built beside the database — in a *shadow*, by the same SQL
`generate_schemas()` writes — and then the database and the shadow are read by the same introspector and
compared part by part. Reading both sides through the same introspector avoids differences caused only
by the database's normalized SQL. The comparison reports supported catalog differences for the selected
models and declared schema objects: column types, nullability and defaults, index expressions and
conditions, constraints, triggers and view queries. It does not inventory every undeclared schema object.

### <a id="shadow"></a>The shadow

The shadow is removed at the end, whatever happened — after an error too:

| Database | Shadow | Rights it needs |
|---|---|---|
| PostgreSQL | a schema `hare_shadow_<hex>` — and `hare_shadow_<hex>_<schema>` for each other schema of the models — on a connection of its own, searched first, in a transaction rolled back at the end: nothing of it is ever committed | `CREATE` on the database (`CREATE SCHEMA`) |
| SQLite | a database in memory, opened with the connection's settings — and one in memory under the name of each database the connection attaches | none |
| ClickHouse | a database `hare_shadow_<hex>` — and `hare_shadow_<hex>_<database>` for each other database of the models — on every server of the `cluster`, dropped by `DROP DATABASE ... SYNC`; see below | `CREATE DATABASE` and `DROP DATABASE` |

On ClickHouse a shadow database is of the `Replicated` engine under the Keeper path `/hare_shadow_<hex>/...`
when the database it stands for is one, and a `Replicated*` table with a Keeper path of its own keeps it
under that prefix too — no table of the shadow becomes a replica of a real one.

Comparison maps the database setting of a local ClickHouse dictionary source back to the real
database. Remote sources and SQL string values keep their declared contents.

A database whose dialect has no shadow raises `UnSupportedError`.

### <a id="parts"></a>Parts and operations

Each part is matched by its table and its name — the names hare gives the objects are its own
([explicit names](../extending/writing-a-dialect/schema.md#names)), so an object renamed by hand is the
removal of the database's and the addition of the declared one, never a guessed rename. A part only the
shadow has, only the database has, or that reads otherwise on the two comes back as the operation that
would make the database the models' — built from what the models declare:

| Part | Missing from the database | Only in the database | Read otherwise |
|---|---|---|---|
| table | `CreateModel` | untracked table | — |
| column | `AddField` | untracked column | `AlterField` |
| foreign key | `AlterField` of its field | `AlterField` of its field | `AlterField` of its field |
| field's own index (`db_index`, `unique`) | `AlterField` | — | `AlterField` |
| `Meta.indexes` entry | `AddIndex` | — | `RemoveIndex` + `AddIndex` |
| `Meta.constraints` entry | `AddConstraint` | — | `RemoveConstraint` + `AddConstraint` |
| index or constraint no model declares | — | `RemoveIndex`, `RemoveConstraint` for a unique index or a constraint | — |
| trigger, policy, sequence | `AddTrigger`/`AddPolicy`/`AddSequence` | `RemoveTrigger`/... | `AlterTrigger`/... |
| view, materialized view, function, dictionary a model declares | `AddView`/... | — | `AlterView`/... |
| grant | `AddGrant` | `RemoveGrant` | — |
| partition | `AddPartition` | `RemovePartition` | `RemovePartition` + `AddPartition` |
| other table options, the table's comment | — | — | `AlterModelOptions` |
| row level security | — | — | `AlterRowLevelSecurity` |
| `ENUM` type | `CreateEnumType` | — | `AlterEnumType` |
| schema, extension | `CreateSchema`, `CreateExtension` | — | — |

A column's comment is part of the column — another one is an `AlterField`; SQLite keeps the table's
and the columns' comments inside `CREATE TABLE`, where drift and `inspectdb` read them. A table's storage
is the connection's dialect entry of
[`Meta.table_options`](../models/meta-options.md#table_options); a [partitioned
table](../dialects/postgresql/indexes-and-tables.md#partitioning) whose partitions alone differ gets its
partitions added and dropped. The partitions are never listed as tables of their own by `drift` or
`inspectdb`. A partitioning strategy or key that Hare cannot reproduce is reported in
`.mismatched_objects`.

A missing automatic through table of a `ManyToManyField` gets an `AddField`. If the only difference is
that all the plain key indexes generated by `db_index=True` are missing, `AlterField` adds them.
Other differences in an existing through table are reported in `.mismatched_objects`.

Two differences no operation restores are a `ColumnMismatch` (`app_label`, `model_name`, `table`,
`column`, `detail`) in `.mismatched_columns`: a table whose `PRIMARY KEY` holds other columns than the
model's primary key ("is not in the table's PRIMARY KEY, the model's primary key holds it"; on ClickHouse a
`PRIMARY KEY (id, v)` where the model's key is `id`), and a foreign key declared with `swappable()` that
references the table of the model its setting pointed at before — its rows name rows of that table.

`.mismatched_objects` retains other differences without a faithful migration operation as
`ObjectMismatch(part_type, table, name, detail)`. For example, an unrecognized catalog privilege or
a grant on an ambiguous function overload remains drift even though a name-based `Grant` cannot
express its correction. These records contribute to `.has_drift` and appear in CLI output. Review
the reported object and reconcile it explicitly; an empty `.operations` list alone does not mean
that the schemas agree.

For example, drift reports an invalid PostgreSQL index, or a changed ClickHouse view or dictionary
whose former declaration cannot be reconstructed from the catalog. A missing declared view or
dictionary can still be added with the corresponding migration operation.

The column catalog also records the settings of associated sequences: type, start, increment,
bounds, cache and cycle. An internal identity sequence is matched through its column, so a different
server-generated sequence name in the shadow does not cause drift. The same applies to a SERIAL
primary key's owned sequence and its exact `nextval` default after a table or column rename.
Explicit defaults and independent sequence references retain their identities. Changing a generated
integer key's type also changes its owned sequence's integer type, preserving custom sequence settings.
Its current counter is data and
is not compared. Settings that the field declaration cannot express are reported in
`.mismatched_objects`, rather than as an `AlterField` that would leave them unchanged. A simultaneous
representable change, such as a column comment, can still produce its own operation.

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
