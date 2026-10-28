# ClickHouse: models

A ClickHouse table is sorted by a key, not indexed by it: nothing in the server keeps a key unique or a
relation pointing at a row. A key a model declares generated comes from the server's own `generateSerialID()`
(see [Keys](#keys)), and hare runs the `on_delete` of the relations itself; a uniqueness or a relation with
`db_constraint=True` is refused when the model is bound (see [Uniqueness and relations](#uniqueness-and-relations)).
`ClickhouseTableOptions` says how the server stores the rows.

```python
import uuid

from datetime import timedelta

from hare import fields
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.models import Model
from hare.query.expressions import F
from hare.query.functions import Trunc


class PageView(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)
    site = fields.CharField(max_length=50)
    viewed_at = fields.DatetimeField()

    class Meta:
        table_options = [
            ClickhouseTableOptions(
                order_by=("id", "site", "viewed_at"),
                partition_by=Trunc("viewed_at", "month"),
                ttl=F("viewed_at") + timedelta(days=365),
                settings=(("index_granularity", 8192),),
            )
        ]
```

## <a id="keys"></a>Keys

A key the database generates — `IntField(primary_key=True)`, `BigIntField(primary_key=True)` — is taken
from a series of numbers ClickHouse Keeper keeps (`generateSerialID`). Before the rows
are written, hare takes as many numbers as there are rows without a key in one query and sets them; each
number is handed out once, across every process writing the model.

- The series is named after the database and the model (`<database>.<app_label>.<Model>`), not its
  table: renaming the table keeps it, and each database of a server has a series of its own.
- A server keeps at most `max_autoincrement_series` series (1000 by default), and dropping a database
  leaves its series in Keeper. With `keeper_hosts` naming the server's Keeper, deleting the database
  (`db_delete()` — a test database at the end of its tests) deletes its series too.
- Rows written with keys of their own (an import, a copy from another database) leave the series behind.
  The first write of a model in a process moves the series past the greatest key of its table; the
  migration operation `SynchronizeKeySeries("Model")` does it at once.
- A key beyond the field's range raises `ValidationError` before anything is written.
- On a server without a Keeper, such a model is refused when it is bound — use
  `UUIDField(primary_key=True, default=uuid.uuid4)` (or `default=uuid.uuid7` on Python 3.14), or set the
  key on every row.

## <a id="uniqueness-and-relations"></a>Uniqueness and relations

ClickHouse keeps no unique constraint and no foreign key (`Features.supports_unique_constraints` and
`Features.supports_foreign_keys` are off), so a model bound to a ClickHouse connection declares neither,
and one that does raises `ConfigurationError` when it is bound:

| Declared | When the model is bound |
|---|---|
| `unique=True` on a field other than the primary key, `UniqueConstraint`, `Index(unique=True)`, a unique `PartialIndex` | `ConfigurationError` — ClickHouse keeps nothing unique |
| A relation (`ForeignKeyField`, `ManyToManyField` with its automatic through table) of `db_constraint=True`, the default | `ConfigurationError` — declare it `db_constraint=False` |
| A `OneToOneField`, of any `db_constraint` — its key is unique | `ConfigurationError` — ClickHouse keeps nothing unique; declare a `ForeignKeyField` of `db_constraint=False` |

A migration declaring a uniqueness — `CreateModel` or `AddField` of a `unique=True` field, `AlterField`
making one unique, `AddIndex`/`AddConstraint` of a unique one — is refused the same way, with
`UnSupportedError` before its first statement runs.

```python
class Order(Model):
    id = fields.BigIntField(primary_key=True)
    customer = fields.ForeignKeyField("models.Customer", db_constraint=False)
```

What follows from it:

- **The primary key isn't unique.** ClickHouse's `ORDER BY` key sorts the rows; a `create()` of a key
  already stored writes a second row of it. A table keeping versions of a row — `ReplacingMergeTree`,
  `CollapsingMergeTree` and the other engines `final()` reads — merges them by design.
- **`get_or_create()` and `update_or_create()`** read the row, then write it: two of them at once can both
  create one, as on any database without a unique index.
- **`in_bulk(field_name=...)`** takes the primary key alone (`"pk"`, the default); another field raises
  `QueryError`, as no field is unique.
- **A many-to-many `add()`** reads the pairs already linked before it inserts; two
  `add()`s of one pair at once can both insert it.
- **`on_delete`** is run by hare itself, as for a `db_constraint=False` relation on any database.

## <a id="table-options"></a>Table options

`ClickhouseTableOptions` in `Meta.table_options`:

| Option | Meaning |
|---|---|
| `engine` | The table engine with its arguments — `"MergeTree"`, `"ReplacingMergeTree(version)"`. Default `"MergeTree"`. The options of the `MergeTree` family below are refused with another engine. |
| `order_by` | What the rows are sorted by — field names and [expressions](#expressions), the primary key first; a foreign key or one-to-one relation by its name sorts by its key columns (`"team"` is `team_id`). Empty: the primary key, or `tuple()` for a model without one. A nullable field in it or in `partition_by` needs `settings=(("allow_nullable_key", 1),)` — else `ConfigurationError`. |
| `sample_by` | The expression [`sample()`](query-modifiers.md#sample) reads a share of — one of `order_by`. The table's `PRIMARY KEY` then runs from the model's key through it. |
| `partition_by` | The expression the rows are partitioned by — `Trunc("viewed_at", "month")`. |
| `ttl` | The table's `TTL` — see [`ttl`](#ttl) |
| `settings` | The table's `SETTINGS`, as `(name, value)` pairs — a value a string or an integer. |
| `column_codecs` | The compression of columns — `(field name, codecs)` pairs: `("payload", "ZSTD(3)")`, `("created", "Delta, ZSTD")`. |
| `column_ttls` | How long columns keep their values — `(field name, expression)` pairs, the moment a value is reset to its default. Not of a key column. |
| `projections` | The table's projections — see [Schema objects](schema-objects.md#projections). |
| `distributed_over` | The local table storing the rows on every server of the connection's cluster — the model's table is then a `Distributed` one over it. See [Cluster](schema-objects.md#cluster). |
| `sharding_key` | With `distributed_over`: the expression a row's shard is picked by; any shard without. One reading a nullable field raises `ConfigurationError` — ClickHouse shards by an integer that holds no NULL. |
| `lightweight_updates` | Rows change by a lightweight `UPDATE` — see [below](#lightweight-updates). |

### <a id="expressions"></a>Expressions

A key, a time to live and a sharding key are a field's name, an expression of hare over the model's
fields — `F`, functions, arithmetic: `Trunc("viewed_at", "month")`, `F("viewed_at") + timedelta(days=30)`
— or `RawSQLTerm` of raw SQL. An expression of hare is written in ClickHouse's SQL, a migration renaming a
field it reads renames it there too, and hare knows the columns it reads — the columns it doesn't update
and the ones a migration changes by remaking the table (below). Raw SQL is written as it is: hare neither
renames a field in it nor reads its columns, so the server refuses an update or a change of a column only
a raw key reads.

### <a id="ttl"></a>`ttl`

The table's `TTL`: an expression of the moment a row is deleted, or `RawSQLTerm` of a whole clause with
another action (`RawSQLTerm("viewed_at + INTERVAL 1 MONTH TO VOLUME 'cold'")`). A TTL of a nullable field
(`null=True`) raises `UnSupportedError` — `RawSQLTerm("assumeNotNull(seen) + INTERVAL 30 DAY")` takes its
moment; so does a column's TTL.

A model without these options is a `MergeTree` sorted by its primary key. The primary key is the engine's
`PRIMARY KEY` clause, written after `ORDER BY`.

A migration changes what the server changes in place — `ttl`, `settings` (but `index_granularity` and
`index_granularity_bytes`, set when the table is created), `column_codecs`, `column_ttls`, `projections`,
`lightweight_updates` — with `ALTER TABLE`. Any other change (the engine, `order_by`, `sample_by`,
`partition_by`, the distribution) remakes the table: a new table is created, the rows copied, the old one
dropped and the new renamed, the views and dictionaries over it dropped and created again around it, its
columns keeping their codecs and times to live. On a cluster a table holding rows of its own on each
server is refused there — only the rows of one server would be copied. A column of the table's keys (its
primary key, `order_by`, `sample_by`, `partition_by`, sharding key — a field's name or an expression of
hare), one a projection reads (its queryset's columns, a raw projection's `read_columns`), one a
dictionary reads, and any column of a table with a materialized view, can't be renamed in place: `RenameField`
of its field remakes the table the same way — the field renamed in the dictionaries' attributes and the
materialized views' keys with it, their raw queries left as written — the column under its new name, and so
does an `AlterField` changing its type or nullability — the table of a relation storing the key's values in a
column of its own keys is remade likewise when the key takes a new type; a column a data skipping index reads
gets a new type or nullability with the index dropped for the change and created again — the one index over
every key column of a relation to a composite key too. `hare drift` compares the options with the server's, as
the server formats them.

When a migration changes fields too, a changed `ttl`, column codec, column TTL or projection —
expressions over columns — is removed before the fields change and set after them: a field made NOT NULL
takes its new `ttl` after its change (ClickHouse refuses a `ttl` over a `Nullable` column), one made
nullable loses its `ttl` before. A changed key (`order_by`, `partition_by`, `sample_by`, the sharding
key) or engine keeps its old value until the fields are added and takes the new one after them; the
fields removed are removed last. `makemigrations` writes this as an `AlterModelOptions` on each side of
the field operations. A field made NOT NULL while a row holds NULL in it raises `IntegrityError` before
the column changes, as on PostgreSQL — the `MODIFY COLUMN` would fail in its mutation, which stays and
fails every later one; fill the NULLs first. A field made nullable on ClickHouse before 26.3 — see
[Faults of a server version](differences.md#server-faults). Before a column changes its type the
rows a lightweight `DELETE` only marked are removed (`APPLY DELETED MASK`): the `MODIFY COLUMN` converts
them too, and one the new type can't hold would fail it, which the checks of the rows left can't see.

## <a id="lightweight-updates"></a>Lightweight updates

`ClickhouseTableOptions(lightweight_updates=True)` sets the table settings
`enable_block_number_column` and `enable_block_offset_column`; `save()`, `update()` and `bulk_update()`
of such a table write `UPDATE t SET ... WHERE ...`. The new values are written beside the row and read in
place of the old ones at once — a mutation rewrites each part holding a row instead. A key column isn't
updated either way. A migration turns the option on or off with `MODIFY SETTING`/`RESET SETTING`, without
remaking the table.

ClickHouse doesn't update a column its keys (`order_by`, `sample_by`, `partition_by`, sharding key — a
field's name or an expression of hare; a raw key's columns the server refuses itself) or its engine's
arguments (a version, a sign) read. `save()` leaves such a column as it is — it reads the
row's key columns first and raises `UnSupportedError` when the instance changed one; `update()`,
`bulk_update()` and `save(update_fields=...)` naming one raise `UnSupportedError`. The sorting key begins
with the primary key, so a part of a composite primary key is such a column too: `update(number=...)` of
one raises `UnSupportedError`. A row gets a new key by being deleted and created again.

## <a id="data-skipping-indexes"></a>Data skipping indexes

An index of ClickHouse skips the granules (8192 rows by default) that can't hold a row a query looks for.
`Index(fields=...)` is a `minmax` index of granularity 1; `hare.dialects.clickhouse.indexes` has every
type, each with `granularity`:

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
`hare drift` read the type and its arguments back — a `bloom_filter` written without its argument as one
of ClickHouse's default 0.025 — and an expression key (`BloomFilterIndex(Lower("title"), name=...)`) as its
SQL. A `MinMaxIndex` of granularity 1 is the same
index as a plain `Index`. A key keeps no direction: `"-field"`, `.asc()` and `.desc()` keys raise
`UnSupportedError` when the index is created. An index takes no condition: a `PartialIndex` is created as
the index of every row.

Each index type is a `ClickhouseIndex` (`hare.dialects.clickhouse.indexes`) setting its type's name and
arguments; it is created and dropped on a ClickHouse database only.

## <a id="generated-columns"></a>Generated columns

A `GeneratedField` is a `MATERIALIZED` column with `stored=True` — computed when the row is written — and
an `ALIAS` column with `stored=False` — computed when it is read. Neither is written by an insert, and an
`ALIAS` column can't be in `order_by`. Its `output_field` may be a ClickHouse type
(`GeneratedField(RawSQLTerm("cityHash64(name)"), UInt64Field())`, as `inspectdb` writes one) — the
migrations and `hare drift` compare it as a field of that type.

## <a id="column-types"></a>Column types

Every field of hare has a ClickHouse type, and `hare.dialects.clickhouse.fields` adds ClickHouse's own —
see [Types](types.md).
