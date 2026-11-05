# ClickHouse: schema objects

Views, materialized views, projections and dictionaries are declared on the model, created and changed by
migrations and compared by `hare drift`, as on PostgreSQL — see [Views, functions, sequences and
access](../../models/schema-objects.md). A connection to a cluster runs every statement of the schema on
all its servers.


Schema placement is compiled before SQL is rendered. A column change addresses both the storage table
and its Distributed companion; indexes, projections and storage settings address storage only.
`ON CLUSTER` is quoted and included in the SQL preview. Placement uses the engine of the target
`Meta.schema` database, or the connection database when no schema is given. A `Replicated` or `Shared`
database
propagates schema changes itself; mutations of distributed rows still address storage on all shards.

A connected client captures database engines and refreshes them at schema-operation boundaries.
Offline schema compilation with a cluster needs an explicit catalog snapshot:
`ClickhouseSchemaEditor(connection, collect_sql=True, database_engines={"analytics": "Atomic"})`.
Include each target database. An unknown engine is reported before rendering, rather than assumed.
Raw scripts execute literally; the client does not add cluster clauses or redirect table names.
Reloading a dictionary uses its qualified database name and runs on the configured cluster, including
when the database itself replicates DDL.


Local ORM mutations carry a failure target alongside SQL. If the server reports failed mutation IDs,
Hare attempts to cancel those IDs on the compiled database and table; it does not retry the write.
This is best-effort cleanup, not a rollback of rows already changed. An error in recovery does not
replace the original statement error. Distributed mutations and cluster schema changes require
inspection of `system.mutations` on the affected servers: mutation IDs are not unique across shards,
so Hare never broadcasts a cancellation based on one server's error. Raw SQL has no implicit recovery
target; pending mutations from raw commands also remain under the caller's control.

## <a id="views"></a>Views

`Meta.views` with `View(name, query)` is a ClickHouse view — a table of the `View` engine, renamed as one
and not dropped with the table it reads. `hare drift` compares a view's query as the server parses it, so
a query written in other words but the same is no drift.

## <a id="materialized-views"></a>Materialized views

A ClickHouse materialized view follows the rows inserted into the table its query reads: each inserted
block goes through the query and into the view. `ClickhouseMaterializedView` says where its rows go:

```python
from hare import Model, fields
from hare.ddl import RawSQLTerm
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView


class Visit(Model):
    site = fields.CharField(max_length=50)
    country_code = fields.CharField(max_length=2)

    class Meta:
        materialized_views = [
            # A storage of its own, summed by site as its parts merge.
            ClickhouseMaterializedView(
                "visits_by_site",
                RawSQLTerm("SELECT site, count() AS visits FROM visit GROUP BY site"),
                engine="SummingMergeTree",
                order_by=("site",),
            ),
            # Rows written into another table.
            ClickhouseMaterializedView(
                "visits_to_archive", RawSQLTerm("SELECT * FROM visit"), to="visit_archive"
            ),
            # The whole query again every hour.
            ClickhouseMaterializedView(
                "top_sites",
                RawSQLTerm("SELECT site, count() AS visits FROM visit GROUP BY site ORDER BY visits DESC LIMIT 10"),
                refresh="EVERY 1 HOUR",
            ),
        ]
```

| Argument | Meaning |
|---|---|
| `to` | The table the view writes its rows into, instead of a storage of its own. |
| `engine`, `order_by`, `partition_by` | The view's own storage — default a `MergeTree` sorted by `unique_columns`. A key is a column of the view, an expression of hare over the fields of the model declaring the view that the view selects under their own names (`Trunc("visited_at", "month")`), or a `RawSQLTerm`. |
| `refresh` | The schedule the server runs the whole query on — `"EVERY 1 HOUR"`, `"AFTER 30 MINUTE"`, with `OFFSET` and `RANDOMIZE FOR`. The view then holds the rows of its last refresh. |
| `append` | With `refresh`: a refresh adds its rows instead of replacing them. |
| `depends_on` | With a `refresh` of `EVERY` a period: the refreshed views this one is refreshed after. With `AFTER` it raises `ConfigurationError` — ClickHouse takes dependencies of an `EVERY` schedule alone. |
| `with_data` | Fill the view with the rows its query gives now, when it is created. Default `True`. |

