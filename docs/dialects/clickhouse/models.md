# ClickHouse models

A ClickHouse table is sorted by a key, not indexed by it: nothing in the server keeps a key unique
or a relation pointing at a row. hare takes over what a model declares — keys, uniqueness,
relations — and `ClickhouseTableOptions` says how the server stores the rows.

```python
import uuid

from hare import fields
from hare.ddl import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.models import Model


class PageView(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    site = fields.CharField(max_length=50)
    viewed_at = fields.DatetimeField()

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                order_by=("id", "site", "viewed_at"),
                partition_by=RawSQLTerm("toYYYYMM(viewed_at)"),
                ttl=RawSQLTerm("toDateTime(viewed_at) + INTERVAL 1 YEAR"),
                settings=(("index_granularity", 8192),),
            )
        ]
```

## <a id="keys"></a>Keys

A key the database generates — `IntField(primary_key=True)`, `BigIntField(primary_key=True)` — is
taken from a series of numbers ClickHouse Keeper keeps (`generateSerialID`, ClickHouse 25.1). Before
the rows are written, hare takes as many numbers as there are rows without a key in one query and
sets them; each number is handed out once, across every process writing the model.

- The series is named after the model (`<app_label>.<Model>`), not its table: renaming the table
  keeps it.
- Rows written with keys of their own (an import, a copy from another database) leave the series
  behind. The first write of a model in a process moves the series past the greatest key of its
  table; the migration operation `SynchronizeKeySeries("Model")` does it at once.
- A key beyond the field's range raises `ValidationError` before anything is written.
- On a server older than 25.1 or without a Keeper, such a model is refused when it is bound —
  use `UUIDField(primary_key=True, default=uuid.uuid4)` (or `default=uuid.uuid7` on Python 3.14),
  or set the key on every row.

## <a id="uniqueness-and-relations"></a>Uniqueness and relations

ClickHouse keeps no unique constraint and no foreign key (`Features.checks_constraints_before_write`),
so before `create()`, `save()`, `bulk_create()`, `update()` and `bulk_update()` hare checks what the
model declares:

| Declared | Checked |
|---|---|
| The primary key, `unique=True`, `UniqueConstraint`, `Index(unique=True)` | No stored row and no other written row has the same values. A `UniqueConstraint(condition=Q(...))` covers the rows its condition holds for. |
| A relation (`ForeignKeyField`, `OneToOneField`) of `db_constraint=True`, the default | The row it points to exists. |

A breach raises `IntegrityError`, as the same write raises on PostgreSQL and SQLite, and nothing is
written. A `None` value is never checked, and a relation of `db_constraint=False` neither. A batch
takes one `SELECT` per declared uniqueness and per relation, its keys sent as an external table; the
rows of the batch repeating a key are found without a query. `update()` checks only the uniqueness
its fields change.

- **A table keeping versions of a row** — `ReplacingMergeTree`, `CollapsingMergeTree` and the other
  engines `final()` reads — repeats a key by design: its key isn't checked.
- **The check and the write are two statements.** Another writer can write a row between them,
  leaving two rows of one key — the check is what a single application writing through hare relies
  on, not a lock.

`on_delete` is run by hare itself, as for a `db_constraint=False` relation on any database.

## <a id="table-options"></a>Table options

`ClickhouseTableOptions` in `Meta.table_options`:

