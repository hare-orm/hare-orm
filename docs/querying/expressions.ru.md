# Выражения

`Q`, `F`, `Case`/`When` и `Exists`/`OuterRef`/`Subquery` из `hare.query.expressions` — всё, что
передаётся в `.filter()`/`.annotate()`/`.update()` и не является простым значением. Функции базы из
`hare.query.functions` (`Count`, `Sum`, `Max`, `Min`, `Avg`, строковые функции, функции даты и
другие) описаны в разделе [Функции базы данных](functions.ru.md), `Window` — в разделе
[Агрегаты, группировка и оконные функции](aggregation.ru.md), `RawSQL` — в разделе
[Сырой SQL](raw-sql.ru.md), а как написать свою функцию или выражение — в разделе
[Свои функции и выражения](../extending/custom-functions-and-expressions.ru.md). Методы `QuerySet`,
которые это принимают, описаны в разделе [Методы QuerySet](queryset-methods.ru.md).

## `Q` {: #q }

```python
class Connector(StrEnum):  # hare.query.enums
    AND = "AND"
    OR = "OR"

class Q:
    def __init__(self, *conditions: Q | Exists, **filters: Any) -> None
    @classmethod
    def with_connector(cls, connector: Connector, /, *conditions: Q | Exists, **filters: Any) -> Q
```

```python
Book.objects.filter(Q(title__icontains="left hand") | Q(title__icontains="dispossessed"), author=author)
Book.objects.filter(Q.with_connector(Connector.OR, title__icontains="wizard", author__name="Ursula"))
```

`Q(...)` объединяет свои условия через AND, `Q.with_connector(Connector.OR, ...)` — через OR.
Конструктор принимает только условия, поэтому в него помещается фильтр по полю с любым именем — в
том числе по полю `join_type` или `connector`, — а словарь фильтров `dict[str, str]` проходит
проверку типов как `Q(**filters)`. Поле, названное так же, как параметр метода `QuerySet`/`Model`
(`get(using=..., exception=...)`, `When(then=...)`), фильтруется через `Q`:
`Model.objects.get(Q(exception="timeout"))`.

Операторы: `&` / `|` (новый `Q`, объединяющий оба через AND/OR), `~` (новый `Q` с отрицанием,
исходный не меняется), `.negate()` (меняет `_is_negated` на месте), `bool(q)` (`True`, если есть
хотя бы один фильтр или истинный вложенный `Q`; `False` для пустого `Q()`).

## `F` {: #f }

```python
class F(Expression):
    def __init__(self, name: str) -> None
```

`name` — имя поля, `related__field`, путь JSON (`data__key`, `data__0`) или имя вычисляемого
значения; `pk` (и `related__pk`) означает первичный ключ. Поддерживает `+ - * / % **` и их
отражённые варианты (когда `F` справа) — **без `//`**. Те же операторы работают с результатом любого
выражения, поэтому арифметику можно вкладывать и применять к функциям, агрегатам и `Case`:
`(F("price") + 1) * 2`, `100 - (F("price") - 1)`, `Coalesce("rating", 0) + 1`, `Length("title") * 2`,
`Sum("price") + 1`, `Case(...) + 1` — в вычисляемых значениях, фильтрах, агрегатах и `.update()`.

`annotate(x=F("field"))`/`F("related__field")` раскодируется через это поле так же, как
`.values("field")`: `Decimal`, `datetime` с поясом, `UUID`, элемент перечисления, разобранное значение
JSON, расшифрованное значение `EncryptedTextField`/`EncryptedJSONField` — в объектах моделей, в
`.values()`/`.values_list()` и в результатах `union()`. `F("relation")` — первичный ключ связанной
строки, `F("annotation")` сохраняет тип самого вычисляемого значения. Путь JSON внутрь
`EncryptedJSONField` (`F("config__url")`) даёт `FieldError`: по ключу хранится токен Fernet.

