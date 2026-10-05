# Выражения

`Q`, `F`, `Value`, `Case`/`When` и `Exists`/`OuterReference`/`Subquery` из `hare.query.expressions` —
всё, что передаётся в `.filter()`/`.annotate()`/`.update()` и не является простым значением. Функции базы
данных из `hare.query.functions` (`Count`, `Sum`, `Max`, `Min`, `Avg`, строковые функции, функции даты и
другие) описаны в разделе [Функции базы данных](functions.ru.md), `Window` — в разделе
[Агрегаты, группировка и оконные функции](aggregation.ru.md), `RawSQL` — в разделе
[Готовый SQL](raw-sql.ru.md), а как написать свою функцию или выражение — в разделе [Свои функции и
выражения](../extending/custom-functions-and-expressions.ru.md). Методы `QuerySet`, которые это
принимают, описаны в разделе [Методы QuerySet](queryset-methods.ru.md).

## <a id="q"></a>`Q`

```python
# hare.query.enums
class Connector(StrEnum):
    AND = "AND"
    OR = "OR"

class Q(Plannable):
    def __init__(self, *conditions: Q | Exists, **filters: Any) -> None: ...

    @classmethod
    def with_connector(cls, connector: Connector, *conditions: Q | Exists, **filters: Any) -> Q: ...
```

```python
Book.objects.filter(Q(title__icontains="left hand") | Q(title__icontains="dispossessed"), author=author)
Book.objects.filter(Q.with_connector(Connector.OR, title__icontains="wizard", author__name="Ursula"))
```

`Q(...)` объединяет свои условия через AND, `Q.with_connector(Connector.OR, ...)` — через OR. Конструктор
принимает только условия, поэтому в него помещается фильтр по полю с любым именем — в том числе по полю
`join_type` или `connector`, — а словарь фильтров `dict[str, str]` проходит проверку типов как
`Q(**filters)`. Поле, названное так же, как параметр метода `QuerySet`/`Model`
(`get(does_not_exist_exception=...)`, `When(then=...)`), фильтруется через `Q`:
`Model.objects.get(Q(does_not_exist_exception="timeout"))`.

Операторы: `&` / `|` (новый `Q`, объединяющий оба через AND/OR), `~` (новый `Q` с отрицанием, исходный не
меняется), `.negate()` (меняет `_is_negated` на месте), `bool(q)` (`True`, если есть хотя бы один фильтр,
`Exists` или истинный вложенный `Q`; `False` для пустого `Q()`).

## <a id="f"></a>`F`

```python
class F(CombinableExpression):
    def __init__(self, name: str) -> None: ...
```

`name` — имя поля, `related__field`, путь JSON (`data__key`, `data__0`) или имя вычисляемого значения;
`pk` (и `related__pk`) означает первичный ключ. Поддерживает `+ - * / % **` и их отражённые варианты
(когда `F` справа) — **без `//`**. Те же операторы работают с результатом `F()`, функции, агрегата и
`Case` (но не `Value(...)` — литерала, записанного как одно значение), поэтому арифметику можно
вкладывать: `(F("price") + 1) * 2`, `100 - (F("price") - 1)`, `Coalesce("rating", 0) + 1`,
`Length("title") * 2`, `Sum("price") + 1`, `Case(...) + 1` — в вычисляемых значениях, фильтрах, агрегатах
и `.update()`. Каждый оператор строит `CombinedExpression(left, connector, right)` с `connector` из
`ArithmeticOperator` (`ADD`, `SUB`, `MUL`, `DIV`, `POW`, `MOD`).

`annotate(x=F("field"))`/`F("related__field")` раскодируется через это поле так же, как
`.values("field")`: `Decimal`, `datetime` с поясом, `UUID`, элемент перечисления, разобранное значение
JSON, расшифрованное значение `EncryptedTextField`/`EncryptedJSONField` — в объектах моделей, в
`.values()`/`.values_list()` и в результатах `union()`. `F("relation")` — первичный ключ связанной
строки, `F("annotation")` сохраняет тип самого вычисляемого значения. Путь JSON внутрь
`EncryptedJSONField` (`F("config__url")`) даёт `FieldError`: по ключу хранится токен Fernet.

