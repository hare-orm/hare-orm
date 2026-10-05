# ClickHouse: агрегаты

В `hare.dialects.clickhouse.functions.aggregates` — собственные агрегаты ClickHouse. Они работают в
`annotate()`, `aggregate()` и `Window(...)`, как агрегаты hare, и принимают `_filter=` и `distinct=True`,
как любой агрегат.

```python
from hare.dialects.clickhouse.functions.aggregates import ArgMax, Quantile, TopK, Uniq

await PageView.objects.values("day").annotate(visitors=Uniq("visitor_id"))
await Quote.objects.values("symbol").annotate(last_price=ArgMax("price", "quoted_at"))
await PageView.objects.aggregate(typical=Quantile("load_ms", 0.5), slow=Quantile("load_ms", 0.99, exact=True))
await PageView.objects.aggregate(popular=TopK("path", 5))
```

## <a id="aggregates"></a>Агрегаты

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

`Quantiles`, `GroupArray`, `GroupUniqArray` и `TopK` дают список, `SumMap` — словарь. `Uniq`, `Quantile`
и `TopK` — оценки и на многих строках могут отличаться от запуска к запуску; `UniqExact` и `exact=True`
детерминированы.

## <a id="filter-distinct-and-empty-groups"></a>`_filter`, `distinct` и пустые группы

- `_filter=Q(...)` пишется как `FILTER (WHERE ...)`, а `distinct=True` — как `DISTINCT` внутри вызова,
  как в других базах; ClickHouse выполняет их как свои комбинаторы `-If` и `-Distinct`.
- На пустом наборе `Max`, `Min`, `Avg`, `StdDev` и `Variance` дают `None`, как и агрегаты ниже: каждый
  пишется в форме `-OrNull` (`maxOrNull(amount)`) — список см. в [`NULL` от агрегата по пустому
  набору](differences.ru.md#null-from-an-aggregate). `Sum` даёт собственный `0` ClickHouse (`0.00` для десятичного). В
  группирующем запросе, где в группе хотя бы одна строка, агрегат без `_filter` остаётся в простой и более
  быстрой форме. Выборочное отклонение или дисперсия по одному значению — `None`, а не NaN ClickHouse.
- На пустом наборе (или только из `NULL`) `Quantile`, `Median`, `ArgMin`, `ArgMax`, `AnyValue` и
  `AnyLast` дают `None`; `Uniq`/`UniqExact` — `0` (`UniqCombined` от поля с `null=True` — `None`: так
  считает сам `uniqCombined` в ClickHouse), `GroupArray`, `GroupUniqArray` и `TopK` — пустой список, а
  `Quantiles` — `NaN` для каждого уровня: внутри массива ClickHouse не хранит `NULL`.
- `ArgMin`/`ArgMax` читают только строки, где `field` не `NULL`: значение строки с наименьшим или
  наибольшим `by` среди них, а не `None`, когда строка с крайним `by` хранит `NULL`.
- `ArrayAgg` из hare — это `groupArray` кортежей, каждый из которых держит значение: каждое значение —
  элемент `ArrayAgg` hare, в том числе `NULL`, который простой `groupArray` пропускает; с
  `distinct=True` это `groupUniqArray`, а его `order_by` сортирует массив — `NULL` в значении сортировки
  встаёт туда, куда его ставит сортировка hare (последним по возрастанию, первым по убыванию), а все
  сортировки идут в одну сторону (по возрастанию или по убыванию; обе сразу дают `UnSupportedError`).