`F("data__key")` — значение JSON по этому пути (`jsonb` в PostgreSQL), прочитанное уже разобранным:
объект — `dict`, массив — `list`, логическое значение — `bool`, число — число. Фильтр по такому
вычисляемому значению сравнивает значения JSON: `filter(v=10)` и `filter(v="10")` совпадают с числом
и со строкой соответственно, значение `dict`/`list` сравнивается с объектом или массивом, а
`__in`/`__not_in` принимают сколько угодно значений (они передаются одним параметром-массивом). Как с
преобразованиями ключей в Django, `filter(v=None)` (и `None` внутри `__in`/`__not_in`) совпадает с
JSON `null`, а `v__isnull=True` — с отсутствующим путём: JSON `null` не считается отсутствующим,
поэтому с ним совпадает `v__isnull=False`; `exclude(v=None)` оставляет отсутствующий путь.
`__contains`/`__startswith`/`__icontains`/... сравнивают текст значения (строку без кавычек) и
принимают строку — `dict`/`list` дают `QueryError`. `__has_key`/`__has_keys`/`__has_any_keys`/
`__contained_by`/`__filter` проверяют объект или массив по этому пути, как для всего `JSONField`.
Сортировка (`order_by`, `__gt`/`__lt`/`__range`, а также `order_by()` по всему `JSONField`) на любой
базе идёт в порядке `jsonb`: объект > массив > логическое значение > число > строка > `null`, числа —
по величине, объект или массив с большим числом элементов выше, чем с меньшим, а на верхнем уровне
пустой массив ниже `null` и скалярное значение ниже массива. Строки сравниваются по правилам
сравнения (collation) базы — в SQLite по кодам символов.

`F("annotation__key")` — это так же путь внутрь вычисляемого значения JSON (`JSONObject`,
`F("data")`); путь внутрь вычисляемого значения другого типа даёт `FieldError`. `values()`,
`values_list()` и `order_by()` принимают те же пути по имени, как преобразования ключей в Django:
`values_list("data__owner__name")`, `values("summary__total")`, `order_by("-data__score")`, как и
`filter()` (`filter(data__score__gt=10)`), в том числе после связи (`values("owner__data__plan")`).

Путь JSON, сравниваемый с выражением другого типа (`filter(data__rank=F("number"))`,
`filter(number__gt=F("data__rank"))`, `filter(summary__at=F("at"))`), сравнивает значения JSON:
выражение берётся как значение JSON, которое записал бы для него `JSONObject`, поэтому значение
равно колонке, из которой оно было записано, строка JSON `"2"` не равна числу `2`, а колонка `NULL`
не совпадает ни с чем.

`DateField`, сравниваемое с `DatetimeField`, — `filter(at__gte=F("day"))`, `filter(day__lt=F("at"))` —
сравнивает дату как первый момент её дня в настроенном поясе, как и значение `date`; при
`use_tz=False` — как полночь на часах.

```python
await Employee.objects.filter(pk=emp.pk).update(salary=F("salary") + 100)
```

Выражение `F()`, присвоенное полю перед `save()`, вычисляет база, а не Python, и после сохранения оно
остаётся в объекте невычисленным: `emp.salary` — по-прежнему выражение, а не записанное число.
Поэтому повторный `save()` применит его ещё раз (`salary` вырастет на 100 дважды); после первого
сохранения вызовите `refresh_from_db()`, чтобы прочитать записанное значение и убрать выражение.
`QuerySet.bulk_update()` выражения вообще не принимает (`QueryError`) — используйте
`.update()` или `save()`.

### Арифметика с датой и временем {: #date-time-arithmetic }

`+` и `-` между датой, датой-временем и интервалом (`timedelta`) работают одинаково в SQLite,
PostgreSQL (`asyncpg`) и PostgreSQL (`rust_pg`) — в вычисляемых значениях, фильтрах, `order_by`,
агрегатах и `.update()`:

| Выражение | Результат |
|---|---|
| `DatetimeField ± timedelta` (значение или `F()` поля `TimeDeltaField`) | дата-время |
| `DateField ± timedelta` | дата |
| `DatetimeField - DatetimeField` | интервал |
| `DateField - DateField` | интервал |
| `TimeDeltaField ± TimeDeltaField` / `± timedelta` | интервал |

