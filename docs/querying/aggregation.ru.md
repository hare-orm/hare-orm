# Агрегаты, группировка и оконные функции

## Неявный `GROUP BY` {: #implicit-group-by }

Неявный `GROUP BY` у агрегата в `annotate()` следует правилам Django:

- Выражение, где-либо содержащее агрегат, — `Count("books") + F("id")`,
  `Coalesce(Sum("books__pages"), F("fallback"))`, `F("book_count") * F("price")` над агрегатным
  вычисляемым значением, `Case` с агрегатом в одной из веток, — считается агрегатным: его фильтры идут
  в `HAVING`, а запрос группируется по колонкам, которые оно читает вне агрегата.
- Выбранное не агрегатное выражение группируется по своему номеру в `SELECT` (`GROUP BY 2`), поэтому
  константа внутри него (`F("price") + 5`, `Case(When(..., then=Value("high")))`, `Concat(...)`)
  передаётся один раз.
- Колонка, которую `HAVING` читает вне своих агрегатов (`Q(book_count__gte=2) | Q(city="Oslo")`,
  `book_count__gt=F("limit")`), тоже попадает в группировку.
- Связанный с внешним запросом `Exists(...)`/`Subquery(...)` рядом с агрегатом группируется целиком,
  по своему номеру в `SELECT` (как в Django 4.2+), а не по внешним колонкам, которые читает его
  `OuterRef(...)`: две внешние строки с разными значениями `OuterRef`, но одинаковым результатом
  подзапроса попадают в одну группу. Только подзапрос, читающий внешний агрегат, группируется по
  этим колонкам.
- Оконная функция (`Window(...)`) никогда не бывает ключом группировки — она вычисляется для каждой
  группы.
- Запрос объектов модели (и его `count()`/`exists()`/`update()`/`delete()`) всегда группируется ещё и
  по первичному ключу — одна группа на строку. `values()`/`values_list()` группируют ровно по
  названным полям, если агрегат добавлен **после** них (`values("author_id").annotate(n=Count("id"))`
  — строка на автора); агрегат, добавленный **до** них или переданный им именованным аргументом,
  считается для каждой строки модели, с группировкой и по первичному ключу
  (`annotate(n=Count("books")).values("country", "n")` — строка на автора), как в Django.

## Агрегаты и фильтры по связям «ко многим» {: #aggregates-to-many }

`annotate(n=Count("products")).filter(products__price__gt=6)` использует тот же `JOIN`, что и
агрегат, поэтому считаются только товары, подходящие под фильтр, — ровно как
`filter(...).annotate(...)`, в каком бы порядке вы их ни вызвали (в Django от порядка результат
меняется). Агрегат без `distinct` верен, только пока цепочка его собственной связи — единственный
`JOIN` «ко многим» в запросе. Вторая связь «ко многим» — другой агрегат, более глубокий шаг вроде
`Count("products__reviews")` рядом с `Sum("products__price")`, собственный `_filter=Q(orders__...)`
агрегата, `.filter(orders__...)`, не агрегатное вычисляемое значение вроде
`F("orders__total")`/`Case(When(orders__...))`, поле `values("orders__total")` или
`.order_by("orders__total")` — повторяет каждую строку для каждой связанной строки и молча завысило
бы числа, поэтому вместо этого даётся `QueryError`. Выходы: передайте `distinct=True` в
каждый затронутый `Count()` (это безопасно для подсчёта строк связи), перенесите агрегат в
вычисляемое значение `Subquery()` или выполните агрегаты отдельными запросами. `distinct=True` не
помогает `Sum()`/`Avg()`: они тогда отбросят равные значения, а не повторённые строки
(`.exclude(orders__...)` не мешает — он становится подзапросом `NOT EXISTS`, а не `JOIN`).
`OuterRef("orders__id")` внутри `Subquery()`/`Exists()` соединяет `orders` с внешним запросом и
учитывается так же. `Max()`/`Min()` (и `BoolAnd()`/`BoolOr()`) повторённая строка изменить не может,
поэтому им `distinct=True` не нужен. У агрегата по собственным колонкам основной таблицы
(`Sum("budget")`, `Count("id")`) своей цепочки «ко многим» нет, поэтому любой `JOIN` «ко многим» в
запросе — другого агрегата, `.filter(products__...)`, вычисляемого значения, поля `values()` или
сортировки — даёт ту же `QueryError`; `Count("id", distinct=True)` допустим, как и
`.distinct().aggregate(...)` или `aggregate()` по агрегатному вычисляемому значению — они работают
по одной строке на строку основной таблицы.

