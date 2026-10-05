# PostgreSQL: indexes and tables

The index types PostgreSQL has beyond the B-tree every database has, and partitioned tables.

## <a id="postgresql-index-types"></a>Index types

`hare.dialects.postgresql.indexes` — all subclass [`PartialIndex`](../../models/indexes.md#partial-index)
with an identical constructor, so every one of these also supports `condition=`:

`GinIndex`, `GistIndex`, `BrinIndex`, `BloomIndex`, `HashIndex`, `SpGistIndex`.

They are indexes of PostgreSQL only, as `HnswIndex`/`IvfflatIndex` are: on a database of another dialect
creating or dropping one — by `generate_schemas()` or a migration — raises `UnSupportedError` before any
SQL. Declare that database's own index in the model, or change the migration. SQLite's own indexes are
the same the other way round — [`FullTextIndex`](../search-and-geodata/full-text-search.md#fulltextindex)
and [`SpatialiteIndex`](../search-and-geodata/gis.md#spatial-index). On ClickHouse an `Index` is a data
skipping index — see [ClickHouse](../clickhouse/models.md).

```python
from hare.dialects.postgresql.indexes import GinIndex


class Book(Model):
    tags = fields.JSONField(default=list, field_type=list[str])

    class Meta:
        indexes = (GinIndex(fields=("tags",)),)
```

## <a id="partitioning"></a>Partitioned tables

`PostgresqlTableOptions(partitioning=...)` makes the table a partitioned one: it is created
`PARTITION BY <strategy> (<key columns>)`, and each partition as a table of its own named
`<table>_<partition name>` (within 63 bytes, like every name hare generates). The classes live in
`hare.dialects.postgresql.partitioning`:

```python
import datetime

from hare import fields
from hare.dialects.postgresql.partitioning import (
    HashPartitioning, ListPartition, ListPartitioning, RangeBound, RangePartition, RangePartitioning,
)
from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions
from hare.models import Model


class ReadingHistory(Model):
    reader_id = fields.BigIntField()
    book_id = fields.BigIntField()
    pk = fields.CompositePrimaryKey("reader_id", "book_id")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                # readinghistory_p0 ... readinghistory_p31, FOR VALUES WITH (MODULUS 32, REMAINDER i)
                partitioning=HashPartitioning(fields=("reader_id",), partition_count=32),
                storage_parameters={"autovacuum_vacuum_scale_factor": 0.02},
            )
        ]


class StoreSale(Model):
    region = fields.CharField(max_length=10)
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("region", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=ListPartitioning(
                    fields=("region",),
                    partitions=[
                        ListPartition("west", values=["us", "ca"]),  # storesale_west FOR VALUES IN ('us', 'ca')
                        ListPartition("east", values=["jp"]),
                    ],
                    default_partition="other",  # storesale_other DEFAULT - every other region
                )
            )
        ]


class LoanRecord(Model):
    day = fields.DateField()
    number = fields.IntField()
    pk = fields.CompositePrimaryKey("day", "number")

    class Meta:
        table_options = [
            PostgresqlTableOptions(
                partitioning=RangePartitioning(
                    fields=("day",),
                    partitions=[
                        RangePartition("old", from_values=(RangeBound.MINVALUE,), to_values=(datetime.date(2026, 1, 1),)),
                        RangePartition(
                            "y2026", from_values=(datetime.date(2026, 1, 1),), to_values=(datetime.date(2027, 1, 1),)
                        ),
                    ],
                    default_partition="later",
                )
            )
        ]
```

| Class | Arguments | Partitions |
|---|---|---|
| `HashPartitioning` | `fields`, `partition_count` (1 to 1024) | `<table>_p0` ... `<table>_p<n-1>`, rows spread evenly by a hash of the key. |
| `ListPartitioning` | `fields` (one), `partitions` (`ListPartition(name, values)`), `default_partition` | Each partition holds the rows of the key values it lists; `None` among them means the rows with a `NULL` key. |
| `RangePartitioning` | `fields`, `partitions` (`RangePartition(name, from_values, to_values)`), `default_partition` | Each partition holds the rows from its lower bound up to, not including, its upper one — one value per key column. `RangeBound.MINVALUE`/`RangeBound.MAXVALUE` stand below/above every value. |

`fields` name the key: fields with a column, or a foreign key, which stands for its key column(s).
`default_partition` is the name of the partition for the rows no other one holds; without it, a row no
partition holds is refused by the database (`IntegrityError`). The order the partitions are listed in
doesn't matter.

What PostgreSQL requires is checked before any DDL is written — when the table is created, and when a
later migration adds a constraint, an index or a field or changes a field (`AddConstraint`, `AddIndex`,
`AddField`, `AlterField`) — `ConfigurationError` names the problem:

- the key fields exist and have columns;
- the primary key, every unique field, `UniqueConstraint`, unique `Index` and `ExclusionConstraint` (with
  `=`) includes every key column — PostgreSQL enforces each within one partition. A model whose key is
  one generated `id` can only be partitioned by that `id`;
- a partitioned table can't be `unlogged`;
- the partitions' names differ, a `ListPartitioning` value is listed once, `RangePartition`s don't
  overlap and give one value per key column.

An `ExclusionConstraint` on a partitioned table needs PostgreSQL 17 (`UnSupportedError` on an older
server). Foreign keys to and from a partitioned table work as on a plain one.

A partitioned table holds no rows itself, so `storage_parameters` are set on each partition (at creation
and when they change), and `tablespace` on the table — where new partitions go — and on each partition.
Indexes and constraints are created on the table and reach every partition. A concurrent index
(`AddIndex(concurrently=True)`) can't be built on a partitioned table as a whole: hare creates it on the
table alone, builds it concurrently on each partition and attaches each — it is valid once the last one
is attached; dropping it is never concurrent.

How a change reaches the database:

| Change | Migration |
|---|---|
| A `ListPartition`/`RangePartition` or the default partition added | `AddPartition` — a new, empty partition. PostgreSQL refuses it while the default partition holds rows that belong to it. |
| One removed | `RemovePartition` — the partition is detached and dropped **with its rows**; `makemigrations` warns. |
| A partition's values or bounds changed | `RemovePartition` and `AddPartition` of the same name — its rows are lost. |
| `storage_parameters`, `tablespace` | `AlterModelOptions`, in place on every partition. |
| Partitioning added or removed, another strategy, key or `partition_count` | `AlterModelOptions` that creates the table anew: the rows are copied, the indexes, constraints, comments, triggers and the foreign keys of other tables referencing it are set again. It rewrites the whole table under an exclusive lock. |
| `Meta.table` changed, `RenameModel` | The partitions are renamed with the table. |

On SQLite (and any other dialect) the `PostgresqlTableOptions` entry is not used: the table is a plain
one, and the partition operations only change the migration state there.