```python
await Task.objects.filter(pk=task.pk).update(due_at=F("due_at") + timedelta(days=2))
await Task.objects.annotate(duration=F("finished_at") - F("started_at")).filter(duration__gt=timedelta(hours=1))
await Task.aggregate(longest=Max(F("finished_at") - F("started_at")))   # timedelta
```

- Сдвиг даты-времени — это сдвиг **абсолютного времени**, а не календарных дней: прибавление
  `timedelta(days=1)` через переход на летнее время сдвигает часы на 23 или 25 часов, а разность двух
  значений даты-времени — это действительно прошедшее время. Точность — до микросекунды.
- Дата считается полночью: `date ± timedelta` — это дата момента `полночь ± timedelta`, поэтому
  остаток меньше суток **отбрасывается вниз** (`date + 23 часа` — та же дата, `date - 1 час` —
  предыдущая).
- Если одна из сторон `NULL`, результат `NULL`.
- При `use_tz=False` дата-время без пояса — это местное время на часах, и сдвигается оно как
  абсолютное время в поясе системы.
- В SQLite арифметику выполняют функции Python, зарегистрированные в каждом соединении (в SQLite нет
  типа интервала, а собственная арифметика дат теряет микросекунды); такое выражение нельзя
  использовать в определении индекса. В PostgreSQL она записывается как собственная арифметика
  `interval`, а разность значений даты-времени использует `EXTRACT(EPOCH ...)` (точно в PostgreSQL
  14+).
- Всё остальное с датой или временем — число (`F("day") + 1`), `datetime * 2`, `date + datetime`,
  `datetime - date`, любая арифметика с `TimeField`, значение `timedelta` рядом с полем, которое не
  хранит дату или время, — даёт `FieldError` ещё до записи. `TimeDeltaField` вместе с обычным числом
  (`F("duration") * 2`) — по-прежнему обычная арифметика в микросекундах.

### Тип результата {: #result-type }

У результата арифметики на любой базе один и тот же тип Python, определяемый операндами:

- любой операнд с плавающей точкой (`FloatField`, значение `float`, `Avg` от целых) делает его
  `float` — `F("size") * 1.5`, `F("price") * 0.5` и `Sum(F("size") * 1.5)` дают `float`;
- иначе любой операнд `Decimal` (`DecimalField`, значение `Decimal`) делает его `Decimal`;
- иначе это **целое число** (`Count`, `Length`, целочисленные поля и значения, а также
  `TimeDeltaField`, умноженное на число, — `F("duration") * 2` даёт целое число микросекунд).

Число знаков после запятой у результата `Decimal` на любой базе одинаково: наибольшее из операндов
для `+ - % **`, их сумма для `*` (у целого операнда их 0). `F("price") + 1` у поля с
`decimal_places=2` — это `Decimal("2.10")`, у `F("price") * Decimal("1.5")` три знака после запятой,
`F("qty") * Decimal("0.5")` у `IntField` — `Decimal("1.0")`, `Count("items") * F("price")` —
`Decimal("2.20")`. Деление на любой базе настоящее (`F("price") / 3` при `2.00` — это `0.666...`, а
не `0`) и **не** округляется — точность важнее одинаковости на разных базах: в PostgreSQL
`F("price") / 3` — точное `Decimal("0.36666666666666666667")`, а SQLite считает в плавающей точке и
возвращает ближайший `Decimal` к своему результату (`Decimal("0.3666666666666667")`). Если все базы
должны возвращать одни и те же цифры, округляйте в базе: `Round(F("price") / 3, 2)` везде даёт
`Decimal("0.37")`.

`%` подчиняется тем же правилам типов. Между целыми числами это точное целое на любой базе (SQLite
использует свой целочисленный `%`, PostgreSQL — `MOD`), поэтому `F("big") % 7` никогда не теряет
точность. С `Decimal` это точный остаток `Decimal` на любой базе — `Decimal("2.00") % Decimal("0.1")`
даёт `0.00`, в том числе в SQLite (он считает с операндами, приведёнными к целым числам, до 15 знаков
после запятой). С `float` это `float` со знаком делимого (`F("ratio") % 1.5`). Составные операнды
сохраняют группировку: `F("a") % (F("a") - 3)` — остаток от деления на `a - 3`.