Поле связи «ко многим», использованное как ключ `GROUP BY`, делит строки на группы, а не повторяет
их внутри одной: `.annotate(total=Sum("qty")).group_by("tags__name").values_list("tags__name",
"total")` (или неявная группировка `.values("tags__name").annotate(total=Sum("qty"))`) суммирует
товары каждого тега, как в Django. Строка основной таблицы считается один раз на каждую связанную
строку с этим значением ключа: группировка отделов по `emps__active` считает отдел один раз на
каждого активного сотрудника, поэтому сами отделы считайте через `Count("id", distinct=True)`. Ключ,
который является первичным ключом связи или уникальным полем без `NULL` (`tags__id`), или ключи,
покрывающие все поля `UniqueConstraint` без `NULL` либо составного первичного ключа связанной
модели, закрепляют одну связанную строку на группу (уникальное поле с `NULL` — нет: его строки с
`NULL` попадают в одну группу, — если только первый `.filter()` по этой связи не исключает `NULL` на
том же `JOIN`, например `tags__code__isnull=False` или `tags__code="x"`; `tags__code__in=["x", None]`
оставляет `NULL`, а отрицательное условие, в том числе `~~Q(...)`, — это подзапрос, который на `JOIN`
ничего не исключает). Поэтому агрегат по другой связи «ко многим» рядом с ним верен, как в Django:
`values("tags__id", "tags__name").annotate(n=Count("reviews"))` считает отзывы каждого тега. Любой
другой ключ (`tags__name`) делит строки, только пока ни один агрегат без `distinct` не читает другую
связь «ко многим»: у двух тегов может быть одно имя, и каждый повторяет отзывы внутри этой группы,
поэтому `Count("reviews")` с группировкой только по `tags__name` даёт ошибку — добавьте в ключ
первичный ключ. Строки делит только собственный `JOIN` ключа: второй вызов `.filter(tags__...)`
строит отдельный `JOIN` по `tags`, который повторяет строки внутри каждой группы, поэтому тоже даёт
ошибку — объединяйте условия в один `.filter()`. Простое вычисляемое значение `F("tags__name")` или
`.alias()`, использованное как ключ, считается этим полем. Группировка по агрегатному вычисляемому
значению даёт `FieldError`.

`JOIN` «ко многим», который фильтр сужает не больше чем до одной связанной строки на строку, ничего
не повторяет, поэтому агрегат рядом с ним точен, как в Django. Это равенство одному значению на
первичном ключе связи, на уникальном поле (и допускающем `NULL` — равенство никогда не совпадает с
`NULL`) или на всех полях уникального набора — `filter(tags=tag)`, `filter(tags=1)`,
`filter(tags__pk=1)`, `filter(tags__id=1)`, `filter(tags__slug="x")`,
`filter(tags__group=1, tags__rank=2)` — объединённое через AND на верхнем уровне вызова `.filter()`,
либо `__isnull=True` обратного внешнего ключа (`filter(books__isnull=True)`, также на его первичном
ключе или поле без `NULL`), которое оставляет одну строку `NULL`.
`Book.objects.filter(tags=1).aggregate(total=Sum("price"))`, `Author.objects.filter(books=1).annotate(n=Count("awards"))`,
`aggregate()` со срезом, `iterator()` по курсору и
`Subquery(Book.objects.filter(tags=OuterRef("pk")).values("id").annotate(n=Count("id")))` работают. Условие
сужает только `JOIN`, который читает его собственный вызов `.filter()` (первый вызов по связи читает
тот, который делят `values()`/`order_by()`/агрегаты; следующий строит свой), и только последний шаг
«ко многим» его пути (`filter(books__tags=1)` по-прежнему повторяет каждую книгу). Связь
«многие-ко-многим» учитывается, только если промежуточная таблица не может связать две строки
дважды — уникальный индекс автоматической промежуточной таблицы или уникальный набор модели
`through=`, покрывающий оба ключа. Всё остальное — `__in`, диапазон, неуникальное поле, часть
уникального набора, `OR`, `__isnull` у «многие-ко-многим», `__isnull=False` — по-прежнему даёт ошибку.
Её даёт и агрегат по самой суженной связи рядом с другим `JOIN` «ко многим»
(`filter(books=1).annotate(Sum("books__pages"), Count("awards"))`): одна книга всё равно повторяется на
каждую награду — Django в этом случае возвращает завышенную сумму.

## Оконные функции {: #window-functions }

```python
class Window(Expression):
    def __init__(self, expression: WindowFunction | Aggregate, partition_by: Sequence[str] = (), order_by: Sequence[str | Ordering] = ()) -> None
```

Как в Django, `expression` может быть и агрегатом из `hare.query.functions` — `Window(Sum("salary"))`,
`Window(Count("id"), partition_by=["department_id"])`, `Window(Avg(F("salary") * 12))`; его `_filter=`
становится `FILTER (WHERE ...)` оконного агрегата — пустой `Q()` (и `~Q()`, `Q(Q())`) оставляет все
строки, как у обычного агрегата. `distinct=True` и агрегат без оконного варианта (`ArrayAgg`,
`StringAgg` и другие) дают `QueryError`.

