# ClickHouse: models

A ClickHouse table is sorted by a key, not indexed by it: nothing in the server keeps a key unique or a
relation pointing at a row. hare takes over what a model declares — keys, uniqueness, relations — and
`ClickhouseTableOptions` says how the server stores the rows.

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

A key the database generates — `IntField(primary_key=True)`, `BigIntField(primary_key=True)` — is taken
from a series of numbers ClickHouse Keeper keeps (`generateSerialID`, ClickHouse 25.1). Before the rows
are written, hare takes as many numbers as there are rows without a key in one query and sets them; each
number is handed out once, across every process writing the model.

- The series is named after the model (`<app_label>.<Model>`), not its table: renaming the table keeps
  it.
- Rows written with keys of their own (an import, a copy from another database) leave the series behind.
  The first write of a model in a process moves the series past the greatest key of its table; the
  migration operation `SynchronizeKeySeries("Model")` does it at once.
- A key beyond the field's range raises `ValidationError` before anything is written.
- On a server older than 25.1 or without a Keeper, such a model is refused when it is bound — use
  `UUIDField(primary_key=True, default=uuid.uuid4)` (or `default=uuid.uuid7` on Python 3.14), or set the
  key on every row.

## <a id="uniqueness-and-relations"></a>Uniqueness and relations

ClickHouse keeps no unique constraint and no foreign key (`Features.checks_constraints_before_write`), so
before `create()`, `save()`, `bulk_create()`, `update()` and `bulk_update()` hare checks what the model
declares:

| Declared | Checked |
|---|---|
| The primary key, `unique=True`, `UniqueConstraint`, `Index(unique=True)` | No stored row and no other written row has the same values — an array compared by its items. A `UniqueConstraint(condition=Q(...))` covers the rows its condition holds for. |
| A relation (`ForeignKeyField`, `OneToOneField`) of `db_constraint=True`, the default | The row it points to exists. |

A breach raises `IntegrityError`, as the same write raises on PostgreSQL and SQLite, and nothing is
written. A primary key field left to its `db_default` (`DateField(db_default=Now())` in a
`CompositePrimaryKey`) raises `UnSupportedError` before anything is written: the server returns no row of
a write, so the key could neither be checked unique nor read back — give it a value, or a `default=`
computing one in Python. A `None` value is never checked, and a relation of `db_constraint=False`
neither. A batch takes one `SELECT` per declared uniqueness and per relation, its keys sent as an
external table; the rows of the batch repeating a key are found without a query. `update()` checks only
the uniqueness its fields change. hare checks a uniqueness of fields, with no condition or a `Q` one: a
model declaring `Index(<expression>, unique=True)`, or a `UniqueConstraint`/unique `PartialIndex` whose
condition is a `RawSQLTerm`, raises `ConfigurationError` when it is bound to a ClickHouse connection.

- **A table keeping versions of a row** — `ReplacingMergeTree`, `CollapsingMergeTree` and the other
  engines `final()` reads — repeats a key by design: its key isn't checked.
- **The check and the write are two statements.** Within one process, a write holds the values it checks
  until its statements end. Concurrent `create()`, `save()`, `bulk_create()`, `update()` and
  `bulk_update()` calls of one value (tasks of one event loop) run one after another, and all but the
  first raise `IntegrityError`. Another process can write a row between the check and the write, leaving
  two rows of one key — the check is what a single process writing through hare relies on, not a lock on
  the server. Inside a transaction the value is held until the write ends, not until the commit, and
  another connection doesn't see the row before the commit.
- **A replicated table is checked on the replica the query reaches.** A row written through another
  replica and not replicated to it yet isn't seen — a `Distributed` table over replicas reads one replica
  of each shard. Write the rows of one key through one server, or wait for the replicas (`insert_quorum`,
  `SYSTEM SYNC REPLICA`) before writing it again.

`on_delete` is run by hare itself, as for a `db_constraint=False` relation on any database.

## <a id="table-options"></a>Table options

`ClickhouseTableOptions` in `Meta.table_options`:

| Option | Meaning |
|---|---|
| `engine` | The table engine with its arguments — `"MergeTree"`, `"ReplacingMergeTree(version)"`. Default `"MergeTree"`. The options of the `MergeTree` family below are refused with another engine. |
| `order_by` | What the rows are sorted by — field names and `RawSQLTerm` expressions, the primary key first; a foreign key or one-to-one relation by its name sorts by its key columns (`"team"` is `team_id`). Empty: the primary key, or `tuple()` for a model without one. |
| `sample_by` | The expression [`sample()`](query-modifiers.md#sample) reads a share of — a field name or `RawSQLTerm`, one of `order_by`. The table's `PRIMARY KEY` then runs from the model's key through it. |
| `partition_by` | `RawSQLTerm` of the expression the rows are partitioned by. |
| `ttl` | `RawSQLTerm` of the table's `TTL` — see [`ttl`](#ttl) |
| `settings` | The table's `SETTINGS`, as `(name, value)` pairs — a value a string or an integer. |
| `column_codecs` | The compression of columns — `(field name, codecs)` pairs: `("payload", "ZSTD(3)")`, `("created", "Delta, ZSTD")`. |
| `column_ttls` | How long columns keep their values — `(field name, RawSQLTerm)` pairs, the moment a value is reset to its default. Not of a key column. |
| `projections` | The table's projections — see [Schema objects](schema-objects.md#projections). |
| `distributed_over` | The local table storing the rows on every server of the connection's cluster — the model's table is then a `Distributed` one over it. See [Cluster](schema-objects.md#cluster). |
| `sharding_key` | With `distributed_over`: `RawSQLTerm` of the expression a row's shard is picked by; any shard without. |
| `lightweight_updates` | Rows change by a lightweight `UPDATE` — see [below](#lightweight-updates). |

### <a id="ttl"></a>`ttl`

`RawSQLTerm` of the table's `TTL`. ClickHouse 24.3 keeps a TTL of a `Date` or a `DateTime` only — a
`DateField` (`Date32`) or a `DatetimeField` (`DateTime64`) in it is converted with `toDateTime(seen)`,
else its DDL raises `UnSupportedError`; so is a column's TTL. A TTL of a nullable field (`null=True`)
raises `UnSupportedError` on every version — `assumeNotNull(seen) + INTERVAL 30 DAY` or
`ifNull(seen, now()) + ...` takes its moment.

A model without these options is a `MergeTree` sorted by its primary key. The primary key is the engine's
`PRIMARY KEY` clause, written after `ORDER BY`.

A migration changes what the server changes in place — `ttl`, `settings` (but `index_granularity` and
`index_granularity_bytes`, set when the table is created), `column_codecs`, `column_ttls`, `projections`,
`lightweight_updates` — with `ALTER TABLE`. Any other change (the engine, `order_by`, `sample_by`,
`partition_by`, the distribution) remakes the table: a new table is created, the rows copied, the old one
dropped and the new renamed, the views and dictionaries over it dropped and created again around it, its
columns keeping their codecs and times to live. On a cluster a table holding rows of its own on each
server is refused there — only the rows of one server would be copied. A column of the table's keys (its
primary key, `order_by`, `sample_by`, `partition_by`, sharding key), one a projection names (or a
projection of `SELECT *`), one a dictionary reads, and any column of a table with a materialized view,
can't be renamed in place: `RenameField` of its field remakes the table the same way — the field renamed
in the dictionaries and in the materialized views' queries and keys with it — the column under its new
name, and so does an `AlterField` changing its type or nullability — the table of a relation storing the
key's values in a column of its own keys is remade likewise when the key takes a new type; a column a
data skipping index reads gets a new type or nullability with the index dropped for the change and
created again — the one index over every key column of a relation to a composite key too. `hare drift`
compares the options with the server's, as the server formats them.

When a migration changes fields too, a changed `ttl`, column codec, column TTL or projection —
expressions over columns — is removed before the fields change and set after them: a field made NOT NULL
takes its new `ttl` after its change (ClickHouse refuses a `ttl` over a `Nullable` column), one made
nullable loses its `ttl` before. `makemigrations` writes this as an `AlterModelOptions` on each side of
the field operations. A field made NOT NULL while a row holds NULL in it raises `IntegrityError` before
the column changes, as on PostgreSQL — the `MODIFY COLUMN` would fail in its mutation, which stays and
fails every later one; fill the NULLs first. A field made nullable has its column rewritten with the
change (`UPDATE column = column`): ClickHouse 25.8 reads the NULL map of the parts stored before as every
value NULL, and `filter(title__isnull=True)` would match all of them.

## <a id="lightweight-updates"></a>Lightweight updates

`ClickhouseTableOptions(lightweight_updates=True)` (ClickHouse 25.7) sets the table settings
`enable_block_number_column` and `enable_block_offset_column`; `save()`, `update()` and `bulk_update()`
of such a table write `UPDATE t SET ... WHERE ...`. The new values are written beside the row and read in
place of the old ones at once — a mutation rewrites each part holding a row instead. A key column isn't
updated either way. A migration turns the option on or off with `MODIFY SETTING`/`RESET SETTING`, without
remaking the table.

ClickHouse doesn't update a column its keys (`order_by`, `sample_by`, `partition_by`, sharding key) or
its engine's arguments (a version, a sign) read. `save()` leaves such a column as it is — it reads the
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
`hare drift` read the type and its arguments back, and an expression key
(`BloomFilterIndex(Lower("title"), name=...)`) as its SQL. A `MinMaxIndex` of granularity 1 is the same
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