Арифметика между двумя колонками модели разных типов даёт `FieldError`, кроме целочисленной колонки
с колонкой `float` или `Decimal` (`F("price") + F("id")`): результат получает тип колонки `float` или
`Decimal`, как в Django; колонка `Decimal` с колонкой `float` (`F("price") * F("rating")`) даёт
ошибку. Вычисленное значение (число строк, длина, другое выражение, значение) сочетается с любой
числовой колонкой по правилам выше.

Вычисляемое значение из одной константы (`annotate(flag=Value(True))`) на любой базе возвращается
своим типом Python — `bool`, `int`, `float`, `Decimal` (с числом знаков константы), `date`,
`datetime` и `time` (передаются и раскодируются как значение `DatetimeField`/`TimeField`: `datetime`
без пояса читается в настроенном поясе при `use_tz=True`, `time` без пояса получает стандартное
смещение, а при `use_tz=False` значение с поясом переводится в местное время), `timedelta`
(передаётся как целое число микросекунд, как `TimeDeltaField`), `uuid.UUID` (передаётся текстом, как
`UUIDField`), `dict` (передаётся текстом JSON, как `JSONField`) и `bytes`. Так же кодируется константа
внутри `Case`, `Coalesce` и `Concat`, а агрегат или оконная функция над вычисляемым значением-константой
сохраняет его тип (`Sum(Value(1))` — `int`). `filter(field=Value(v))` и `update(field=Value(v))`
преобразуют и проверяют `v` через поле так же, как `filter(field=v)`/`update(field=v)` (`Value(None)`
остаётся сравнением с `NULL`). Текстовое значение в арифметике с числовой колонкой
(`F("count") + "5"`) превращается в число типа этой колонки; текст, который не является числом, даёт
`ValidationError` колонки. Фильтр по вычисляемому значению-константе
(`annotate(x=Value(v)).filter(x=v)`) на любой базе сравнивает значения типа константы.

Фильтр по вычисляемому значению, которое является датой-временем, датой или временем, — `F("moment")`,
`F("moment") + timedelta(...)`, `Coalesce("moment", ...)`, `Max("moment")`/`Min("moment")` (фильтр в
`HAVING`) — преобразует значение так же, как фильтр по самому полю: дата-время без пояса читается в
настроенном поясе, строка с датой разбирается, время без пояса получает настроенное смещение.

## `Case` / `When` {: #case-when }

```python
CaseBranchValue = (
    str | int | float | bool | Decimal | date | datetime | time | uuid.UUID | None | F | CombinedExpression | Function
)

class When(Expression):
    def __init__(self, *args: Q, then: CaseBranchValue, negate: bool = False, **kwargs) -> None

class Case(Expression):
    def __init__(self, *args: When, default: CaseBranchValue = None) -> None
```

`then`/`default` принимают настоящее выражение (вычисляется для каждой строки) или простое значение
Python любого из обычных типов значений полей — не только `str`.

`When(negate=True, ...)` отрицает всё условие ветки, в точности как `~Q(...)`:
`When(a=1, b=2, negate=True)` подходит под `NOT (a = 1 AND b = 2)`.

```python
await Employee.objects.all().annotate(
    salary_band=Case(
        When(Q(salary__gte=100_000), then="high"),
        When(Q(salary__gte=50_000), then="mid"),
        default="low",
    )
)
```

`When`, ссылающийся на агрегат или вычисляемое значение, правильно попадает в `HAVING`, а не в
`WHERE`.

Тип результата `Case` определяют все его ветки вместе (`Value(...)` считается значением, которое он
оборачивает, ветка `None` не считается):

- числа разных типов расширяются, как в арифметике: ветка `float` делает результат `float`
  (`Case(When(..., then=F("size")), default=0.5)` — это `3.0`/`0.5`), иначе ветка `Decimal` делает его
  `Decimal` с **наибольшим** числом знаков после запятой среди веток — ни одна ветка не округляется
  до числа знаков другой (`then=F("price")` с `default=Decimal("0.125")` дают `Decimal("1.100")` и
  `Decimal("0.125")`), иначе это целое число;
- любое другое поле используется, если все остальные ветки ему подходят (`JSONField`,
  `TimeDeltaField` рядом со значением `timedelta`);