A plain `MaterializedView` is a view of its own `MergeTree` sorted by its `unique_columns`. A view's own
sorted storage is created with `allow_nullable_key = 1`: its columns are its query's, and a key column
may hold NULL.

- **Filling.** A view created `with_data` is filled by an `INSERT ... SELECT` of its query right after it
  is created, never by `POPULATE` — which loses the rows inserted while it runs. On a cluster the fill
  runs on the server the connection is to, over that server's rows: a view whose query reads a table
  that isn't distributed — or reads tables hare doesn't know, raw SQL without `read_columns` — raises
  `UnSupportedError` before anything is created. Read distributed tables (named in `read_columns`), or
  declare the view `with_data=False` and fill it on every server yourself.
- **`refresh_materialized_view()`** refreshes a view of `refresh` at once and waits for it, its rows
  replaced in one go. Any other view is emptied and filled again — its rows missing in between, so
  `concurrently=True` raises `UnSupportedError` for it.
- **Changes.** A migration changes the query of a view writing `to` a table (`MODIFY QUERY`) and the
  schedule of a refreshed one (`MODIFY REFRESH`) in place; any other change drops the view and creates it
  again — a storage of its own filled by the new query, a table written to keeping its rows. Rolling
  back the removal of a view writing `to` a table creates it again over the rows its drop left there,
  not filled again; only adding a view fills the table with the rows its query gives.
- **A table remade** by a migration first copies rows into a staging table. Only after a successful
  copy are its dependent views dropped for the exchange, then recreated over the replacement.
  A failed copy leaves the original table and its views in place; the old table is removed after
  the dependencies are recreated.

`hare drift` compares the whole statement of a materialized view as the server writes it — the query,
the target table, the engine, the sort, the partitioning and the schedule.

## <a id="projections"></a>Projections

A projection keeps the rows of a table a second time inside each part, sorted or aggregated as its query
says; the server reads it in place of the table where a query fits it.

```python
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.schema_objects import ClickhouseProjection
from hare.query.functions import Count
from hare.query.queryset import ModelQuery

class Meta:
    table_options = [
        ClickhouseTableOptions(
            projections=(ClickhouseProjection("by_site", ModelQuery().values("site").annotate(visits=Count("id"))),)
        )
    ]
```

