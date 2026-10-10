# ClickHouse schema objects

Views, materialized views, projections and dictionaries are declared on the model, created and
changed by migrations and compared by `hare drift`, as on PostgreSQL — see
[Views, functions, sequences and access](../../models/schema-objects.md). A connection to a cluster
runs every statement of the schema on all its servers.

## <a id="views"></a>Views

`Meta.views` with `View(name, query)` is a ClickHouse view — a table of the `View` engine, renamed
as one and not dropped with the table it reads. `hare drift` compares a view's query as the server
parses it, so a query written in other words but the same is no drift.

## <a id="materialized-views"></a>Materialized views

A ClickHouse materialized view follows the rows inserted into the table its query reads: each
inserted block goes through the query and into the view. `ClickhouseMaterializedView` says where its
rows go:

```python
from hare.ddl import RawSQLTerm
from hare.dialects.clickhouse.schema_objects.clickhouse_materialized_view import ClickhouseMaterializedView


class Visit(Model):
    site = fields.CharField(max_length=50)

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
            # The whole query again every hour (ClickHouse 24.10).
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
| `engine`, `order_by`, `partition_by` | The view's own storage — default a `MergeTree` sorted by `unique_columns`. |
| `refresh` | The schedule the server runs the whole query on — `"EVERY 1 HOUR"`, `"AFTER 30 MINUTE"`, with `OFFSET` and `RANDOMIZE FOR`. The view then holds the rows of its last refresh. |
| `append` | With `refresh`: a refresh adds its rows instead of replacing them. |
| `depends_on` | With `refresh`: the refreshed views this one is refreshed after. |
| `with_data` | Fill the view with the rows its query gives now, when it is created. Default `True`. |

A plain `MaterializedView` is a view of its own `MergeTree` sorted by its `unique_columns`.

- **Filling.** A view created `with_data` is filled by an `INSERT ... SELECT` of its query right after
  it is created, never by `POPULATE` — which loses the rows inserted while it runs.
- **`refresh_materialized_view()`** refreshes a view of `refresh` at once and waits for it, its rows
  replaced in one go. Any other view is emptied and filled again — its rows missing in between, so
  `concurrently=True` raises `UnSupportedError` for it.
- **Changes.** A migration changes the query of a view writing `to` a table (`MODIFY QUERY`) and the
  schedule of a refreshed one (`MODIFY REFRESH`) in place; any other change drops the view and creates
  it again — a storage of its own filled by the new query, a table written to keeping its rows.
- **A table remade** by a migration takes its views with it: they are dropped before the copy of its
  rows and created again after — a view would otherwise take the copied rows a second time.

`hare drift` reads the query, the target table, the engine, the sort, the partitioning and the
schedule back from the server.

## <a id="projections"></a>Projections

A projection keeps the rows of a table a second time inside each part, sorted or aggregated as its
query says; the server reads it in place of the table where a query fits it.

```python
from hare.dialects.clickhouse.schema_objects import ClickhouseProjection

class Meta:
    table_options = [
        ClickhouseTableOptions(
            projections=(ClickhouseProjection("by_site", RawSQLTerm("SELECT site, count() GROUP BY site")),)
        )
    ]
```

The query is a `SELECT` without `FROM`, with `GROUP BY` or `ORDER BY`. A migration adds a projection
with `ADD PROJECTION` and builds it for the stored rows with `MATERIALIZE PROJECTION`, which finishes
before the migration goes on; it drops one with `DROP PROJECTION`.

On ClickHouse 24.8 and later (`Features.rebuilds_projections`) a table with projections gets the
settings rebuilding them as rows are deleted and merged away — a lightweight `DELETE` and a
`ReplacingMergeTree` merge then keep the projections right. On an older server the rows of such a
table are deleted by an `ALTER TABLE ... DELETE` mutation.

## <a id="dictionaries"></a>Dictionaries

A dictionary keeps rows of a table in the server's memory; a value of them is read by its key with
`DictGet` in place of a `JOIN`.
`ClickhouseDictionary` is ClickHouse's `hare.ddl.Dictionary` — the base every dialect's dictionary
subclasses.

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
| `layout` | How the server keeps the rows, with its arguments — `"COMPLEX_KEY_HASHED()"` (default) takes a key of any fields; `"HASHED()"`, `"FLAT()"` a key of one unsigned 64-bit integer. |
| `lifetime` | After how many seconds the dictionary is loaded again — a number, or `(least, greatest)` for the server to pick a moment in between; `0` (default) for never. |
| `source` | `RawSQLTerm` of another source than the model's table, as `SOURCE(...)` takes it. |

The dictionary is loaded from the model's table by the connection's own user and password, so a
row written to the table is read through the dictionary after its next load — `reload_dictionary()`
loads it at once. `DictGet(..., default=...)` gives a value for a key the dictionary doesn't hold
(`dictGetOrDefault`); without, the server gives the attribute type's own default. A key of several
fields is looked up by a sequence of them.

A password never reaches the migration file: the connection's own is put into the statement when the
dictionary is created (`[HIDDEN]` in `hare sqlmigrate`), and a `{env:NAME}` in a `source` is replaced
by the environment variable `NAME` then. The migration operations are `AddDictionary`,
`AlterDictionary`, `RemoveDictionary` and `RenameDictionary`, written by `makemigrations`; a
dictionary isn't dropped with the table it reads.

## <a id="cluster"></a>Cluster

A connection with `cluster=<name>` belongs to a cluster of servers:

- Every statement of the schema — `CREATE`, `ALTER`, `DROP`, `TRUNCATE` of tables, views and
  dictionaries — runs `ON CLUSTER <name>`, on every server. A database of the `Replicated` engine
  replicates its schema by itself: the connection notices it when it opens and writes no
  `ON CLUSTER`.
- The journal of applied migrations is a `ReplicatedMergeTree`, so every server sees the same
  migrations.
- A table remade by a migration on a cluster must be replicated or distributed — a table keeping
  rows of its own on each server is refused, as only the rows of one server would be copied.

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

A migration creates the local table `hit_local` of the engine and the options on every server, and
the model's table `hit` as a `Distributed` table over it. Rows are read and written through `hit`,
each written to the shard its `sharding_key` picks (any shard without one). A mutation, a lightweight
`DELETE` and a change of the storage go to `hit_local` on every server; a change of columns to both
tables. A table remade is copied through a distributed table over the new local ones, each row to
its shard. `mutations_sync=2` waits for every replica.