`order_by` использует то же обозначение `-field`, что `QuerySet.order_by()`, и принимает также
сортировки `F("field").asc()`/`.desc()` с `nulls_first=True`/`nulls_last=True`, чтобы задать, где внутри
окна окажутся `NULL` (см. [Порядок NULL при сортировке](queryset-methods.ru.md#null-ordering)).

| Класс | Сигнатура | SQL |
|---|---|---|
| `RowNumber` | `()` | `ROW_NUMBER()` |
| `Rank` | `()` | `RANK()` |
| `DenseRank` | `()` | `DENSE_RANK()` |
| `NTile` | `(buckets: int)` | `NTILE(buckets)` |
| `Sum`/`Avg`/`Max`/`Min`/`Count` | `(field: str)` | `SUM`/`AVG`/`MAX`/`MIN`/`COUNT(field)` |
| `StdDev`/`Variance` | `(field: str, sample: bool = False)` | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP(field)` |
| `CumeDist`/`PercentRank` | `()` | `CUME_DIST()`/`PERCENT_RANK()` |
| `FirstValue`/`LastValue` | `(field: str)` | `FIRST_VALUE`/`LAST_VALUE(field)` |
| `NthValue` | `(field: str, nth: int = 1)` | `NTH_VALUE(field, nth)` по всей группе окна, как `LastValue`; `nth` с 1, иначе `QueryError` |
| `Lag`/`Lead` | `(field: str, offset: int = 1, default: Any = None)` | `LAG`/`LEAD(field, offset, default)` |

```python
await Employee.objects.all().annotate(
    salary_rank=Window(Rank(), partition_by=["department_id"], order_by=["-salary"]),
    prev_salary=Window(Lag("salary"), partition_by=["department_id"], order_by=["hired_at"]),
)
```

Типы результатов: `Count` — всегда `int` (и `Sum`/`Max`/`Min` над вычисляемым значением `Count()`
тоже, а `Avg` над ним — `float`); `Avg` от целых — `float` на любой базе, в том числе внутри
арифметики или `Concat`; `Avg` от `FloatField` — `float`, от `DecimalField` — `Decimal`, не
округлённый до `decimal_places` поля (точность важнее одинаковости на разных базах: PostgreSQL
возвращает своё точное среднее, SQLite — ближайший `Decimal` к результату в плавающей точке, `1.7125`,
а не `1.71`, для `DecimalField(decimal_places=2)`), одинаково в `.aggregate()`, `.annotate()` и в окне;
`Sum`/`Max`/`Min`/`FirstValue`/`LastValue`/`Lag`/`Lead` сохраняют тип того, что агрегируют, в том
числе выражения или вычисляемого значения-константы (`Sum(F("duration") * 2)` — `int`, оконный `Sum`
над `Value(Decimal("1.5"))` — `Decimal`). `default` у `Lag`/`Lead` кодируется через поле, как
записываемое в него значение (`dict` для `JSONField`, `datetime`/`Decimal` для своего поля);
значение, которое тип поля не может хранить без потерь (`Lag("int_field", 1, 0.5)`), даёт
`QueryError`. Тип результата `StdDev`/`Variance` определяется как у `Avg`; `CumeDist`/`PercentRank` —
`float` от 0 до 1, `NthValue` сохраняет тип поля.

!!! note "`LastValue` всегда видит всю группу окна"
    `LastValue(field)` записывает явную рамку `ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED
    FOLLOWING`, поэтому всегда возвращает значение поля в действительно последней строке группы.
    Без явной рамки рамка SQL по умолчанию при заданном `order_by` (`RANGE BETWEEN UNBOUNDED
    PRECEDING AND CURRENT ROW`) делала бы `LAST_VALUE` всегда равным значению *текущей* строки —
    известная ловушка SQL. `FirstValue` это не затрагивает (нижняя граница рамки по умолчанию и так
    `UNBOUNDED PRECEDING`), поэтому у него остаётся рамка SQL по умолчанию.

!!! warning "Фильтр по вычисляемому значению `Window(...)`"
    SQL вычисляет оконные функции *после* `WHERE`/`GROUP BY`/`HAVING`, поэтому оконная функция не
    может оказаться в `WHERE` того же запроса. Запрос `.values()`/`.values_list()` с фильтром по
    ней — `ranked_qs.filter(salary_rank=1).values("id", "name")`, в том числе внутри
    `Q(...) | Q(...)`, в `exclude()`, в сравнении с полем (`filter(prev_salary__lt=F("salary"))`) или
    с окном в значении (`filter(salary__gt=F("prev_salary"))`) — строится как производная таблица:
    внутренний запрос сохраняет остальные фильтры, группировку и оконные функции (фильтр по обычному
    полю применяется до вычисления окна, как в Django), а внешний фильтрует по колонке окна и
    применяет сортировку, `distinct()`, `limit()`/`offset()` и курсор к оставшимся строкам; `with_cte()`
    остаётся в начале команды. Вместе с `select_for_update()` это даёт `QueryError`. Запрос
    объектов модели (и его `count()`/`exists()`/`update()`/`delete()`) с фильтром по оконной функции
    даёт `QueryError` — фильтруйте запрос `.values()`, например
    `Employee.objects.filter(pk__in=Subquery(ranked_qs.filter(salary_rank=1).values("id")))` (`"id"`, а не
    `"pk"` — буквальное `"pk"` в `.values()`/`.values_list()` не разбирается). Окно в условии
    `Case(When(...))` — допустимый SQL и работает везде; `.aggregate(Sum("salary_rank"))` над
    вычисляемым значением-окном и `.update(field=Window(...))` дают `QueryError`.
