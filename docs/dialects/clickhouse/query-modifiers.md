# ClickHouse query modifiers

ClickHouse reads a table in ways no other database hare runs on has: a table's row versions merged
on read, a sample of its rows, a condition read before the other columns, a limit of rows per group.
hare gives each one as a `QuerySet` method of the ClickHouse dialect. A call is recorded on the
queryset like any other and written into the query when it is built for its connection; on a
connection of another database it raises `UnSupportedError`. A query with these calls keeps its
[plan](../../querying/query-plan-cache.md) — the values of their conditions bound like a filter's —
except a `sample()` one, whose share is written into `FROM`.

| Method | SQL |
|---|---|
| [`final()`](#final) | `FROM t FINAL`; `final(all_tables=True)` — `SETTINGS final = 1` |
| [`sample(percent)`](#sample) | `SAMPLE <share>` |
| [`sample_rows(rows)`](#sample) | `SAMPLE <rows>` |
| [`sample_offset(percent)`](#sample) | `SAMPLE ... OFFSET <share>` |
| [`prewhere(*conditions, **filters)`](#prewhere) | `PREWHERE ...` |
| [`limit_by(limit, *expressions, offset=0)`](#limit-by) | `LIMIT n [OFFSET m] BY ...` |
| [`settings(**values)`](#settings) | `SETTINGS name = value, ...` |
| [`with_totals()`](#with-totals) | the totals row of `GROUP BY ... WITH TOTALS` |
| [`__global_in`, `__global_not_in`](#global-in) | `GLOBAL IN`, `GLOBAL NOT IN` |
| [`AsofJoin`](../../querying/filters.md#asof-join) | `ASOF LEFT JOIN` |

The `hare stubs` stubs and the [mypy plugin](../../querying/type-checking.md#dialect-methods) declare
these methods on the queryset of a model whose connection is a ClickHouse one.

## <a id="final"></a>`final()`

An engine keeping several versions of a row — `ReplacingMergeTree`, `CollapsingMergeTree`,
`VersionedCollapsingMergeTree`, `AggregatingMergeTree`, `SummingMergeTree`, `CoalescingMergeTree`,
`GraphiteMergeTree`, and their `Replicated`/`Shared` forms — merges them in the background, so a read
can see a row's old versions next to its last one. `final()` reads the model's table with `FINAL`: its
versions merged as a merge would.

```python
class Reading(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    value = fields.IntField()
    version = fields.IntField(default=0)

    class Meta:
        table_options = [ClickhouseTableOptions(engine="ReplacingMergeTree(version)")]


await Reading.objects.filter(id=1).values_list("value", flat=True)  # [1, 1000] - two versions
await Reading.objects.filter(id=1).final().values_list("value", flat=True)  # [1000]
```

- `count()`, `exists()`, `aggregate()`, `values()` and model rows read the merged rows alike.
- `final(all_tables=True)` reads every table of the query with `FINAL` — the tables of
  `select_related()` and of filters across relations too — by the query's `SETTINGS final = 1`.
- A model whose engine keeps one version of a row raises `QueryError`: ClickHouse refuses `FINAL`
  there.
- An `UPDATE` (`update()`) or `DELETE` (`delete()`) of a queryset with `final()` raises `QueryError`
  — a mutation matches its rows by its condition alone.

## <a id="sample"></a>`sample()`, `sample_rows()`, `sample_offset()`

ClickHouse reads a sample of a table by its sample key — an expression of its primary key it reads a
share of the range of. A model declares it with `ClickhouseTableOptions(sample_by=...)`, one of the
keys of `order_by`:

```python
class PageView(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    site = fields.CharField(max_length=50)

    class Meta:
        table_options = [
            ClickhouseTableOptions(order_by=("id", RawSQLTerm("intHash32(id)")), sample_by=RawSQLTerm("intHash32(id)"))
        ]


await PageView.objects.sample(10).count()  # about a tenth of the rows: SAMPLE 0.1
await PageView.objects.sample_rows(10_000)  # about 10 000 rows: SAMPLE 10000
first_half = PageView.objects.sample(50)  # SAMPLE 0.5
second_half = PageView.objects.sample(50).sample_offset(50)  # SAMPLE 0.5 OFFSET 0.5 - the other rows
```

- `sample(percent)` is the queryset's own [sample](../../querying/queryset-methods.md#sample): on ClickHouse the
  share of the key range is `percent` percent of it. The same share reads the same rows while the
  table doesn't change — ClickHouse takes no seed, so `seed=` and `method="system"` raise
  `UnSupportedError`; `sample(0)` raises it too, as ClickHouse reads a `SAMPLE 0` as the whole table.
- `sample_rows(rows)` reads about `rows` rows — at least 2: `SAMPLE 1` is the whole table.
- `sample_offset(percent)` reads the sample from the part of the key range after `percent` of it, so
  samples with offsets read disjoint rows; without a sample it raises `QueryError`.
- A model declaring no `sample_by` raises `QueryError`. The primary key of a table with `sample_by`
  runs from the model's key through the sample key: ClickHouse samples by a key of the primary key.
- `sample()` and `sample_rows()` together, and a write of a sampled queryset, raise `QueryError`.

## <a id="prewhere"></a>`prewhere()`

`prewhere(*conditions, **filters)` takes `filter()`'s arguments and writes them into `PREWHERE`: the
condition is read first, and the rows it drops have no other column read. ClickHouse moves some of
`WHERE` there by itself; `prewhere()` says which.

```python
await PageView.objects.prewhere(site="docs").filter(duration_ms__gt=1000).count()
# SELECT count(*) FROM "page_view" PREWHERE "site"='docs' WHERE "duration_ms">1000
await PageView.objects.prewhere(Q(site="docs") | Q(site="blog"), id__gt=100)
```

- The condition reads the columns of the model's own table: a field across a relation or an aggregate
  raises `QueryError`. Several calls are joined with `AND`.
- With `final()`, `PREWHERE` is read before the versions are merged — a condition on a column a
  later version changes can keep an older version's row out of the merge.
- A write of a queryset with `prewhere()` raises `QueryError`.

## <a id="limit-by"></a>`limit_by()`

`limit_by(limit, *expressions, offset=0)` keeps at most `limit` rows of each group of the
expressions' values, after skipping `offset` of them, in the queryset's order — the two latest views of
each site:

```python
latest = PageView.objects.order_by("site", "-viewed_at").limit_by(2, "site")
await latest.values_list("site", "viewed_at")
# SELECT ... ORDER BY "site" ASC, "viewed_at" DESC LIMIT 2 BY "site"
await latest[:10]  # LIMIT 2 BY "site" LIMIT 10
await PageView.objects.order_by("id").limit_by(1, F("duration_ms") % 2, offset=1)
```

- The expressions are field names — across relations too —, annotation names or expressions.
- `count()` and `exists()` count the rows kept; `aggregate()` and writes raise `QueryError`.

## <a id="settings"></a>`settings()`

`settings(**values)` gives the statement ClickHouse's settings for this query alone — over the
connection's own — in its `SETTINGS` clause. A later call adds to an earlier one, its values winning.

```python
await PageView.objects.settings(max_threads=4, max_execution_time=30).count()
await PageView.objects.filter(site="docs").settings(mutations_sync=2).update(duration_ms=0)
await PageView.objects.filter(id__global_in=...).settings(distributed_product_mode="global")
```

- A value is a `bool` (written `1`/`0`), an `int` of ClickHouse's 64-bit range, a finite `float` or a
  `str`; a name is an identifier — anything else raises `QueryError`.
- Reads, `update()` (`ALTER TABLE ... UPDATE ... SETTINGS`) and `delete()` (`DELETE ... SETTINGS`)
  take them.
- A setting the server's profile doesn't allow to change raises the server's error,
  `OperationalError`.

## <a id="with-totals"></a>`with_totals()`

`with_totals()` adds to the rows of a grouped `values()` query the row of its aggregates over every
group — `GROUP BY ... WITH TOTALS`:

```python
rows = await (
    PageView.objects.values("site").annotate(views=Count("id"), time=Sum("duration_ms")).order_by("-views")
)[:3].with_totals()
rows  # [{"site": "docs", "views": 40, "time": 52000}, ...] - three sites
rows.totals  # {"site": None, "views": 100, "time": 130000} - every site
```

- The query returns a `TotalsResult` (`hare.dialects.clickhouse.query.totals_result`): the list of
  its rows, and `.totals` — the row of its aggregates over all the rows its conditions match, before
  `ORDER BY`, `limit_by()` and the slice, read as its rows are; its grouped fields are `None`. A filter
  on an aggregate (`HAVING`) keeps the groups it keeps there too. Over no rows `Count` is `0` and the
  other aggregates `None`, as `aggregate()` gives.
- Neither ClickHouse driver returns the row `WITH TOTALS` adds, so it is read by a second query of the
  same rows, run right after the first.
- On a query that isn't a grouped `values()` query it raises `QueryError`.

## <a id="global-in"></a>`__global_in`, `__global_not_in`

`__global_in` and `__global_not_in` are `__in` and `__not_in` written `GLOBAL IN`: ClickHouse reads
the list or subquery once, on the server the query was sent to, and sends the result to every shard of
a `Distributed` table — with a plain `IN` each shard runs the subquery itself.

```python
active = Session.objects.filter(active=True).values("user_id")
await PageView.objects.filter(user_id__global_in=active).count()
await PageView.objects.filter(team__global_not_in=[team_a, team_b])
```

They take what `__in` takes — a list or a queryset — on a field of one column, and treat `None` and an
empty list as `__in` does. On another database a filter with them raises `UnSupportedError`.
