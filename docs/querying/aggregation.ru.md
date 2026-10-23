# Агрегаты, группировка и оконные функции

Как агрегат в `annotate()` группирует строки; когда агрегат рядом со связью «ко многим» посчитал бы
строку дважды (тогда hare вместо завышенного числа даёт `QueryError`); наборы группировок с
промежуточными итогами; оконные функции и их рамки. Сами агрегаты описаны в разделе
[Функции базы данных](functions.ru.md#aggregates).

## <a id="implicit-group-by"></a>Неявный `GROUP BY`

Неявный `GROUP BY` у агрегата в `annotate()` следует таким правилам:

- Выражение, где-либо содержащее агрегат, — `Count("books") + F("id")`,
  `Coalesce(Sum("books__pages"), F("fallback"))`, `F("book_count") * F("price")` над агрегатным
  вычисляемым значением, `Case` с агрегатом в одной из веток, — считается агрегатным: его фильтры идут в
  `HAVING`, а запрос группируется по колонкам, которые оно читает вне агрегата.
- Выбранное не агрегатное выражение группируется по своему номеру в `SELECT` (`GROUP BY 2`), поэтому
  константа внутри него (`F("price") + 5`, `Case(When(..., then=Value("high")))`, `Concat(...)`)
  передаётся один раз.
- Колонка, которую `HAVING` читает вне своих агрегатов (`Q(book_count__gte=2) | Q(city="Oslo")`,
  `book_count__gt=F("limit")`), тоже попадает в группировку.
- Связанный с внешним запросом `Exists(...)`/`Subquery(...)` рядом с агрегатом группируется целиком, по
  своему номеру в `SELECT`, а не по внешним колонкам, которые читает его `OuterReference(...)`: две
  внешние строки с разными значениями `OuterReference`, но одинаковым результатом подзапроса попадают в
  одну группу. Только подзапрос, читающий внешний агрегат, группируется по этим колонкам.
- Оконная функция (`Window(...)`) никогда не бывает ключом группировки — она вычисляется для каждой
  группы; колонки, которые она читает вне своих агрегатов (аргумент, `partition_by`, `order_by`), тоже
  попадают в группировку.
- Запрос объектов модели (и его `count()`/`exists()`/`update()`/`delete()`) всегда группируется ещё и по
  первичному ключу — одна группа на строку. `values()`/`values_list()` группируют ровно по названным
  полям, если агрегат добавлен **после** них (`values("author_id").annotate(n=Count("id"))` — строка на
  автора); агрегат, добавленный **до** них или переданный им именованным аргументом, считается для каждой
  строки модели, с группировкой и по первичному ключу
  (`annotate(n=Count("books")).values("country", "n")` — строка на автора).
- `Meta.ordering` упорядочивает запрос объектов модели с агрегатом и запрос `values()` с агрегатами,
  добавленными до него, — его колонки тоже попадают в группировку, а группа остаётся одна на строку.
  Запрос, сгруппированный по названным полям, — `group_by()` или агрегат после `values()`, — его не
  применяет: его колонки разбили бы группы.
- Запрос, сгруппированный по названным полям, который читает вне агрегата другую колонку — выбранное
  поле, `order_by()` или условие `HAVING` (`values("author_id").annotate(n=Count("id")).order_by("name")`),
  — выполняет сама база: PostgreSQL и ClickHouse такой запрос отклоняют, а SQLite берёт значение из любой
  строки группы. Сгруппируйте и по ней или читайте её через
  агрегат (`Max("name")`). При группировке по первичному ключу (`group_by("id")`) читаются все колонки
  модели — значение у них одно на группу (ClickHouse получает их и в группировке).

## <a id="aggregates-to-many"></a>Агрегаты и фильтры по связям «ко многим»

`annotate(n=Count("books")).filter(books__price__gt=6)` использует тот же `JOIN`, что и агрегат, поэтому
считаются только книги, подходящие под фильтр, — ровно как `filter(...).annotate(...)`, в каком бы
порядке вы их ни вызвали. Агрегат без `distinct` верен, только пока цепочка его собственной связи —
единственный `JOIN` «ко многим» в запросе. Вторая связь «ко многим» — другой агрегат, более глубокий шаг
вроде `Count("books__reviews")` рядом с `Sum("books__price")`, собственный `_filter=Q(awards__...)`
агрегата, `.filter(awards__...)`, не агрегатное вычисляемое значение вроде
`F("awards__year")`/`Case(When(awards__...))`, поле `values("awards__year")` или
`.order_by("awards__year")` — повторяет каждую строку для каждой связанной строки и молча завысило бы
числа, поэтому вместо этого даётся `QueryError`. Выходы: передайте `distinct=True` в каждый затронутый
`Count()` (это безопасно для подсчёта строк связи), перенесите агрегат в вычисляемое значение
`Subquery()` или выполните агрегаты отдельными запросами. `distinct=True` не помогает `Sum()`/`Avg()`:
они тогда отбросят равные значения, а не повторённые строки (`.exclude(awards__...)` не мешает — он
становится подзапросом `NOT EXISTS`, а не `JOIN`). `OuterReference("awards__id")` внутри
`Subquery()`/`Exists()` соединяет `awards` с внешним запросом и учитывается так же. `Max()`/`Min()` (и
`BoolAnd()`/`BoolOr()`) повторённая строка изменить не может, поэтому им `distinct=True` не нужен. У
агрегата по собственным колонкам основной таблицы (`Sum("advance")`, `Count("id")`) своей цепочки «ко
многим» нет, поэтому любой `JOIN` «ко многим» в запросе — другого агрегата, `.filter(books__...)`,
вычисляемого значения, поля `values()` или сортировки — даёт ту же `QueryError`;
`Count("id", distinct=True)` допустим, как и `.distinct().aggregate(...)` или `aggregate()` по
агрегатному вычисляемому значению — они работают по одной строке на строку основной таблицы.

Поле связи «ко многим», использованное как ключ `GROUP BY`, делит строки на группы, а не повторяет их
внутри одной: `.annotate(total=Sum("copies")).group_by("tags__name").values_list("tags__name", "total")`
(или неявная группировка `.values("tags__name").annotate(total=Sum("copies"))`) суммирует экземпляры
каждого тега. Строка основной таблицы считается один раз на каждую связанную строку с этим значением
ключа: группировка авторов по `books__in_print` считает автора один раз на каждую книгу в продаже,
поэтому самих авторов считайте через `Count("id", distinct=True)`. Ключ, который является первичным
ключом связи или уникальным полем без `NULL` (`tags__id`), или ключи, покрывающие все поля
`UniqueConstraint` без `NULL` либо составного первичного ключа связанной модели, закрепляют одну
связанную строку на группу (уникальное поле с `NULL` — нет: его строки с `NULL` попадают в одну группу, —
если только первый `.filter()` по этой связи не исключает `NULL` на том же `JOIN`, например
`tags__code__isnull=False` или `tags__code="x"`; `tags__code__in=["x", None]` оставляет `NULL`, а
отрицательное условие, в том числе `~~Q(...)`, — это подзапрос, который на `JOIN` ничего не исключает).
Поэтому агрегат по другой связи «ко многим» рядом с ним верен:
`values("tags__id", "tags__name").annotate(n=Count("reviews"))` считает отзывы каждого тега. Любой другой
ключ (`tags__name`) делит строки, только пока ни один агрегат без `distinct` не читает другую связь «ко
многим»: у двух тегов может быть одно имя, и каждый повторяет отзывы внутри этой группы, поэтому
`Count("reviews")` с группировкой только по `tags__name` даёт ошибку — добавьте в ключ первичный ключ.
Строки делит только собственный `JOIN` ключа: второй вызов `.filter(tags__...)` строит отдельный `JOIN`
по `tags`, который повторяет строки внутри каждой группы, поэтому тоже даёт ошибку — объединяйте условия
в один `.filter()`. Простое вычисляемое значение `F("tags__name")` или `.alias()`, использованное как
ключ, считается этим полем. Группировка по агрегатному вычисляемому значению даёт `FieldError` — и на
запросе экземпляров модели, `count()` и `exists()`, которые группы не читают.

`JOIN` «ко многим», который фильтр сужает не больше чем до одной связанной строки на строку, ничего не
повторяет, поэтому агрегат рядом с ним точен. Это равенство одному значению на первичном ключе связи, на
уникальном поле (и допускающем `NULL` — равенство никогда не совпадает с `NULL`) или на всех полях
уникального набора — `filter(tags=tag)`, `filter(tags=1)`, `filter(tags__pk=1)`, `filter(tags__id=1)`,
`filter(tags__slug="x")`, `filter(tags__group=1, tags__rank=2)` — объединённое через AND на верхнем
уровне вызова `.filter()`, либо `__isnull=True` обратного внешнего ключа (`filter(books__isnull=True)`,
также на его первичном ключе или поле без `NULL`), которое оставляет одну строку `NULL`.
`Book.objects.filter(tags=1).aggregate(total=Sum("price"))`,
`Author.objects.filter(books=1).annotate(n=Count("awards"))`, `aggregate()` со срезом, `iterator()` по
курсору и `Subquery(Book.objects.filter(tags=OuterReference("pk")).values("id").annotate(n=Count("id")))`
работают. Условие сужает только `JOIN`, который читает его собственный вызов `.filter()` (первый вызов по
связи читает тот, который делят `values()`/`order_by()`/агрегаты; следующий строит свой), и только
последний шаг «ко многим» его пути (`filter(books__tags=1)` по-прежнему повторяет каждую книгу). Связь
«многие-ко-многим» учитывается, только если промежуточная таблица не может связать две строки дважды —
уникальный индекс автоматической промежуточной таблицы или уникальный набор модели `through=`,
покрывающий оба ключа. Всё остальное — `__in`, диапазон, неуникальное поле, часть уникального набора,
`OR`, `__isnull` у «многие-ко-многим», `__isnull=False` — по-прежнему даёт ошибку. Её даёт и агрегат по
самой суженной связи рядом с другим `JOIN` «ко многим»
(`filter(books=1).annotate(Sum("books__pages"), Count("awards"))`): одна книга всё равно повторяется на
каждую награду, и сумма вышла бы завышенной.

## <a id="grouping-sets"></a>Несколько группировок: `Rollup`, `Cube`, `GroupingSets`

`group_by()` принимает один `Rollup`, `Cube` или `GroupingSets` (`hare.query.grouping`), чтобы
сгруппировать строки несколькими способами в одном запросе — промежуточные итоги и общий итог рядом с
группами:

```python
from hare.query.functions import Count, Grouping
from hare.query.grouping import Cube, GroupingSets, Rollup

await Loan.objects.values("branch", "genre").annotate(total=Count("id")).group_by(Rollup("branch", "genre"))
# группы branch+genre, итог по каждому branch (genre — NULL), общий итог (оба NULL)
```

| Группировка | Группирует по | SQL |
|---|---|---|
| `Rollup("branch", "genre")` | `(branch, genre)`, `(branch)`, всем строкам | `GROUP BY ROLLUP(branch, genre)` |
| `Cube("branch", "format")` | каждому сочетанию: обоим, каждому отдельно, всем строкам | `GROUP BY CUBE(branch, format)` |
| `GroupingSets(("branch", "genre"), "format", ())` | каждому аргументу — имени, последовательности имён, `()` для всех строк | `GROUP BY GROUPING SETS ((branch, genre), (format), ())` |

- Имя — поле, путь к полю связанной модели или вычисляемое значение, как в `group_by()`; обычные имена
  рядом с группировкой входят в каждую группировку (`group_by("year", Rollup("branch", "genre"))` — это
  `GROUP BY year, ROLLUP(branch, genre)`).
- Поле, которое группировка не учитывает, в её строках равно `NULL`. `Grouping(*fields)` отличает такой
  `NULL` от значения `NULL`: это `GROUPING()`, `int` с битом на каждое поле (последнее поле — младший
  бит), установленным там, где группа строки не учитывает поле, — `Grouping("genre")` равен 1 в итогах по
  branch. Считается для каждой группы, как агрегат. Читает поля, по которым запрос группирует (у
  обычного поля группировки он всегда 0); поле вне группировки или запрос без группировки отклоняет сама
  база (PostgreSQL это делает).
- `aggregate()` над такими группами даёт `QueryError` — строки итогов посчитали бы каждую строку
  повторно.
- Они есть в PostgreSQL (`Features.supports_grouping_sets`); в SQLite и ClickHouse группировка и
  `Grouping()` дают `UnSupportedError` до отправки запроса. Один `group_by()` принимает одну группировку —
  иначе `QueryError`; `Rollup()`/`Cube()` без полей, `GroupingSets()` без единого поля и `Grouping()` без
  полей дают `QueryError`.

## <a id="window-functions"></a>Оконные функции

```python
class Window(CombinableExpression):
    def __init__(
        self,
        expression: WindowFunction | Aggregate,
        partition_by: Sequence[str | Expression] = (),
        order_by: Sequence[str | Expression | Ordering] = (),
        frame: WindowFrame | None = None,
    ) -> None: ...
```

`expression` может быть и агрегатом из `hare.query.functions` — `Window(Sum("price"))`,
`Window(Count("id"), partition_by=["author_id"])`, `Window(Avg(F("price") * 2))`; его `_filter=`
становится `FILTER (WHERE ...)` оконного агрегата — пустой `Q()` (и `~Q()`, `Q(Q())`) оставляет все
строки, как у обычного агрегата. `distinct=True` и агрегат без оконного варианта (`ArrayAgg`, `StringAgg`
и другие) дают `QueryError`. Окно принимает арифметику, как любое выражение:
`Window(RowNumber(), order_by=["id"]) + 1`, `Window(Sum("fee"), order_by=["lent_at"]) - F("fee")`.

`order_by` использует то же обозначение `-field`, что `QuerySet.order_by()`, и принимает также сортировки
`F("field").asc()`/`.desc()` с `nulls_first=True`/`nulls_last=True`, чтобы задать, где внутри окна
окажутся `NULL` (см. [Порядок NULL при сортировке](queryset-methods.ru.md#null-ordering)). `partition_by`
и `order_by` принимают и выражения — `partition_by=[Coalesce("genre", Value("none"))]`,
`order_by=[(F("price") * F("quantity")).desc()]`.

Оконные функции лежат в `hare.query.functions.window` —
`from hare.query.functions.window import Lag, Rank, RowNumber`. Его
`Sum`/`Avg`/`Max`/`Min`/`Count`/`StdDev`/`Variance` — оконные двойники агрегатов; `condition=` у них —
это `FILTER (WHERE ...)`, как `_filter=` у агрегата.

| Класс | Сигнатура | SQL |
|---|---|---|
| `RowNumber` | `()` | `ROW_NUMBER()` |
| `Rank` | `()` | `RANK()` |
| `DenseRank` | `()` | `DENSE_RANK()` |
| `NTile` | `(buckets: int)` | `NTILE(buckets)`; `buckets` — целое от 1 до 2147483647, иначе `QueryError` |
| `Sum`/`Avg`/`Max`/`Min`/`Count` | <code>(field: str &#124; Expression, condition: Q &#124; None = None)</code> | `SUM`/`AVG`/`MAX`/`MIN`/`COUNT(field)` |
| `StdDev`/`Variance` | <code>(field: str &#124; Expression, condition: Q &#124; None = None, sample: bool = False)</code> | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP(field)` |
| `CumeDist`/`PercentRank` | `()` | `CUME_DIST()`/`PERCENT_RANK()` |
| `FirstValue`/`LastValue` | <code>(field: str &#124; Expression, condition: Q &#124; None = None)</code> | `FIRST_VALUE`/`LAST_VALUE(field)` |
| `NthValue` | <code>(field: str &#124; Expression, nth: int = 1)</code> | `NTH_VALUE(field, nth)` по всей группе окна, как `LastValue`; `nth` от 1 до 2147483647, иначе `QueryError` |
| `Lag`/`Lead` | <code>(field: str &#124; Expression, offset: int = 1, default: Any = None)</code> | `LAG`/`LEAD(field, offset, default)`; `offset` — целое от 0 до 2147483647, иначе `QueryError` |

```python
await Book.objects.all().annotate(
    price_rank=Window(Rank(), partition_by=["author_id"], order_by=["-price"]),
    previous_price=Window(Lag("price"), partition_by=["author_id"], order_by=["published_at"]),
)
```

Типы результатов: `Count` — всегда `int` (и `Sum`/`Max`/`Min` над вычисляемым значением `Count()` тоже, а
`Avg` над ним — `float`); `Avg` от целых — `float` на любой базе, в том числе внутри арифметики или
`Concat`; `Avg` от `FloatField` — `float`, от `DecimalField` — `Decimal`, не округлённый до `decimal_places`
поля (точность важнее одинаковости на разных базах: PostgreSQL возвращает среднее с собственным масштабом
деления, `1.7125`, а не `1.71`, для `DecimalField(decimal_places=2)`; SQLite и ClickHouse — точную сумму,
делённую с отсечением (не округлением) частного до 20 знаков после запятой), одинаково в `.aggregate()`,
`.annotate()` и в окне; по пустому набору строк `Min`/`Max`/`Avg`/`StdDev`/ `Variance` дают `None` на любой
базе, а `Sum` — `None` в PostgreSQL и SQLite, но собственный `0` в ClickHouse (`0.00` для `Decimal`);
`Sum`/`Max`/`Min`/`FirstValue`/`LastValue`/`Lag`/`Lead` сохраняют тип того, что агрегируют, в том числе
выражения или вычисляемого значения-константы (`Sum(F("duration") * 2)` — `timedelta`, оконный `Sum` над
`Value(Decimal("1.5"))` — `Decimal`). `default` у `Lag`/`Lead` кодируется через поле, как записываемое в
него значение (`dict` для `JSONField`, `datetime`/`Decimal` для своего поля); значение, которое тип поля не
может хранить без потерь (`Lag("pages", 1, 0.5)`), даёт `QueryError`. Тип результата `StdDev`/`Variance`
определяется как у `Avg`; `CumeDist`/`PercentRank` — `float` от 0 до 1, `NthValue` сохраняет тип поля.

> [!NOTE]
> **`LastValue` всегда видит всю группу окна**
>
> `LastValue(field)` записывает явную рамку `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`,
> поэтому всегда возвращает значение поля в действительно последней строке группы. Без явной рамки рамка
> SQL по умолчанию при заданном `order_by` (`RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`) делала
> бы `LAST_VALUE` всегда равным значению *текущей* строки — известная ловушка SQL. `FirstValue` это не
> затрагивает (нижняя граница рамки по умолчанию и так `UNBOUNDED PRECEDING`), поэтому у него остаётся
> рамка SQL по умолчанию. Заданный `frame=` заменяет её.

### <a id="window-frames"></a>Рамки

`frame=` задаёт строки вокруг текущей, по которым считает агрегат или функция значения (`FirstValue`,
`LastValue`, `NthValue`), — `RowRange` и `ValueRange` из `hare.query.expressions`:

```python
from hare.query.expressions import RowRange, ValueRange, Window

await Loan.objects.annotate(
    # Текущая выдача и две перед ней.
    moving_sum=Window(Sum("fee"), order_by=["lent_at"], frame=RowRange(start=-2, end=0)),
    # Все выдачи, плата за которые не больше чем на 10 ниже или выше текущей.
    nearby=Window(Count("id"), order_by=["fee"], frame=ValueRange(start=-10, end=10)),
)
```

`start` и `end` — смещения от текущей строки: отрицательное число — перед ней (`n PRECEDING`), 0 — сама
текущая строка (`CURRENT ROW`), положительное — после неё (`n FOLLOWING`), `None` — без границы: от
первой строки группы для `start`, до последней для `end`; по умолчанию оба `None`.

- `RowRange` — `ROWS BETWEEN ...`: смещения считают строки.
- `ValueRange` — `RANGE BETWEEN ...`: смещения измеряют значение сортировки, а строки с одинаковым
  значением (равные) входят в рамку друг друга, — `ValueRange(start=None, end=0)` и есть рамка SQL по
  умолчанию при `order_by`. Смещению, отличному от 0, нужно окно, упорядоченное ровно по одному числовому
  полю; другой `order_by` даёт `QueryError`.

Рамка без строк даёт `NULL` (`RowRange(start=1, end=2)` на последней строке группы). Функция ранжирования
(`RowNumber`, `Rank`, `DenseRank`, `NTile`, `CumeDist`, `PercentRank`) и `Lag`/`Lead` всегда видят всю
группу — рамка даёт `QueryError`; так же — смещение, которое не `int` и не `None`, и `start` после `end`.
Рамки работают одинаково в PostgreSQL, SQLite и ClickHouse.

> [!WARNING]
> **Фильтр по вычисляемому значению `Window(...)`**
>
> SQL вычисляет оконные функции *после* `WHERE`/`GROUP BY`/`HAVING`, поэтому оконная функция не может
> оказаться в `WHERE` того же запроса. Запрос `.values()`/`.values_list()` с фильтром по ней —
> `ranked_qs.filter(price_rank=1).values("id", "title")`, в том числе внутри `Q(...) | Q(...)`, в
> `exclude()`, в сравнении с полем (`filter(previous_price__lt=F("price"))`) или с окном в значении
> (`filter(price__gt=F("previous_price"))`) — строится как производная таблица: внутренний запрос
> сохраняет остальные фильтры, группировку и оконные функции (фильтр по обычному полю применяется до
> вычисления окна), а внешний фильтрует по колонке окна и применяет сортировку, `distinct()`,
> `limit()`/`offset()` и курсор к оставшимся строкам; `with_cte()` остаётся в начале команды. Вместе с
> `select_for_update()` это даёт `QueryError`. Запрос объектов модели (и его
> `count()`/`exists()`/`update()`/`delete()`) с фильтром по оконной функции даёт `QueryError` —
> фильтруйте запрос `.values()`, например
> `Book.objects.filter(pk__in=Subquery(ranked_qs.filter(price_rank=1).values("pk")))`. Окно в условии
> `Case(When(...))` — допустимый SQL и работает везде; `.aggregate(Sum("price_rank"))` над вычисляемым
> значением-окном и `.update(field=Window(...))` дают `QueryError`.
