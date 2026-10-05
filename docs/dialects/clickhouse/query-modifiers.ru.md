# Модификаторы запросов ClickHouse

ClickHouse читает таблицу так, как не умеет ни одна другая база, с которой работает hare: сливает
версии строк при чтении, читает выборку строк, проверяет условие до чтения остальных столбцов,
ограничивает число строк в каждой группе. hare даёт каждую возможность методом `QuerySet` диалекта
ClickHouse. Вызов записывается в queryset, как любой другой, и попадает в запрос, когда тот строится
для своего соединения; на соединении другой базы он даёт `UnSupportedError`. Запрос с такими вызовами
хранит [план](../../querying/query-plan-cache.ru.md) — значения их условий подставляются, как значения
фильтров, — кроме запроса с `sample()`: его доля записана во `FROM`.

| Метод | SQL |
|---|---|
| [`final()`](#final) | `FROM t FINAL`; `final(all_tables=True)` — `SETTINGS final = 1` |
| [`sample(percent)`](#sample) | `SAMPLE <доля>` |
| [`sample_rows(rows)`](#sample) | `SAMPLE <строк>` |
| [`sample_offset(percent)`](#sample) | `SAMPLE ... OFFSET <доля>` |
| [`prewhere(*conditions, **filters)`](#prewhere) | `PREWHERE ...` |
| [`limit_by(limit, *expressions, offset=0)`](#limit-by) | `LIMIT n [OFFSET m] BY ...` |
| [`settings(**values)`](#settings) | `SETTINGS name = value, ...` |
| [`with_totals()`](#with-totals) | итоговая строка `GROUP BY ... WITH TOTALS` |
| [`__global_in`, `__global_not_in`](#global-in) | `GLOBAL IN`, `GLOBAL NOT IN` |
| [`AsofJoin`](../../querying/filters.ru.md#asof-join) | `ASOF LEFT JOIN` |

Заглушки `hare stubs` и [плагин mypy](../../querying/type-checking.ru.md#dialect-methods) объявляют эти
методы у queryset модели, соединение которой — ClickHouse.

## <a id="final"></a>`final()`

Движок, хранящий несколько версий строки, — `ReplacingMergeTree`, `CollapsingMergeTree`,
`VersionedCollapsingMergeTree`, `AggregatingMergeTree`, `SummingMergeTree`, `CoalescingMergeTree`,
`GraphiteMergeTree` и их формы `Replicated`/`Shared`, — сливает их в фоне, поэтому чтение может увидеть
старые версии строки рядом с последней. `final()` читает таблицу модели с `FINAL`: версии слиты так,
как их слило бы слияние частей.

```python
class Reading(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    value = fields.IntField()
    version = fields.IntField(default=0)

    class Meta:
        table_options = [ClickhouseTableOptions(engine="ReplacingMergeTree(version)")]


await Reading.objects.filter(id=1).values_list("value", flat=True)  # [1, 1000] - две версии
await Reading.objects.filter(id=1).final().values_list("value", flat=True)  # [1000]
```

- `count()`, `exists()`, `aggregate()`, `values()` и строки модели одинаково читают слитые строки.
- `final(all_tables=True)` читает с `FINAL` все таблицы запроса — и таблицы `select_related()`, и
  таблицы фильтров через связи — настройкой запроса `SETTINGS final = 1`.
- Модель, движок которой хранит одну версию строки, даёт `QueryError`: ClickHouse там `FINAL` не
  принимает.
- `UPDATE` (`update()`) или `DELETE` (`delete()`) queryset с `final()` даёт `QueryError`: мутация
  выбирает строки только по своему условию.

## <a id="sample"></a>`sample()`, `sample_rows()`, `sample_offset()`

ClickHouse читает выборку таблицы по её ключу выборки — выражению первичного ключа, от диапазона
значений которого он читает долю. Модель объявляет его в `ClickhouseTableOptions(sample_by=...)`, одним
из ключей `order_by`:

```python
class PageView(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    site = fields.CharField(max_length=50)

    class Meta:
        table_options = [
            ClickhouseTableOptions(order_by=("id", RawSQLTerm("intHash32(id)")), sample_by=RawSQLTerm("intHash32(id)"))
        ]


await PageView.objects.sample(10).count()  # около десятой части строк: SAMPLE 0.1
await PageView.objects.sample_rows(10_000)  # около 10 000 строк: SAMPLE 10000
first_half = PageView.objects.sample(50)  # SAMPLE 0.5
second_half = PageView.objects.sample(50).sample_offset(50)  # SAMPLE 0.5 OFFSET 0.5 - остальные строки
```

- `sample(percent)` — собственная [выборка](../../querying/queryset-methods.ru.md#sample) queryset: на
  ClickHouse это `percent` процентов диапазона ключа. Одна и та же доля читает одни и те же строки, пока
  таблица не меняется, — `seed` ClickHouse не принимает, поэтому `seed=` и `method="system"` дают
  `UnSupportedError`; `sample(0)` тоже: `SAMPLE 0` ClickHouse читает как всю таблицу.
- `sample_rows(rows)` читает около `rows` строк — не меньше 2: `SAMPLE 1` — это вся таблица.
- `sample_offset(percent)` читает выборку из части диапазона ключа после `percent` его процентов,
  поэтому выборки с разными смещениями читают непересекающиеся строки; без выборки даёт `QueryError`.
- Модель без `sample_by` даёт `QueryError`. Первичный ключ таблицы с `sample_by` идёт от ключа модели до
  ключа выборки: ClickHouse делает выборку по ключу из первичного ключа.
- `sample()` вместе с `sample_rows()`, а также запись queryset с выборкой дают `QueryError`.

## <a id="prewhere"></a>`prewhere()`

`prewhere(*conditions, **filters)` принимает аргументы `filter()` и пишет их в `PREWHERE`: условие
читается первым, и у строк, которые оно отбросило, остальные столбцы не читаются. ClickHouse и сам
переносит часть `WHERE` туда; `prewhere()` говорит, какую.

```python
await PageView.objects.prewhere(site="docs").filter(duration_ms__gt=1000).count()
# SELECT count(*) FROM "page_view" PREWHERE "site"='docs' WHERE "duration_ms">1000
await PageView.objects.prewhere(Q(site="docs") | Q(site="blog"), id__gt=100)
```

- Условие читает столбцы собственной таблицы модели: поле через связь или агрегат дают `QueryError`.
  Несколько вызовов объединяются через `AND`.
- С `final()` `PREWHERE` читается до слияния версий: условие по столбцу, который меняет более поздняя
  версия, может не пустить в слияние строку старой версии.
- Запись queryset с `prewhere()` даёт `QueryError`.

## <a id="limit-by"></a>`limit_by()`

`limit_by(limit, *expressions, offset=0)` оставляет не больше `limit` строк каждой группы значений
выражений, пропустив сначала `offset` из них, в порядке queryset, — два последних просмотра каждого
сайта:

```python
latest = PageView.objects.order_by("site", "-viewed_at").limit_by(2, "site")
await latest.values_list("site", "viewed_at")
# SELECT ... ORDER BY "site" ASC, "viewed_at" DESC LIMIT 2 BY "site"
await latest[:10]  # LIMIT 2 BY "site" LIMIT 10
await PageView.objects.order_by("id").limit_by(1, F("duration_ms") % 2, offset=1)
```

- Выражения — имена полей (и через связи), имена вычисляемых значений или выражения.
- `count()` и `exists()` считают оставленные строки; `aggregate()` и запись дают `QueryError`.

## <a id="settings"></a>`settings()`

`settings(**values)` задаёт настройки ClickHouse только для этого запроса — поверх настроек соединения —
в его клаузе `SETTINGS`. Следующий вызов дополняет предыдущий, его значения побеждают.

```python
await PageView.objects.settings(max_threads=4, max_execution_time=30).count()
await PageView.objects.filter(site="docs").settings(mutations_sync=2).update(duration_ms=0)
await PageView.objects.filter(id__global_in=...).settings(distributed_product_mode="global")
```

- Значение — `bool` (пишется `1`/`0`), `int` из 64-битного диапазона ClickHouse, конечный `float` или
  `str`; имя — идентификатор. Всё остальное даёт `QueryError`.
- Их принимают чтения, `update()` (`ALTER TABLE ... UPDATE ... SETTINGS`) и `delete()`
  (`DELETE ... SETTINGS`).
- Настройка, которую профиль сервера менять не разрешает, даёт ошибку сервера — `OperationalError`.

## <a id="with-totals"></a>`with_totals()`

`with_totals()` добавляет к строкам сгруппированного запроса `values()` строку его агрегатов по всем
группам — `GROUP BY ... WITH TOTALS`:

```python
rows = await (
    PageView.objects.values("site").annotate(views=Count("id"), time=Sum("duration_ms")).order_by("-views")
)[:3].with_totals()
rows  # [{"site": "docs", "views": 40, "time": 52000}, ...] - три сайта
rows.totals  # {"site": None, "views": 100, "time": 130000} - все сайты
```

- Запрос возвращает `TotalsResult` (`hare.dialects.clickhouse.query.totals_result`): список своих строк
  и `.totals` — строку своих агрегатов по всем строкам, подходящим под его условия, до `ORDER BY`,
  `limit_by()` и среза, прочитанную так же, как его строки; сгруппированные поля в ней — `None`. Фильтр
  по агрегату (`HAVING`) оставляет там те же группы. По пустому набору строк `Count` — `0`, остальные
  агрегаты — `None`, как в `aggregate()`.
- Ни один драйвер ClickHouse не возвращает строку, которую добавляет `WITH TOTALS`, поэтому она
  читается вторым запросом тех же строк сразу после первого.
- Для запроса, который не является сгруппированным запросом `values()`, даёт `QueryError`.

## <a id="global-in"></a>`__global_in`, `__global_not_in`

`__global_in` и `__global_not_in` — это `__in` и `__not_in`, записанные как `GLOBAL IN`: ClickHouse
читает список или подзапрос один раз, на сервере, куда пришёл запрос, и рассылает результат каждому
шарду таблицы `Distributed`, — с обычным `IN` каждый шард выполняет подзапрос сам.

```python
active = Session.objects.filter(active=True).values("user_id")
await PageView.objects.filter(user_id__global_in=active).count()
await PageView.objects.filter(team__global_not_in=[team_a, team_b])
```

Они принимают то же, что `__in`, — список или queryset — у поля из одного столбца и так же обращаются
с `None` и пустым списком. На другой базе фильтр с ними даёт `UnSupportedError`.
