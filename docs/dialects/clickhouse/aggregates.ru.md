# Агрегаты ClickHouse

В `hare.dialects.clickhouse.functions.aggregates` — собственные агрегаты ClickHouse. Они работают в
`annotate()`, `aggregate()` и `Window(...)`, как агрегаты hare, и принимают `_filter=` и
`distinct=True`, как любой агрегат.

```python
from hare.dialects.clickhouse.functions.aggregates import ArgMax, Quantile, TopK, Uniq

await Event.objects.values("day").annotate(visitors=Uniq("user_id"))
await Quote.objects.values("symbol").annotate(last_price=ArgMax("price", "quoted_at"))
await Order.objects.aggregate(typical=Quantile("total", 0.5), high=Quantile("total", 0.99, exact=True))
await Event.objects.aggregate(popular=TopK("page", 5))
```

| Агрегат | SQL | Результат |
|---|---|---|
| `Uniq(field)` | `uniq` | Примерно сколько различных значений в группе — оценка по выборке хешей, самая быстрая |
| `UniqExact(field)` | `uniqExact` | Сколько различных значений, точно — все хранятся в памяти |
| `UniqCombined(field)` | `uniqCombined` | Оценка в меньшей памяти и точнее `Uniq` при многих значениях |
| `Quantile(field, level=0.5, *, exact=False)` | `quantile(level)`, `quantileExact(level)` | Значение, ниже которого лежит доля `level` значений, — интерполированная оценка или, с `exact=True`, одно из значений |
| `Quantiles(field, *levels, exact=False)` | `quantiles(...)`, `quantilesExact(...)` | Несколько квантилей за один проход — массив в порядке уровней |
| `Median(field, *, exact=False)` | `quantile(0.5)` | Медиана — оценка или, с `exact=True`, среднее значение |
| `ArgMin(field, by)`, `ArgMax(field, by)` | `argMin`, `argMax` | Значение `field` в строке группы, где `by` наименьшее или наибольшее |
| `GroupArray(field, *, max_size=None)` | `groupArray` | Значения массивом в порядке чтения, без `NULL`; `max_size` оставляет столько первых |
| `GroupUniqArray(field, *, max_size=None)` | `groupUniqArray` | Различные значения массивом, без порядка; `max_size` оставляет не больше стольких |
| `TopK(field, k=10)` | `topK(k)` | Примерно `k` самых частых значений, самое частое первым, — точно только при немногих различных значениях |
| `AnyValue(field)`, `AnyLast(field)` | `any`, `anyLast` | Первое или последнее значение группы, какое встретит ClickHouse |
| `SumMap(field)` | `sumMap` | Словари `MapField`, сложенные по ключам |

`Quantiles`, `GroupArray`, `GroupUniqArray` и `TopK` дают список, `SumMap` — словарь. `Uniq`,
`Quantile` и `TopK` — оценки и на многих строках могут отличаться от запуска к запуску; `UniqExact` и
`exact=True` детерминированы.

## <a id="filter-distinct-and-empty-groups"></a>`_filter`, `distinct` и пустые группы

- `_filter=Q(...)` пишется как `FILTER (WHERE ...)`, а `distinct=True` — как `DISTINCT` внутри вызова,
  как в других базах; ClickHouse выполняет их как свои комбинаторы `-If` и `-Distinct`.
- На пустом наборе `Sum`, `Max`, `Min`, `Avg`, `StdDev` и `Variance` дают `None`, как в SQL: они
  пишутся в форме `-OrNull` (`sumOrNull(amount)`). В группирующем запросе, где в группе хотя бы одна
  строка, агрегат без `_filter` остаётся в простой и более быстрой форме. Выборочное отклонение или
  дисперсия по одному значению — `None`, а не NaN ClickHouse.
- `ArrayAgg` из hare — это `groupArray` кортежей, каждый из которых держит значение: так сохраняются
  значения `NULL`, которые простой `groupArray` пропускает, как `ARRAY_AGG` в PostgreSQL; с
  `distinct=True` это `groupUniqArray`, а его `order_by` сортирует массив.
