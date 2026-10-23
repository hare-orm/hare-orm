# ClickHouse: aggregates

`hare.dialects.clickhouse.functions.aggregates` has ClickHouse's own aggregates. They run in
`annotate()`, `aggregate()` and `Window(...)` like hare's, and take `_filter=` and `distinct=True` as any
aggregate does.

```python
from hare.dialects.clickhouse.functions.aggregates import ArgMax, Quantile, TopK, Uniq

await PageView.objects.values("day").annotate(visitors=Uniq("visitor_id"))
await Quote.objects.values("symbol").annotate(last_price=ArgMax("price", "quoted_at"))
await PageView.objects.aggregate(typical=Quantile("load_ms", 0.5), slow=Quantile("load_ms", 0.99, exact=True))
await PageView.objects.aggregate(popular=TopK("path", 5))
```

## <a id="aggregates"></a>The aggregates

| Aggregate | SQL | Result |
|---|---|---|
| `Uniq(field)` | `uniq` | About how many distinct values a group has — an estimate from a sample of hashes, the fastest |
| `UniqExact(field)` | `uniqExact` | How many distinct values, exactly — every one kept in memory |
| `UniqCombined(field)` | `uniqCombined` | An estimate in less memory and closer than `Uniq` for many values |
| `Quantile(field, level=0.5, *, exact=False)` | `quantile(level)`, `quantileExact(level)` | The value below which `level` of the values lie — an interpolated estimate, or with `exact=True` one of the values |
| `Quantiles(field, *levels, exact=False)` | `quantiles(...)`, `quantilesExact(...)` | Several quantiles in one pass, an array in the order of the levels |
| `Median(field, *, exact=False)` | `quantile(0.5)` | The median — an estimate, or the middle value with `exact=True` |
| `ArgMin(field, by)`, `ArgMax(field, by)` | `argMin`, `argMax` | The value of `field` in the row of the group where `by` is the least or the greatest |
| `GroupArray(field, *, max_size=None)` | `groupArray` | The values as an array in the order read, `NULL` left out; `max_size` keeps the first that many |
| `GroupUniqArray(field, *, max_size=None)` | `groupUniqArray` | The distinct values as an array, in no order; `max_size` keeps that many of them |
| `TopK(field, k=10)` | `topK(k)` | About the `k` most frequent values, the most frequent first — exact only for few distinct values |
| `AnyValue(field)`, `AnyLast(field)` | `any`, `anyLast` | The first or the last value ClickHouse meets in the group, whichever it is |
| `SumMap(field)` | `sumMap` | The maps of a `MapField` added up key by key |

`Quantiles`, `GroupArray`, `GroupUniqArray` and `TopK` give a list; `SumMap` a dict. `Uniq`, `Quantile`
and `TopK` are estimates and may differ between runs over many rows — `UniqExact` and `exact=True` are
deterministic.

## <a id="filter-distinct-and-empty-groups"></a>`_filter`, `distinct` and empty groups

- `_filter=Q(...)` is written `FILTER (WHERE ...)` and `distinct=True` as `DISTINCT` inside the call, as
  on other databases — ClickHouse runs them as its `-If` and `-Distinct` combinators.
- Over no rows `Max`, `Min`, `Avg`, `StdDev` and `Variance` give `None`, as do the aggregates below:
  each is written by its `-OrNull` form (`maxOrNull(amount)`) — the list is in [`NULL` from an aggregate
  over no rows](differences.md#null-from-an-aggregate). `Sum` gives ClickHouse's own `0` (`0.00` for a decimal). In a
  grouped statement, whose groups hold at least one row, an aggregate without `_filter` keeps its plain,
  cheaper form. A sample deviation or variance over one value is `None`, not ClickHouse's NaN.
- Over no rows (or only `NULL` values) `Quantile`, `Median`, `ArgMin`, `ArgMax`, `AnyValue` and `AnyLast`
  give `None`; `Uniq`/`UniqExact` give `0` (`UniqCombined` of a nullable field gives `None` — ClickHouse's
  own `uniqCombined`), `GroupArray`, `GroupUniqArray` and `TopK` an empty list, and `Quantiles` a `NaN` for
  each level — ClickHouse holds no `NULL` inside an array.
- `ArgMin`/`ArgMax` read only the rows whose `field` isn't `NULL`: the value of the row with the
  least/greatest `by` among them, not `None` when the row with the extreme `by` holds `NULL`.
- `ArrayAgg` of hare is `groupArray` of a tuple holding each value — every value is an element of
  hare's `ArrayAgg`, `NULL` too, which a plain `groupArray` leaves out; with `distinct=True` it is
  `groupUniqArray`, and its `order_by` sorts the array — a `NULL` ordering value where hare's ordering
  puts it (last going up, first going down), all orderings going one way (up or down; both raise
  `UnSupportedError`).
