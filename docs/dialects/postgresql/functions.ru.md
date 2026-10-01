# Функции PostgreSQL

| Функция | SQL |
|---|---|
| `ToTsVector(field, config=None)` | `TO_TSVECTOR(...)` |
| `PlainToTsQuery(field, config=None)` | `PLAINTO_TSQUERY(...)` |
| `STDistance(field, point)` | `ST_DISTANCE(...)` |
| `STDWithin(field, point, radius_m)` | `ST_DWITHIN(...)` |
| `ArrayAgg(field, distinct=False, _filter=None, order_by=())` | `ARRAY_AGG(...)` — каждый элемент раскодируется через поле |
| `StringAgg(field, delimiter, distinct=False, _filter=None, order_by=())` | `STRING_AGG(CAST(field AS TEXT), ...)` |
| `JSONBAgg(field, distinct=False, _filter=None, order_by=())` | `JSONB_AGG(...)` — каждый элемент раскодируется через поле |
| `BoolAnd(field)` / `BoolOr(field)` | `BOOL_AND`/`BOOL_OR` |
| `BitAnd(field)` / `BitOr(field)` / `BitXor(field)` | `BIT_AND`/`BIT_OR`/`BIT_XOR` целочисленной колонки |
| `Corr(y, x)` | `CORR(y, x)` — коэффициент корреляции |
| `CovarPop(y, x, sample=False)` | `COVAR_POP`, или `COVAR_SAMP` при `sample=True` |
| `RegrAvgX(y, x)` / `RegrAvgY(y, x)` | `REGR_AVGX`/`REGR_AVGY` — среднее `x`/`y` |
| `RegrCount(y, x)` | `REGR_COUNT` — строки, где оба значения не `NULL` (целое число) |
| `RegrIntercept(y, x)` / `RegrSlope(y, x)` / `RegrR2(y, x)` | свободный член, наклон и R² прямой по методу наименьших квадратов |
| `RegrSXX(y, x)` / `RegrSXY(y, x)` / `RegrSYY(y, x)` | `REGR_SXX`/`REGR_SXY`/`REGR_SYY` |
| `TransactionNow()` | `CURRENT_TIMESTAMP` — начало транзакции, одинаковое для всех её команд |
| `RandomUUID()` | `gen_random_uuid()` — случайный UUID версии 4 для каждой строки |
| `ArrayItem(field, index)` | `field[index + 1]` (позиция с 0) — раскодируется через `base_field` массива; у вложенного массива — целая строка (`[[1, 2], [3, 4]]` → `[1, 2]`) |

```python
await Store.objects.annotate(distance_m=STDistance("location", (55.75, 37.62))).order_by("distance_m")

await Article.objects.all().annotate(tag_names=ArrayAgg("tags__name")).group_by("id")

await Article.objects.annotate(first_tag=ArrayItem("tags", 0)).values("first_tag")
```

`TruncYear`/`TruncMonth`/`TruncDay` и `ExtractYear`/`ExtractMonth`/`ExtractDay` — это функции даты
из `hare.query.functions`, которые можно импортировать и отсюда; они работают на любой базе — см.
[Функции даты](../../querying/functions.ru.md#date-functions).

`ArrayAgg`/`JSONBAgg` раскодируют каждый элемент через агрегируемое поле (элемент `JSONField`
разбирается, элемент `DatetimeField` — в поясе Hare, элемент `TimeDeltaField` — `timedelta`).
`JSONBAgg` от `DecimalField` даёт `Decimal`, округлённый до `decimal_places` поля: сам JSON хранит число
с точностью числа с плавающей точкой, поэтому, если важны точные цифры, используйте `ArrayAgg`.
`JSONBAgg` от поля диапазона даёт значения `Range` (включая пустой диапазон, сторону без границы и
включённые/невключённые границы).

`_filter=Q(...)` не пускает в агрегат строку, которая ему не соответствует (`FILTER (WHERE ...)`), —
`ArrayAgg`/`JSONBAgg` собирают только подходящие строки, а группа без них даёт `None`.

Вычисляемое значение `ArrayAgg` фильтруется операторами `ArrayField` — `contains`, `contained_by`,
`overlap`, `len`, равенство, — а значение `JSONBAgg` — операторами `JSONField`:

```python
await Author.objects.annotate(titles=ArrayAgg("books__title")).filter(titles__overlap=["War and Peace"])
await Author.objects.annotate(titles=ArrayAgg("books__title")).filter(titles__len=2)
```

Такой оператор у вычисляемого значения, которое не является массивом, диапазоном или значением JSON,
даёт `FieldError`.

`order_by=` задаёт порядок строк, которые читают `ArrayAgg`/`StringAgg`/`JSONBAgg`: имя поля (`"-name"` —
по убыванию), `F("name")` или `F("name").desc(nulls_last=True)`, либо список таких значений, в том числе
через связь. Любой другой агрегат даёт для него `QueryError` — его результат от порядка не зависит.

```python
await Author.objects.annotate(titles=ArrayAgg("books__title", order_by="-books__published")).group_by("id")
await Tag.objects.all().aggregate(names=StringAgg("name", ", ", order_by=["name", "id"]))
```

Двухколоночные статистики принимают зависимое значение первым (`y`), как PostgreSQL, и не учитывают
строку, где любое из значений `NULL`; они возвращают `float`, а `RegrCount` — целое число.