The query is a queryset of the table's model — `ModelQuery()` in the model's own `Meta` — written as a
`SELECT` without `FROM` in ClickHouse's SQL; it selects, groups and orders, and a filter, a join or a limit
raises `ConfigurationError` (a projection keeps every row: take a queryset without a model's default filter, a
soft delete's too). Its order is a sorting key: ascending, written without a direction — an order by `-field`
raises `ConfigurationError`. hare knows the columns it reads: a migration changing one drops the projection
before and adds it after, and renaming one remakes the table. Or `RawSQLTerm` of a `SELECT` without `FROM`,
with `GROUP BY` or `ORDER BY`, written as it is — its columns are the ones you name in `read_columns=(...)`. A
migration file keeps a queryset's projection as its SQL with its `read_columns`. A migration adds a projection
with `ADD PROJECTION` and builds it for the stored rows with `MATERIALIZE PROJECTION`, which finishes before
the migration goes on; it drops one with `DROP PROJECTION`.

Projection compilation uses a fresh SQL tree and neither reads nor publishes runnable statement
plans. Rendering or serializing a projection leaves the source queryset unchanged; SQL and
`read_columns` come from the same compilation. Structured projections reject `DISTINCT`, `HAVING`,
CTEs, sampling, row locks and ClickHouse row-selection modifiers as well. Raw SQL is preserved
verbatim, including whitespace; its dependencies remain explicit in `read_columns`.

A table with projections gets the settings rebuilding them as rows are deleted and merged away — a
lightweight `DELETE` and a `ReplacingMergeTree` merge then keep the projections right.

## <a id="dictionaries"></a>Dictionaries

A dictionary keeps rows of a table in the server's memory; a value of them is read by its key with
`DictGet` in place of a `JOIN`. `ClickhouseDictionary` is ClickHouse's `hare.ddl.Dictionary` — the base
every dialect's dictionary subclasses.

```python
from hare.dialects.clickhouse.functions import DictGet
from hare.dialects.clickhouse.schema_objects import ClickhouseDictionary


class Country(Model):
    code = fields.CharField(max_length=2, primary_key=True)
    name = fields.CharField(max_length=50)

    class Meta:
        dictionaries = [ClickhouseDictionary("country_names", key=("code",), attributes=("name",), lifetime=300)]


await Visit.objects.annotate(
    country=DictGet("country_names", "name", "country_code", output_field=fields.CharField(max_length=50))
)
await Country.objects.reload_dictionary("country_names")
```

| Argument | Meaning |
|---|---|
| `key` | The fields a row is looked up by. |
| `attributes` | The fields a lookup gives. |
| `layout` | How the server keeps the rows, with its arguments — `"COMPLEX_KEY_HASHED()"` (default) takes a key of any fields — see [`layout`](#layout) |
| `lifetime` | After how many seconds the dictionary is loaded again — a number, or `(least, greatest)` for the server to pick a moment in between; `0` (default) for never. A `DIRECT` layout takes none — another value raises `ConfigurationError`, and its statement has no `LIFETIME`. |
| `source` | `DictionarySource` with explicit options, or literal `RawSQLTerm` as `SOURCE(...)` takes it. `None` reads the model's table. `hare drift` compares the definition as the server stores it. |

### <a id="layout"></a>`layout`

How the server keeps the rows, with its arguments — `"COMPLEX_KEY_HASHED()"` (default) takes a key of any
fields; `"HASHED()"`, `"FLAT()"` a key of one unsigned 64-bit integer — for any other key the server
keeps it as its `COMPLEX_KEY_` layout (`"HASHED()"` as `COMPLEX_KEY_HASHED()`), which `hare drift` takes
as the declared one. `"DIRECT()"`/`"COMPLEX_KEY_DIRECT()"` keep no rows: each lookup reads the source.

The dictionary is loaded from the model's table by the connection's own user and password, so a row written to
the table is read through the dictionary after its next load — `reload_dictionary()` loads it at once.
`DictGet(..., default=...)` gives a value for a key the dictionary doesn't hold (`dictGetOrDefault`); without,
the server gives the attribute type's own default. A key is a field name, an expression or a literal (a text
in `Value(...)` — a bare one names a field); a key of several fields is looked up by a list or a tuple of
them. A dictionary a model declares is checked when the query is built: the attribute is the name of a field
it gives (or that field's column — `name` reads the column `title` of `CharField(source_field="title")`), the
keys are as many as its key fields, and a key literal is checked as a value of its field; anything else raises
`QueryError`/`ValidationError`. A dictionary declared elsewhere is read as named.

Use an explicit environment reference for runtime credentials:

```python
from hare.declarations.environment_value import EnvironmentValue
from hare.dialects.clickhouse.schema_objects import DictionarySource

source = DictionarySource(
    "CLICKHOUSE",
    table="country",
    user=EnvironmentValue("DICTIONARY_USER"),
    password=EnvironmentValue("DICTIONARY_PASSWORD"),
)
```

Pass this object as `ClickhouseDictionary(..., source=source)`. Option names are case-insensitive SQL
identifiers; their values use the dialect's literal escaping. An option requiring SQL rather than a
literal accepts `RawSQLTerm`. Migration serialization stores environment variable names, and executable
SQL reads their current values when rendered. An unset variable raises `ConfigurationError`.
SQL previews redact environment references and password options as `[HIDDEN]` without reading them,
so preview works without credentials. The default source also hides the connection password in previews.
`RawSQLTerm` remains exact SQL: text such as `{env:NAME}` has no template meaning.

The migration operations are `AddDictionary`, `AlterDictionary`,
`RemoveDictionary` and `RenameDictionary`, written by `makemigrations`. The dictionaries of a model are
dropped with its table, and dropped and created again when a migration remakes or renames the table — the
server drops and renames no table a dictionary reads.

## <a id="cluster"></a>Cluster

A connection with `cluster=<name>` belongs to a cluster of servers:

- Every statement of the schema — `CREATE`, `ALTER`, `DROP`, `TRUNCATE` of tables, views and dictionaries
  — runs `ON CLUSTER <name>`, on every server. A database of the `Replicated` or `Shared` engine replicates
  its
  schema by itself: the connection notices it when it opens and writes no `ON CLUSTER`. SQL you write
  — `execute_script()`, `execute()`, `RunSQL` — is sent as written: name the cluster in it yourself.
- The journal of applied migrations is a `ReplicatedMergeTree`, so every server sees the same migrations.
- A table remade by a migration on a cluster must be replicated or distributed — a table keeping rows of
  its own on each server is refused, as only the rows of one server would be copied. So is a replicated
  table on a cluster of several shards whose replica path names the shard (`{shard}`, as the server's
  default path does): each shard keeps rows of its own — distribute it over its shards instead.
- A remade replicated table — on a cluster or on one server — needs a replica path of its own: the new
  table is created beside the old one, so an engine declaring a path without `{uuid}` (a fixed one, or
  one by `{table}`, which keeps the name the table was created with) is refused before anything
  changes. Declare `ClickhouseEngine` with `replica_path` containing `{uuid}`, or omit its replication
  arguments if the actual server `default_replica_path` contains `{uuid}`. The default is checked
  before the rebuild's first schema change.

`ClickhouseTableOptions(distributed_over=...)` spreads a model's rows over the servers:

```python
class Hit(Model):
    class Meta:
        table_options = [
            ClickhouseTableOptions(
                engine="ReplicatedMergeTree",
                distributed_over="hit_local",
                sharding_key=RawSQLTerm("cityHash64(id)"),
            )
        ]
```

Mutation targets are selected from the model declaration before SQL is rendered. `sql()` therefore
shows the local table and `ON CLUSTER` used by `update()`, `delete()`, instance saves and bulk updates.
The chosen connection participates in plan caching, including its cluster when aliases are shared.
Raw SQL keeps its declared target and does not receive automatic cluster routing.

A migration creates the local table `hit_local` of the engine and the options on every server, and the
model's table `hit` as a `Distributed` table over it — a `distributed_over` naming the model's table
itself raises `ConfigurationError`. Rows are read and written through `hit`, each
written to the shard its `sharding_key` picks (any shard without one). A mutation, a lightweight `DELETE`
and a change of the storage go to `hit_local` on every server; a change of columns to both tables. Each
server runs an `update()` or a `delete()` over its own rows, reading there its own rows of every table its
condition or values name, and no `GLOBAL` exists for a mutation — so one reading a table that isn't
distributed (a relation, a subquery, `Exists()`; a replicated table too: a replica path by `{shard}` keeps
rows per shard) raises `UnSupportedError` instead of leaving the rows of the other servers unchanged.
Distribute that table, or read the keys first and change the rows by `pk__in`. A `delete()` running the
cascade of `Model.delete()` (a model other models point at) reads the keys of its rows first itself. A
table remade is copied through a distributed table over the new local ones, each row to its shard.
`mutations_sync=2` waits for every replica. A `ReplicatedMergeTree` local table keeps its replicas by the
server's `{shard}` and `{replica}` macros — the servers of each shard need a `{shard}` of their own:
servers of two shards sharing one are replicas of one table, each holding every row and each applying a
change of columns sent `ON CLUSTER` again (`DUPLICATE_COLUMN`). On a cluster whose shards have several
replicas the local table needs a `Replicated` engine: one of another engine keeps a row on the replica it
is written to, while a read may ask another, so creating it raises `UnSupportedError` — in a migration
and in `generate_schemas()` alike, before any table is created.

`hare drift` compares both tables of a distributed model — its local table `hit_local`, which it
doesn't report as untracked, and the `Distributed` table `hit` — with the shadow's; a column the two
tables hold another way (`ALTER TABLE hit_local MODIFY COLUMN ...` alone) comes back as an `AlterField`. A
replicated engine declared without its replica path and name (`engine="ReplicatedMergeTree"`) is the same as
the one the server gave the table from its `default_replica_path` and `default_replica_name`; one declared
with a path naming `{database}` or `{table}`, which the server fills with the names on creating the table,
is the same as it too.

A query reading `hit` runs on every shard, each over its own rows — so every table it joins (a relation,
a many-to-many link, `annotate(Count(...))`) and every subquery of its `__in`, `exclude()` and `Exists()`
is read once, on the server the query is sent to, and sent to the shards: hare writes `GLOBAL JOIN` and
`GLOBAL IN` there. A table keeping rows of its own on each server, another distributed model and the rows
of another shard are all seen; without `GLOBAL` a shard would read only its own part of them, and the
server refuses two distributed tables in one statement.