- ветки только из констант одного типа дают этот тип (`bool`, `date`, `datetime`, `timedelta`,
  `uuid.UUID` и другие); константы `date` вместе с константами `datetime` дают `datetime` (дата в
  полночь).

По тем же правилам определяется тип `Coalesce` по всем его аргументам:
`Coalesce("int_null", F("price"))` — `Decimal`, `Coalesce("int_null", 0.5)` — `float`,
`Coalesce("price_null", F("price3"))` — `Decimal` с 3 знаками после запятой.

## `Exists` / `OuterRef` / `Subquery` {: #exists-outerref-subquery }

```python
class Subquery(Term):
    def __init__(self, query: AwaitableQuery) -> None

class OuterRef(Expression):
    def __init__(self, field: str) -> None   # поле внешней модели или путь related__field

class Exists(Expression):
    def __init__(self, queryset: QuerySet | ValuesQuery | ValuesListQuery) -> None
```

```python
await Team.objects.annotate(
    has_open_membership=Exists(Membership.objects.filter(team_id=OuterRef("id"), status="open")),
).filter(has_open_membership=True)
```

`OuterRef` определяет своё имя так же, как это сделал бы `F` во внешнем запросе: поле самой модели,
`pk`, путь `related__field`, имя прямой связи (`OuterRef("author")` — это колонка `author_id`; связь с
составным ключом даёт `FieldError`) или вычисляемое значение внешнего запроса
(`Author.objects.annotate(n=Length("name")).annotate(x=Exists(Book.objects.filter(rating__lt=OuterRef("n"))))`).
Агрегатное вычисляемое значение тоже подходит (`OuterRef("book_count")` при
`book_count=Count("books")`): тогда подзапрос вычисляется для каждой группы внешнего запроса, поэтому
вычисляемое значение `Exists`/`Subquery` само становится агрегатом — оно не попадает в `GROUP BY`, а
фильтр по нему идёт в `HAVING`. На вычисляемое значение с оконной функцией (`Window(...)`) ссылаться
нельзя — это даёт `QueryError`.

`OuterRef` работает только внутри запроса, обёрнутого в `Exists(...)` или `Subquery(...)`. Запрос `QuerySet`, переданный
без `await` в `field__in=`, сам оборачивается в `Subquery`, как в Django.

`Exists(...)` — само по себе условие, как в Django: передавайте его прямо в `filter()`/`exclude()`,
`Q(...)` или `When(...)` и сочетайте через `&`/`|`/`~` (с другим `Exists` или с `Q`):

```python
await Team.objects.filter(Exists(Membership.objects.filter(team_id=OuterRef("id"), status="open")))
await Team.objects.filter(~Exists(Membership.objects.filter(team_id=OuterRef("id"))) | Q(name="Staff"))
await Team.objects.annotate(status_type=Case(When(Exists(Membership.objects.filter(team_id=OuterRef("id"))), then=Value("busy")), default=Value("idle")))
```

Обёрнутый запрос может быть и запросом `.values()`/`.values_list()` — он вставляется со своими
колонками, группировкой и `HAVING`
(`Exists(Book.objects.filter(author_id=OuterRef("id")).group_by("author_id").annotate(total=Sum("rating")).filter(total__gt=10).values("author_id"))`).

Арифметика с `Subquery` с любой стороны строит выражение ORM, которое разбирается внутри внешнего
запроса, поэтому его `OuterRef` продолжает работать: `Subquery(...) + 1`, `1 + Subquery(...)`,
`Subquery(...) * F("qty")` в `annotate()`, как значение фильтра (`filter(age__gt=Subquery(...) + 5)`)
и в `update(price=Subquery(...) + 1)`.

`.annotate(x=Subquery(...))` над запросом `values()`/`values_list()`, выбирающим одну колонку,
раскодируется через поле этой колонки на любой базе: `date`/`datetime` (а не текст в SQLite),
`Decimal` (а не `float`), `timedelta` для `TimeDeltaField` (а не просто микросекунды) — в `values()`,
`values_list()`, объектах моделей и фильтрах по вычисляемому значению.