| Option | Meaning |
|---|---|
| `engine` | The table engine with its arguments — `"MergeTree"`, `"ReplacingMergeTree(version)"`. Default `"MergeTree"`. The options of the `MergeTree` family below are refused with another engine. |
| `order_by` | What the rows are sorted by — field names and `RawSQLTerm` expressions, the primary key first. Empty: the primary key, or `tuple()` for a model without one. |
| `sample_by` | The expression [`sample()`](query-modifiers.md#sample) reads a share of — a field name or `RawSQLTerm`, one of `order_by`. The table's `PRIMARY KEY` then runs from the model's key through it. |
| `partition_by` | `RawSQLTerm` of the expression the rows are partitioned by. |
| `ttl` | `RawSQLTerm` of the table's `TTL`. |
| `settings` | The table's `SETTINGS`, as `(name, value)` pairs — a value a string or an integer. |
| `column_codecs` | The compression of columns — `(field name, codecs)` pairs: `("payload", "ZSTD(3)")`, `("created", "Delta, ZSTD")`. |
| `column_ttls` | How long columns keep their values — `(field name, RawSQLTerm)` pairs, the moment a value is reset to its default. Not of a key column. |
| `projections` | The table's projections — see [Schema objects](schema-objects.md#projections). |
| `distributed_over` | The local table storing the rows on every server of the connection's cluster — the model's table is then a `Distributed` one over it. See [Cluster](schema-objects.md#cluster). |
| `sharding_key` | With `distributed_over`: `RawSQLTerm` of the expression a row's shard is picked by; any shard without. |
| `lightweight_updates` | Rows change by a lightweight `UPDATE` — see [below](#lightweight-updates). |

A model without these options is a `MergeTree` sorted by its primary key. The primary key is the
engine's `PRIMARY KEY` clause, written after `ORDER BY`.

A migration changes what the server changes in place — `ttl`, `settings` (but
`index_granularity` and `index_granularity_bytes`, set when the table is created), `column_codecs`,
`column_ttls`, `projections`, `lightweight_updates` — with `ALTER TABLE`. Any other change (the
engine, `order_by`, `sample_by`, `partition_by`, the distribution) remakes the table: a new table is
created, the rows copied, the old one dropped and the new renamed, the views over it dropped and
created again around it. On a cluster a table holding rows of its own on
each server is refused there — only the rows of one server would be copied. `hare drift` compares
the options with the server's, as the server formats them.

## <a id="lightweight-updates"></a>Lightweight updates

`ClickhouseTableOptions(lightweight_updates=True)` (ClickHouse 25.7) sets the table settings
`enable_block_number_column` and `enable_block_offset_column`; `save()`, `update()` and
`bulk_update()` of such a table write `UPDATE t SET ... WHERE ...`. The new values are written
beside the row and read in place of the old ones at once — a mutation rewrites each part holding a
row instead. A key column isn't updated either way. A migration turns the option on or off with
`MODIFY SETTING`/`RESET SETTING`, without remaking the table.

## <a id="data-skipping-indexes"></a>Data skipping indexes

An index of ClickHouse skips the granules (8192 rows by default) that can't hold a row a query looks
for. `Index(fields=...)` is a `minmax` index of granularity 1; `hare.dialects.clickhouse.indexes` has
every type, each with `granularity`:

| Index | Type | Skips the granules |
|---|---|---|
| `MinMaxIndex` | `minmax` | whose least and greatest value a comparison or a range can't match |
| `SetIndex(max_rows=0)` | `set(max_rows)` | holding none of the values a filter looks for |
| `BloomFilterIndex(false_positive=0.025)` | `bloom_filter(...)` | surely holding none of the values of an equality, an `__in` or an array's `__contains` |
| `TokenBloomFilterIndex(filter_size=256, hash_functions=2, seed=0)` | `tokenbf_v1(...)` | without a word an equality or a whole-word search looks for |
| `NgramBloomFilterIndex(ngram_size=3, ...)` | `ngrambf_v1(...)` | without the n-grams of the text of a `__contains`, a `__startswith` or an equality — a text shorter than an n-gram skips none |

```python
from hare.dialects.clickhouse.indexes import BloomFilterIndex

class Meta:
    indexes = [BloomFilterIndex(fields=["user_id"], name="user_bloom", granularity=4)]
```

An index is added and dropped with `ALTER TABLE ... ADD INDEX`/`DROP INDEX`; `hare inspectdb` and
`hare drift` read the type and its arguments back.

Each index type is a `ClickhouseIndex` (`hare.dialects.clickhouse.indexes`) setting its type's name
and arguments; it is created and dropped on a ClickHouse database only.

## <a id="generated-columns"></a>Generated columns

A `GeneratedField` is a `MATERIALIZED` column with `stored=True` — computed when the row is written
— and an `ALIAS` column with `stored=False` — computed when it is read. Neither is written by an
insert, and an `ALIAS` column can't be in `order_by`.

## <a id="column-types"></a>Column types

Every field of hare has a ClickHouse type, and `hare.dialects.clickhouse.fields` adds ClickHouse's
own — see [Types](types.md).