Поле, которое сравнивается с выражением другого типа (`filter(code=F("number"))`, текст с числом),
сравнивается так, как их сравнивает база: PostgreSQL отклоняет такое сравнение, SQLite сравнивает по своим
правилам. Поле, которое получает значение выражения (`update(total=F("price"))`), должно хранить значения
того же типа — числа с числами, текст с текстом, дата с датой или моментом, — иначе до выполнения запроса
на любой базе возникает `FieldError`: SQLite сохранила бы значение, которое поле потом не прочитает
(`update(flag=F("count"))` записывает число в логическое поле). В текстовое поле можно записать любое
значение — его текстом. `update()` записывает в
поле даты только дату, а в поле момента — только момент (см.
[Арифметика с датой и временем](#date-time-arithmetic)). Где преобразование задумано, его делает
`Cast()`: `filter(code=Cast(F("number"), CharField(max_length=10)))`. Поле другого типа (JSON, UUID,
массив и т. п.) не проверяется.

`F("data__key")` — значение JSON по этому пути (`jsonb` в PostgreSQL), прочитанное уже разобранным:
объект — `dict`, массив — `list`, логическое значение — `bool`, число — число. Арифметика с ним даёт
`FieldError`: для PostgreSQL и ClickHouse значение JSON не число — `Cast(F("data__count"), IntField()) + 1`.
Фильтр по такому
вычисляемому значению сравнивает значения JSON: `filter(v=10)` и `filter(v="10")` совпадают с числом и со
строкой соответственно, значение `dict`/`list` сравнивается с объектом или массивом, а `__in`/`__not_in`
принимают сколько угодно значений (они передаются одним параметром-массивом). `filter(v=None)` (и `None`
внутри `__in`/`__not_in`) совпадает с JSON `null`, а `v__isnull=True` — с отсутствующим путём: JSON
`null` не считается отсутствующим, поэтому с ним совпадает `v__isnull=False`; `exclude(v=None)` оставляет
отсутствующий путь. `__contains`/`__startswith`/`__icontains`/... сравнивают текст значения (строку без
кавычек) и принимают строку — `dict`/`list` дают `QueryError`. `__has_key`/`__has_keys`/`__has_any_keys`/
`__contained_by`/`__filter` проверяют объект или массив по этому пути, как для всего `JSONField`.
Сортировка (`order_by`, `__gt`/`__lt`/`__range`, а также `order_by()` по всему `JSONField`) — собственная
для базы. PostgreSQL идёт в порядке `jsonb`: объект > массив > логическое значение > число > строка >
`null`, числа — по величине, объект или массив с большим числом элементов выше, чем с меньшим, а на
верхнем уровне пустой массив ниже `null` и скалярное значение ниже массива. SQLite упорядочивает значение,
которое читает `json_extract()`: строку — как саму себя, `true`/`false` — как 1/0, JSON `null` — как
`NULL`, объект или массив — как его сжатый текст, числа раньше текста. ClickHouse упорядочивает значение
`Dynamic`, которое читает из текста JSON (настройка сессии `allow_suspicious_types_in_order_by=1`),
сначала по имени его типа, потом по значению (`Float64` `2.5` раньше `Int64` `9`, `Bool` перед числами,
строки после); сравнение пути, где лежат и строки, и числа, даёт ошибку ClickHouse `NO_COMMON_TYPE`.
Строки сравниваются по правилам сравнения (collation) базы — в SQLite по кодам символов.

`F("annotation__key")` — это так же путь внутрь вычисляемого значения JSON (`JSONObject`, `F("data")`);
путь внутрь вычисляемого значения другого типа даёт `FieldError`. `values()`, `values_list()` и
`order_by()` принимают те же пути по имени: `values_list("data__owner__name")`,
`values("summary__total")`, `order_by("-data__score")`, как и `filter()` (`filter(data__score__gt=10)`),
в том числе после связи (`values("owner__data__plan")`).

Путь JSON, сравниваемый с выражением другого типа (`filter(data__rank=F("number"))`,
`filter(number__gt=F("data__rank"))`, `filter(summary__at=F("at"))`), сравнивает значения JSON: выражение
берётся как значение JSON, которое записал бы для него `JSONObject`, поэтому значение равно колонке, из
которой оно было записано, строка JSON `"2"` не равна числу `2`, а колонка `NULL` не совпадает ни с чем.

`DateField`, сравниваемое с `DatetimeField`, — `filter(at__gte=F("day"))`, `filter(day__lt=F("at"))` —
сравнивает дату как первый момент её дня в настроенном поясе, как и значение `date`; при
`use_timezone=False` — как полночь на часах.

```python
await Book.objects.filter(pk=book.pk).update(price=F("price") + 100)
```

Выражение `F()`, присвоенное полю перед `save()`, вычисляет база, а не Python, и после сохранения оно
остаётся в объекте невычисленным: `book.price` — по-прежнему выражение, а не записанное число. Поэтому
повторный `save()` применит его ещё раз (`price` вырастет на 100 дважды); после первого сохранения
вызовите `refresh_from_db()`, чтобы прочитать записанное значение и убрать выражение.
`QuerySet.bulk_update()` выражения вообще не принимает (`QueryError`) — используйте `.update()` или
`save()`.

### <a id="date-time-arithmetic"></a>Арифметика с датой и временем

`+` и `-` между датой, датой-временем и интервалом (`timedelta`) работают одинаково в SQLite, PostgreSQL
(`asyncpg`) и PostgreSQL (`rust_pg`) — в вычисляемых значениях, фильтрах, `order_by`, агрегатах и
`.update()`:

| Выражение | Результат |
|---|---|
| `DatetimeField ± timedelta` (значение или `F()` поля `TimeDeltaField`) | дата-время |
| `DateField ± timedelta` | дата |
| `DatetimeField - DatetimeField` | интервал |
| `DateField - DateField` | интервал |
| `TimeDeltaField ± TimeDeltaField` / `± timedelta` | интервал |
| `TimeDeltaField * number`, `number * TimeDeltaField`, `TimeDeltaField / number` | интервал |

```python
await Loan.objects.filter(pk=loan.pk).update(due_at=F("due_at") + timedelta(days=2))
await Loan.objects.annotate(duration=F("returned_at") - F("lent_at")).filter(duration__gt=timedelta(days=14))
await Loan.objects.aggregate(longest=Max(F("returned_at") - F("lent_at")))   # timedelta
```

- Сдвиг даты-времени — это сдвиг **абсолютного времени**, а не календарных дней: прибавление
  `timedelta(days=1)` через переход на летнее время сдвигает часы на 23 или 25 часов, а разность двух
  значений даты-времени — это действительно прошедшее время. Точность — до микросекунды.
- Дата считается полночью: `date ± timedelta` — это дата момента `midnight ± timedelta`, поэтому остаток
  меньше суток **отбрасывается вниз** (`date + 23 hours` — та же дата, `date - 1 hour` — предыдущая).
- Если одна из сторон `NULL`, результат `NULL`.
- Результат за пределами дат, которые хранит база, даёт `OperationalError`: на SQLite и ClickHouse
  (`timestamp out of range`, `date out of range`; ClickHouse хранит годы с 1900 по 2299, а его
  собственные функции дали бы вместо этого последнюю дату), на PostgreSQL — результат после 9999 года,
  которого не вмещает `datetime` Python, при чтении.
- При `use_timezone=False` дата-время без пояса — это местное время на часах, и сдвигается оно как
  абсолютное время в поясе системы.
- В SQLite арифметику выполняют функции Python, зарегистрированные в каждом соединении с базой (в SQLite
  нет типа интервала, а собственная арифметика дат теряет микросекунды); такое выражение нельзя
  использовать в определении индекса. В PostgreSQL она записывается как собственная арифметика
  `interval`, а разность значений даты-времени использует `EXTRACT(EPOCH ...)` (точно на любой версии
  PostgreSQL, которую поддерживает hare).
- Всё остальное с датой или временем — число (`F("day") + 1`), `datetime * 2`, `date + datetime`,
  `datetime - date`, любая арифметика с `TimeField`, значение `timedelta` рядом с полем, которое не
  хранит дату или время, — даёт `FieldError` ещё до записи. `TimeDeltaField`, умноженное или делённое на
  число — значение или числовую колонку: `F("duration") * 2`, `F("duration") * F("count")`,
  `F("duration") / 3`, — это `timedelta`, как в Python: его микросекунды масштабируются, а частное
  округляется до ближайшей микросекунды, половина — к чётной, как Python делит `timedelta`, а PostgreSQL
  — `interval`. `update(duration=F("duration") * 2)` записывает его обратно. Число, прибавленное к
  интервалу или вычтенное из него (`F("duration") + 1`, `F("duration") - F("count")`), даёт `FieldError`
  — прибавляйте `timedelta`.

### <a id="result-type"></a>Тип результата

У результата арифметики на любой базе один и тот же тип Python, определяемый операндами:

- любой операнд с плавающей точкой (`FloatField`, значение `float`, `Avg` от целых) делает его `float` —
  `F("size") * 1.5`, `F("price") * 0.5` и `Sum(F("size") * 1.5)` дают `float`;
- иначе любой операнд `Decimal` (`DecimalField`, значение `Decimal`) делает его `Decimal`;
- иначе это **целое число** (`Count`, `Length`, целочисленные поля и значения).

Число знаков после запятой у результата `Decimal` на любой базе одинаково: наибольшее из операндов для
`+ - % **`, их сумма для `*` (у целого операнда их 0). `F("price") + 1` у поля с `decimal_places=2` — это
`Decimal("2.10")`, у `F("price") * Decimal("1.5")` три знака после запятой, `F("qty") * Decimal("0.5")` у
`IntField` — `Decimal("1.0")`, `Count("items") * F("price")` — `Decimal("2.20")`. Деление на любой базе
настоящее (`F("price") / 3` при `2.00` — это `0.666...`, а не `0`) и **не** округляется — точность важнее
одинаковости на разных базах. В PostgreSQL `F("price") / 3` — это `Decimal("0.36666666666666666667")`,
с собственным масштабом деления PostgreSQL (не меньше 16 значащих цифр); в SQLite и ClickHouse hare
считает частное точно и отсекает его (не округляет) до 20 знаков после запятой:
`Decimal("0.36666666666666666666")`. Если все базы должны возвращать одни и те же цифры, округляйте в базе:
`Round(F("price") / 3, 2)` везде даёт `Decimal("0.37")`. Сумму, разность и произведение `Decimal` — в том
числе `Decimal` и целого — ClickHouse считает как `Decimal256`, поэтому цифры не теряются там, где узкий
тип переполнился бы (результат длиннее 76 цифр даёт `OperationalError`). SQLite вычисляет любое выражение
над `Decimal` — `+ - * / %`, `Abs`, `Ceil`, `Floor`, `Case`, `Coalesce`, `Sum`, `Avg`, `StdDev`/`Variance` —
по их точному тексту функциями hare, поэтому ни одна цифра не теряется в double, в том числе при
обновлении таким выражением. Деление на ноль — собственное поведение базы, hare его не проверяет:
PostgreSQL даёт `OperationalError`, SQLite даёт `NULL`, ClickHouse даёт `inf`/`nan` для `Decimal` и
`float` и собственную ошибку для целых; делимое или делитель `NULL` дают `NULL`. Между целыми `/` — то,
что даёт база: в PostgreSQL и SQLite целое, округлённое к нулю, — `F("qty") / 2` при `-7` даёт `-3`, и
внутри агрегата тоже (`Avg(F("qty") / 3)` усредняет целые частные); в ClickHouse точное частное, в том числе
дробь (`-3.5`), читаемое как число с плавающей точкой (`Features.divides_integers_exactly`). Сумма
`BigIntField` в PostgreSQL — `NUMERIC`, поэтому `Sum("big") / 2` сохраняет там дробь (`Decimal("2.5")`):
значение выражения — то, что дала база, и до поля вывода оно не обрезается. Целые сумма, разность
или произведение за пределами своего типа дают `OperationalError`: в PostgreSQL это собственное
`integer out of range`, тип — по самому широкому операнду (`IntField` — 32 бита, `BigIntField` или литерал
шире 32 бит — 64); в SQLite — за пределами 64 бит, где SQLite молча превратил бы результат в число с
плавающей точкой; в ClickHouse, который завернул бы результат, hare отказывает.

`%` подчиняется тем же правилам типов. Между целыми числами это точное целое на любой базе (SQLite
использует свой целочисленный `%`, PostgreSQL — `MOD`), поэтому `F("big") % 7` никогда не теряет
точность. С `Decimal` это точный остаток `Decimal` на любой базе — `Decimal("2.00") % Decimal("0.1")`
даёт `0.00`, в том числе в SQLite (он считает с операндами, приведёнными к целым числам, до 15 знаков
после запятой). С `float` это `float` со знаком делимого (`F("ratio") % 1.5`). Составные операнды
сохраняют группировку: `F("a") % (F("a") - 3)` — остаток от деления на `a - 3`.

Арифметика между двумя колонками модели разных типов даёт `FieldError`, кроме целочисленной колонки с
колонкой `float` или `Decimal` (`F("price") + F("id")`): результат получает тип колонки `float` или
`Decimal`; колонка `Decimal` с колонкой `float` (`F("price") * F("rating")`) даёт ошибку. Вычисленное
значение (число строк, длина, другое выражение, значение) сочетается с любой числовой колонкой по
правилам выше.

Вычисляемое значение из одной константы (`annotate(flag=Value(True))`) на любой базе возвращается своим
типом Python — `bool`, `int`, `float`, `Decimal` (с числом знаков константы), `date`, `datetime` и `time`
(передаются и раскодируются как значение `DatetimeField`/`TimeField`: `datetime` без пояса читается в
настроенном поясе при `use_timezone=True`, `time` без пояса получает стандартное смещение, а при
`use_timezone=False` значение с поясом переводится в местное время), `timedelta` (передаётся как целое
число микросекунд, как `TimeDeltaField`), `uuid.UUID` (передаётся текстом, как `UUIDField`), `dict`
(передаётся текстом JSON, как `JSONField`) и `bytes`. Так же кодируется константа внутри `Case`,
`Coalesce` и `Concat`, а агрегат или оконная функция над вычисляемым значением-константой сохраняет его
тип (`Sum(Value(1))` — `int`). `filter(field=Value(v))` и `update(field=Value(v))` преобразуют и
проверяют `v` через поле так же, как `filter(field=v)`/`update(field=v)` (`Value(None)` остаётся
сравнением с `NULL`, а в `update` записывает `NULL`). Текстовое значение в арифметике с числовой колонкой
(`F("count") + "5"`) даёт `ValidationError` колонки, как и её фильтр, — передавайте само число. Фильтр по
любому вычисляемому значению так же преобразует и проверяет значение через его поле
(`annotate(n=Count("id")).filter(n="1")` даёт ошибку). Фильтр по вычисляемому значению-константе
(`annotate(x=Value(v)).filter(x=v)`) на любой базе сравнивает значения типа константы.

Фильтр по вычисляемому значению, которое является датой-временем, датой или временем, — `F("moment")`,
`F("moment") + timedelta(...)`, `Coalesce("moment", ...)`, `Max("moment")`/`Min("moment")` (фильтр в
`HAVING`) — преобразует значение так же, как фильтр по самому полю: дата-время без пояса читается в
настроенном поясе, строка с датой разбирается, время без пояса получает настроенное смещение.

## <a id="case-when"></a>`Case` / `When`

```python
CaseBranchValue = (
    str | int | float | bool | Decimal | date | datetime | time | uuid.UUID | None | F | CombinedExpression | Function
)

class When(Expression):
    def __init__(
        self,
        *args: Q | Exists,
        then: CaseBranchValue,
        negate: bool = False,
        **kwargs: Any,
    ) -> None: ...

class Case(CombinableExpression):
    def __init__(self, *args: When, default: CaseBranchValue = None) -> None: ...
```

`then`/`default` принимают настоящее выражение (вычисляется для каждой строки) или простое значение
Python любого из обычных типов значений полей — не только `str`. Условие ветки — `Q`, `Exists` или
именованные фильтры.

`When(negate=True, ...)` отрицает всё условие ветки, в точности как `~Q(...)`:
`When(a=1, b=2, negate=True)` подходит под `NOT (a = 1 AND b = 2)`.

```python
await Book.objects.all().annotate(
    price_band=Case(
        When(Q(price__gte=100), then="high"),
        When(Q(price__gte=50), then="mid"),
        default="low",
    )
)
```

`When`, ссылающийся на агрегат или вычисляемое значение, правильно попадает в `HAVING`, а не в `WHERE`.

Тип результата `Case` определяют все его ветки вместе (`Value(...)` считается значением, которое он
оборачивает, ветка `None` не считается):

- числа разных типов расширяются, как в арифметике: ветка `float` делает результат `float`
  (`Case(When(..., then=F("size")), default=0.5)` — это `3.0`/`0.5`), иначе ветка `Decimal` делает его
  `Decimal` с **наибольшим** числом знаков после запятой среди веток — ни одна ветка не округляется до
  числа знаков другой (`then=F("price")` с `default=Decimal("0.125")` дают `Decimal("1.100")` и
  `Decimal("0.125")`), иначе это целое число;
- любое другое поле используется, если все остальные ветки ему подходят (`JSONField`, `TimeDeltaField`
  рядом со значением `timedelta`);
- ветки только из констант одного типа дают этот тип (`bool`, `date`, `datetime`, `timedelta`,
  `uuid.UUID` и другие); константы `date` вместе с константами `datetime` дают `datetime` (дата в
  полночь);
- дата рядом с моментом — `DateField` или константа `date` рядом с `DatetimeField` или константой
  `datetime` — даёт момент, дату в полночь по UTC, как PostgreSQL делает её `timestamp` (момент не
  обрезается до даты);
- значения типов, которые не сочетаются, — число с текстом, логическое значение с числом, дата с числом
  или интервалом, текст со временем — дают `FieldError` до выполнения запроса на любой базе: приведите их
  к одному типу через `Cast()`. Строковая константа здесь — текст, даже если читается как число
  (`Coalesce("int_null", Value("7"))` даёт ошибку). Поле другого типа (JSON, UUID, массив и т. п.) не
  проверяется.

По тем же правилам определяется тип `Coalesce` по всем его аргументам, и те же сочетания отвергаются в
`Coalesce`, `NullIf`, `Greatest` и `Least`: `Coalesce("int_null", F("price"))` — `Decimal`,
`Coalesce("int_null", 0.5)` — `float`, `Coalesce("price_null", F("price3"))` — `Decimal` с 3 знаками
после запятой.

## <a id="exists-outerref-subquery"></a>`Exists` / `OuterReference` / `Subquery`

```python
class Subquery(CombinableExpression, Term):
    def __init__(self, query: AwaitableQuery[Any], *, shares_outer_scope: bool = False) -> None: ...

class OuterReference(CombinableExpression):
    # поле внешней модели или путь related__field
    def __init__(self, field: str) -> None: ...

class Exists(Expression):
    def __init__(self, queryset: QuerySet[Any, Any], *, negated: bool = False) -> None: ...
```

```python
await Author.objects.annotate(
    has_book_in_print=Exists(Book.objects.filter(author_id=OuterReference("id"), in_print=True)),
).filter(has_book_in_print=True)
```

`OuterReference` определяет своё имя так же, как это сделал бы `F` во внешнем запросе: поле самой модели,
`pk`, путь `related__field`, имя прямой связи (`OuterReference("author")` — это колонка `author_id`;
связь с составным ключом даёт `FieldError`) или вычисляемое значение внешнего запроса:

```python
Author.objects.annotate(n=Length("name")).annotate(x=Exists(Book.objects.filter(rating__lt=OuterReference("n"))))
```

`OuterReference("pk")` модели с составным первичным ключом сравнивается сразу со всеми его полями: с `pk`
той же модели или со связью с ней (`Child.objects.filter(parent=OuterReference("pk"))`), — а сравнение с
одним полем даёт `FieldError` с перечнем полей ключа. Агрегатное вычисляемое значение тоже подходит
(`OuterReference("book_count")` при `book_count=Count("books")`): тогда подзапрос вычисляется для каждой
группы внешнего запроса, поэтому вычисляемое значение `Exists`/`Subquery` само становится агрегатом — оно
не попадает в `GROUP BY`, а фильтр по нему идёт в `HAVING`. На вычисляемое значение с оконной функцией
(`Window(...)`) ссылаться нельзя — это даёт `QueryError`.

`OuterReference` работает только внутри запроса, обёрнутого в `Exists(...)` или `Subquery(...)`. Запрос
`QuerySet`, переданный без `await` в `field__in=`, сам оборачивается в `Subquery`.

С `shares_outer_scope=True` `OuterReference` запроса подзапроса ссылается на запрос, в который вложен
охватывающий, а не на сам охватывающий — для подзапроса между запросом и его собственными фильтрами: SQL
позволяет им обращаться к любому охватывающему запросу.

`Exists(..., negated=True)` — то же, что `~Exists(...)`. `Exists(...)` — само по себе условие:
передавайте его прямо в `filter()`/`exclude()`, `Q(...)` или `When(...)` и сочетайте через `&`/`|`/`~` (с
другим `Exists` или с `Q`):

```python
await Author.objects.filter(Exists(Book.objects.filter(author_id=OuterReference("id"), in_print=True)))
await Author.objects.filter(~Exists(Book.objects.filter(author_id=OuterReference("id"))) | Q(name="Anonymous"))
await Author.objects.annotate(status_type=Case(When(Exists(Book.objects.filter(author_id=OuterReference("id"))), then=Value("published")), default=Value("unpublished")))
```

Обёрнутый запрос может быть и запросом `.values()`/`.values_list()` — он вставляется со своими колонками,
группировкой и `HAVING`:

```python
Exists(Book.objects.filter(author_id=OuterReference("id")).group_by("author_id").annotate(total=Sum("rating")).filter(total__gt=10).values("author_id"))
```

Арифметика с `Subquery` с любой стороны строит выражение ORM, которое разбирается внутри внешнего
запроса, поэтому его `OuterReference` продолжает работать: `Subquery(...) + 1`, `1 + Subquery(...)`,
`Subquery(...) * F("qty")` в `annotate()`, как значение фильтра (`filter(age__gt=Subquery(...) + 5)`) и в
`update(price=Subquery(...) + 1)`.

`.annotate(x=Subquery(...))` над запросом `values()`/`values_list()`, выбирающим одну колонку,
раскодируется через поле этой колонки на любой базе: `date`/`datetime` (а не текст в SQLite), `Decimal`
(а не `float`), `timedelta` для `TimeDeltaField` (а не просто микросекунды) — в `values()`,
`values_list()`, объектах моделей и фильтрах по вычисляемому значению.
