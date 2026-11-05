# ClickHouse: модификаторы запросов

ClickHouse читает таблицу так, как не умеет ни одна другая база, с которой работает hare: сливает версии
строк при чтении, читает выборку строк, проверяет условие до чтения остальных колонок, ограничивает число
строк в каждой группе. hare даёт каждую возможность методом `QuerySet` диалекта ClickHouse. Вызов
записывается в queryset, как любой другой, и попадает в запрос, когда тот строится для своего соединения;
на соединении другой базы он даёт `UnSupportedError`. Запрос с такими вызовами хранит
[план](../../querying/query-plan-cache.ru.md) — значения их условий подставляются, как значения фильтров,
— кроме запроса с `sample()`: его доля записана во `FROM`.

| Метод | SQL |
|---|---|
| [`final()`](#final) | `FROM t FINAL`; `final(all_tables=True)` — `SETTINGS final = 1` |
| [`sample(percent)`](#sample) | `SAMPLE <share>` |
| [`sample_rows(rows)`](#sample) | `SAMPLE <rows>` |
| [`sample_offset(percent)`](#sample) | `SAMPLE ... OFFSET <share>` |
| [`prewhere(*conditions, **filters)`](#prewhere) | `PREWHERE ...` |
| [`limit_by(limit, *expressions, offset=0)`](#limit-by) | `LIMIT n [OFFSET m] BY ...` |
| [`settings(**values)`](#settings) | `SETTINGS name = value, ...` |
| [`with_totals()`](#with-totals) | итоговая строка `GROUP BY ... WITH TOTALS` |
| [`__global_in`, `__global_not_in`](#global-in) | `GLOBAL IN`, `GLOBAL NOT IN` |
| [`AsofJoin`](../../querying/filters.ru.md#asof-join) | `ASOF LEFT JOIN` |
| [`ArrayJoin`](../../querying/filters.ru.md#array-join) | `ARRAY JOIN` |

`hare stubs` объявляет эти методы в сгенерированном queryset каждой модели ClickHouse.
[Плагин mypy](../../querying/type-checking.ru.md#dialect-methods) добавляет их в общий тип `QuerySet`,
если ClickHouse есть в конфигурации, поэтому принимает такие вызовы и у моделей другого диалекта.
Для выполнения запроса по-прежнему требуется соединение ClickHouse.

## <a id="final"></a>`final()`

Движок, хранящий несколько версий строки, — `ReplacingMergeTree`, `CollapsingMergeTree`,
`VersionedCollapsingMergeTree`, `AggregatingMergeTree`, `SummingMergeTree`, `CoalescingMergeTree`,
`GraphiteMergeTree` и их формы `Replicated`/`Shared`, — сливает их в фоне, поэтому чтение может увидеть
старые версии строки рядом с последней. `final()` читает таблицу модели с `FINAL`: версии слиты так, как
их слило бы слияние частей.

```python
from hare import Model, fields
from hare.ddl import RawSQLTerm
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.clickhouse_engine import ClickhouseEngine
from hare.query.expressions import F, Q
from hare.query.functions import Count, Sum


class Reading(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    value = fields.IntField()
    version = fields.IntField(default=0)

    class Meta:
        table_options = [ClickhouseTableOptions(engine=ClickhouseEngine("ReplacingMergeTree", F("version")))]


await Reading.objects.filter(id=1).values_list("value", flat=True)  # [1, 1000] - две версии
await Reading.objects.filter(id=1).final().values_list("value", flat=True)  # [1000]
```

- `count()`, `exists()`, `aggregate()`, `values()` и строки модели одинаково читают слитые строки.
- Собственные чтения строки по ключу, которые делает hare, читают слитую версию такой таблицы и без
  `final()`: `refresh_from_db()`, поиск в `get_or_create()`/`update_or_create()` и проверка `save()`
  того, что колонки ключа не изменились. Обычный `get()` читает то же, что и запрос.
- `final(all_tables=True)` читает с `FINAL` все таблицы запроса — и таблицы `select_related()`, и таблицы
  фильтров через связи — настройкой запроса `SETTINGS final = 1`.
- Модель, движок которой хранит одну версию строки, даёт `QueryError`: ClickHouse там `FINAL` не
  принимает.
- `UPDATE` (`update()`) или `DELETE` (`delete()`) queryset с `final()` даёт `QueryError`: мутация
  выбирает строки только по своему условию.

## <a id="sample"></a>`sample()`, `sample_rows()`, `sample_offset()`

ClickHouse читает выборку таблицы по её ключу выборки — выражению первичного ключа, от диапазона значений
которого он читает долю. Модель объявляет его в `ClickhouseTableOptions(sample_by=...)`, одним из ключей
`order_by`:

```python
class PageView(Model):
    id = fields.BigIntField(primary_key=True, generated=False)
    site = fields.CharField(max_length=50)
    duration_ms = fields.IntField()
    viewed_at = fields.DatetimeField()

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
- `sample_offset(percent)` начинает выборку с `percent` процентов диапазона ключа. Строки выборок
  не пересекаются, только если их интервалы не перекрываются; без выборки даёт `QueryError`.
- Модель без `sample_by` даёт `QueryError`. Первичный ключ таблицы с `sample_by` идёт от ключа модели до
  ключа выборки: ClickHouse делает выборку по ключу из первичного ключа.
- `sample()` вместе с `sample_rows()`, а также запись queryset с выборкой дают `QueryError`.

## <a id="prewhere"></a>`prewhere()`

`prewhere(*conditions, **filters)` принимает аргументы `filter()` и пишет их в `PREWHERE`: условие
читается первым, и у строк, которые оно отбросило, остальные колонки не читаются. ClickHouse и сам
переносит часть `WHERE` туда; `prewhere()` говорит, какую.

```python
await PageView.objects.prewhere(site="docs").filter(duration_ms__gt=1000).count()
# SELECT COUNT(*) FROM "pageview" PREWHERE "site"=$1 WHERE "duration_ms">$2
await PageView.objects.prewhere(Q(site="docs") | Q(site="blog"), id__gt=100)
```

- Условие читает колонки собственной таблицы модели: поле через связь или агрегат дают `QueryError`.
  Несколько вызовов объединяются через `AND`.
- С `final()` `PREWHERE` читается до слияния версий: условие по колонке, которую меняет более поздняя
  версия, может не пустить в слияние строку старой версии.
- Запись queryset с `prewhere()` даёт `QueryError`.

## <a id="limit-by"></a>`limit_by()`

`limit_by(limit, *expressions, offset=0)` оставляет не больше `limit` строк каждой группы значений
выражений, пропустив сначала `offset` из них, в порядке queryset, — два последних просмотра каждого
сайта:

```python
latest = PageView.objects.order_by("site", "-viewed_at").limit_by(2, "site")
await latest.values_list("site", "viewed_at")
# SELECT ... ORDER BY "site" ASC,"viewed_at" DESC NULLS FIRST LIMIT 2 BY "site"
await latest[:10]  # ... LIMIT 2 BY "site" LIMIT $1 OFFSET $2
await PageView.objects.order_by("id").limit_by(1, F("duration_ms") % 2, offset=1)
```

- Выражения — имена полей (и через связи), имена вычисляемых значений или выражения; `"pk"` составного
  первичного ключа группирует по каждой его колонке.
- `count()`, `exists()` и `aggregate()` читают оставленные строки; запись даёт `QueryError`.
- Запрос, который читает свои строки в производной таблице, — с фильтром по оконной функции — даёт
  `UnSupportedError`: `LIMIT BY` оставил бы строки до того, как они пройдут фильтр. `final()`, `sample()` и
  `prewhere()` читают таблицу внутри такого запроса так же, как в любом другом.

## <a id="settings"></a>`settings()`

`settings(**values)` задаёт настройки ClickHouse только для этого запроса — поверх настроек соединения —
в его клаузе `SETTINGS`. Следующий вызов дополняет предыдущий, его значения побеждают.

```python
await PageView.objects.settings(max_threads=4, max_execution_time=30).count()
await PageView.objects.filter(site="docs").settings(mutations_sync=2).update(duration_ms=0)
docs_ids = PageView.objects.filter(site="docs").values("id")
await PageView.objects.filter(id__global_in=docs_ids).settings(distributed_product_mode="global")
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
rows.totals  # {"site": "", "views": 100, "time": 130000} - все сайты
```

- Запрос возвращает `TotalsResult` (`hare.dialects.clickhouse.query.totals_result`): список своих строк и
  `.totals` — строку итоговых агрегатов до `ORDER BY`, `limit_by()` и среза, прочитанную так же, как
  его строки. Сгруппированные поля в ней держат значение по умолчанию типа своей колонки (`""`, `0`) —
  так их пишет ClickHouse. Влияние фильтра по агрегату (`HAVING`) на итоги зависит от настройки
  [`totals_mode`](https://clickhouse.com/docs/reference/statements/select/group-by#configuring-totals-processing)
  ClickHouse; hare её не переопределяет. Результат по пустому набору строк зависит от функции агрегации.
- Строки и итоговая строка приходят из одной команды: через clickhouse-driver hare читает пакет итогов
  протокола отдельно от строк; через clickhouse-connect, в формате `Native` которого сервер итогов не
  шлёт, команда читается в `JSONCompact`.
- Для запроса, который не является сгруппированным запросом `values()`, даёт `QueryError`.

## <a id="global-in"></a>`__global_in`, `__global_not_in`

`__global_in` и `__global_not_in` — это `__in` и `__not_in`, записанные как `GLOBAL IN`: ClickHouse
читает список или подзапрос один раз, на сервере, куда пришёл запрос, и рассылает результат каждому шарду
таблицы `Distributed`, — с обычным `IN` каждый шард выполняет подзапрос сам.

```python
docs_ids = PageView.objects.filter(site="docs").values("id")
await PageView.objects.filter(id__global_in=docs_ids).count()
await PageView.objects.filter(id__global_not_in=[1, 2])
```

Они принимают то же, что `__in`, — список или queryset — у поля из одной колонки и так же обращаются с
`None` и пустым списком. На другой базе фильтр с ними даёт `UnSupportedError`.
