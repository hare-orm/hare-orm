# Функции базы данных (`hare.query.functions`)

## Агрегаты и строковые функции (`hare.query.functions`) {: #aggregate-string-functions }

| Класс | SQL | Примечания |
|---|---|---|
| `Trim`/`Length`/`Lower`/`Upper` | `TRIM`/`LENGTH`/`LOWER`/`UPPER` | |
| `Coalesce(field, default_value)` | `COALESCE` | |
| `Round(field, precision=0)` | `ROUND` | `precision` — от 0 до 1000 знаков после запятой. У результата `Decimal` на любой базе ровно `precision` знаков, целое остаётся целым, `float` — `float`, в том числе для константы или выражения (`Round(Value(3), 1)` — `3`, `Round(F("qty") * 0.5, 1)` — `float`). PostgreSQL округляет точное значение, SQLite — значение в плавающей точке, поэтому значение без точного двоичного представления (`2.675`) там может округлиться в другую сторону. |
| `Concat(field, *args)` | `CONCAT` | PostgreSQL приводит каждый аргумент к `::text`. Каждый аргумент на любой базе соединяется одним и тем же текстом: логическое значение — как `true`/`false`; значение `date`/`datetime` или `DatetimeField` — как текст ISO (дата-время с поясом — в UTC: `2020-01-02 00:04:05+00:00`); `float` — так, как его пишет PostgreSQL (`2`, `2.25`, `1e+15`); `Decimal` с известным числом знаков — ровно с этим числом знаков (`F("price") * 2` — `2.20`); значение `timedelta` — числом микросекунд, `uuid.UUID` — своим текстом. Значение `bytes` даёт `QueryError`. В SQLite записывается через <code>&#124;&#124;</code>, поэтому работает на любой поддерживаемой версии SQLite. |
| `StdDev(field, sample=False)`/`Variance(field, sample=False)` | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP` | Показатель по всей совокупности или по выборке при `sample=True`; `distinct=`/`_filter=` — как у агрегатов ниже. `NULL` пропускаются; без значений (меньше двух при `sample=True`) результат `None`. Тип — как у `Avg`: `float` для целых и `float`, не округлённый `Decimal` для `DecimalField`. В SQLite таких функций нет — hare регистрирует свои, с теми же результатами. |
| `Count`/`Sum`/`Max`/`Min`/`Avg` | агрегаты | Все принимают <code>distinct: bool = False, &#95;filter: Q &#124; Exists &#124; None = None</code> (`Exists(...)` — то же, что `Q(Exists(...))`, всё остальное даёт `TypeError`; `Avg("x", distinct=True)` усредняет различные значения). `_filter` — это `FILTER (WHERE ...)` агрегата: строка, не прошедшая его, не участвует в агрегате (группа без подходящих строк даёт `0` для `Count` и `None` для остальных). Как в Django, `_filter`, читающий агрегатное вычисляемое значение (`Count("dept_id", _filter=Q(n__gt=1))` рядом с `n=Count("id")`, в том числе внутри `Window(...)`), даёт `FieldError` |

```python
await Employee.objects.all().annotate(
    emp_count=Count("id"), avg_salary=Avg("salary"),
).group_by("department_id").values("department_id", "emp_count", "avg_salary")

# агрегат с условием:
await Team.objects.annotate(active_count=Count("id", _filter=Q(status="active")))
await Team.aggregate(with_members=Count("id", _filter=Exists(Member.objects.filter(team_id=OuterRef("id")))))
```

## Функции даты (`hare.query.functions`) {: #date-functions }

| Класс | Результат |
|---|---|
| `Extract(field, part, *, tzinfo=None)` | часть как `int`: `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day`, `hour`, `minute`, `second`, `microsecond` |
| `ExtractYear`/`ExtractIsoYear`/`ExtractQuarter`/`ExtractMonth`/`ExtractWeek`/`ExtractWeekDay`/`ExtractIsoWeekDay`/`ExtractDay`/`ExtractHour`/`ExtractMinute`/`ExtractSecond(field, *, tzinfo=None)` | то же для одной части |
| `Trunc(field, trunc_type, *, tzinfo=None)` | значение, округлённое вниз до `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute` или `second`; `date`/`time` берут дату или время суток значения даты-времени |
| `TruncYear`/`TruncQuarter`/`TruncMonth`/`TruncWeek`/`TruncDay`/`TruncHour`/`TruncMinute`/`TruncSecond`/`TruncDate`/`TruncTime(field, *, tzinfo=None)` | то же для одного вида |
| `Now()` | момент выполнения команды как значение `DatetimeField` — `STATEMENT_TIMESTAMP()` в PostgreSQL, поэтому две команды одной транзакции читают разные моменты (`TransactionNow()` из `hare.dialects.postgresql.functions` — начало транзакции) |

Они работают на любой базе. `DatetimeField` читается в текущем часовом поясе — том, который задаёт
для блока кода [`Timezone.override()`](filters.ru.md#time-zone-semantics-on-datetimefield), иначе
в настроенном поясе Hare (в местном поясе системы при `use_tz=False`) — так же, как в операторах
`__year`/`__date`: при `timezone="Europe/Moscow"` значение `2024-01-31 22:30Z` даёт `ExtractDay` = `1`,
а `TruncMonth` — 1 февраля, 00:00 по Москве. `tzinfo=` (имя IANA или `ZoneInfo`) берёт части одного
выражения в своём поясе — `Trunc("created_at", "month", tzinfo="Europe/Moscow")`; неизвестный пояс
или постоянное смещение без имени IANA дают `ConfigurationError`, потому что база переводит время по
имени пояса. `Trunc` сохраняет тип значения — дата-время в настроенном поясе, `date`, время суток со
смещением, — кроме `TruncDate`/`TruncTime`. Неделя (`week`) начинается с понедельника; `week_day`
считает от 1 (воскресенье) до 7, `iso_week_day` — от 1 (понедельник) до 7. Часть или вид, которых у
значения нет (час у `DateField`, месяц у `TimeField`), дают `FieldError`, как и аргумент, который не
является датой, временем или датой-временем.

```python
await Order.objects.annotate(month=TruncMonth("created_at")).group_by("month").annotate(total=Count("id")).values("month", "total")
await Order.objects.annotate(weekday=ExtractWeekDay("created_at")).filter(weekday__in=[1, 7])
await Session.objects.filter(expires_at__lt=Now()).delete()
```

## Математические функции (`hare.query.functions`) {: #math-functions }

| Класс | Результат |
|---|---|
| `Abs(x)`, `Ceil(x)`, `Floor(x)`, `Sign(x)` | тип аргумента |
| `Mod(x, y)` | остаток со знаком `x` (`Mod(-7, 3)` — `-1`); целое для двух целых |
| `Power(x, y)`, `Sqrt(x)`, `Exp(x)`, `Ln(x)`, `Log(base, x)` | `Decimal`, если аргумент `Decimal`, иначе `float` |
| `Sin`, `Cos`, `Tan`, `Cot`, `ASin`, `ACos`, `ATan(x)`, `ATan2(y, x)`, `Degrees(x)`, `Radians(x)`, `Pi()` | `float` |

Каждый аргумент — имя поля, выражение или число (`Mod("quantity", 3)`, `Power(2, "level")`);
логическое значение или любая другая константа дают `QueryError`. Работают одинаково на любой базе
(в SQLite — через функции, которые hare регистрирует в каждом соединении), а аргумент `NULL` даёт
`NULL`. Аргумент вне области определения функции (`Sqrt` отрицательного числа, `Ln(0)`, `ASin(2)`,
`Mod(x, 0)`) даёт `OperationalError`; в PostgreSQL это прерывает окружающую транзакцию, как любая
неудачная команда.

```python
await Product.objects.annotate(rounded=Ceil(F("price") / 10) * 10)
await Point.objects.annotate(distance=Sqrt(Power("x", 2) + Power("y", 2))).filter(distance__lt=5)
```

## Текстовые функции (`hare.query.functions`) {: #text-functions }

| Класс | Результат |
|---|---|
| `Left(text, length)`, `Right(text, length)` | первые/последние символы; при отрицательной длине столько символов отбрасывается с другого конца |
| `Substr(text, position, length=None)` | с позиции, считая с 1 (позиции до 1 считаются пустыми, как в PostgreSQL); отрицательная длина даёт `OperationalError` |
| `StrIndex(text, substring)` | позиция первого вхождения, считая с 1, или `0`, если вхождения нет, — `int` |
| `Replace(text, old, new="")`, `Repeat(text, count)`, `Reverse(text)` | текст |
| `LPad(text, length, fill=" ")`, `RPad(...)` | дополнено до `length`; более длинный текст обрезается до неё |
| `LTrim(text)`, `RTrim(text)` | без пробелов в начале/конце |
| `Chr(code)`, `Ord(text)` | символ по коду; код первого символа (`0` у пустого текста) |
| `MD5`, `SHA1`, `SHA224`, `SHA256`, `SHA384`, `SHA512(text)` | хэш текста в UTF-8 шестнадцатеричными цифрами в нижнем регистре |

Первый аргумент — имя поля, выражение или константа; следующий строковый аргумент — это текст
(`Replace("name", "-", " ")`), поле туда передавайте как `F("other")`. Каждая функция на любой базе
работает так же, как в PostgreSQL, а аргумент `NULL` даёт `NULL`. `SHA1` в PostgreSQL требует
расширения `pgcrypto`.

## Функции сравнения и преобразования (`hare.query.functions`) {: #comparison-functions }

| Класс | Результат |
|---|---|
| `Cast(expression, output_field)` | значение, преобразованное к типу `output_field` — целое, `float`, `Decimal`, `CharField`/`TextField`, логическое значение, дата, дата-время или время (любое другое поле даёт `FieldError`) |
| `Greatest(*values)`, `Least(*values)` | наибольшее/наименьшее из двух и более значений, пропуская `NULL` |
| `NullIf(expression, other)` | `NULL`, если значение равно `other`, иначе само значение |
| `Collate(expression, collation)` | текст, который сравнивается и сортируется по правилу сравнения (collation) базы |

`Cast` на любой базе следует правилам PostgreSQL: `float` округляется до целого к ближайшему чётному
(`2.5` → `2`), а `Decimal` — от нуля (`2.50` → `3`); текст обрезается по краям и должен записывать
значение этого типа (`" 42 "` → `42`, `"abc"` даёт `OperationalError`); логическое значение
преобразуется только в 32-битный `IntField`; целевой `Decimal` даёт ошибку, если целых цифр больше,
чем он вмещает; целевой `CharField(max_length=n)` обрезает текст до `n` символов; `float` становится
текстом так, как его пишет PostgreSQL (`1e+15`); дата-время с поясом читается в UTC, а без пояса
(`use_tz=False`) — как время на часах. SQLite не может хранить NaN, поэтому преобразование текста
`"NaN"` в число там даёт ошибку.

`Greatest`/`Least` принимают имена полей, выражения или константы
(`Greatest("updated_at", "created_at")`, `Least("price", 100)`) и дают `NULL`, только если все
значения `NULL` — собственные `max()`/`min()` SQLite с несколькими аргументами дали бы `NULL` при
любом `NULL`. Числа сравниваются как числа, и результат получает их общий тип. `other` у `NullIf` —
константа или выражение; строка там считается текстом. Имя в `Collate` — простое имя (буквы, цифры,
`_`, `-`, `.`, `@`), например `NOCASE` в SQLite или `C`/`und-x-icu` в PostgreSQL.

## Функции JSON (`hare.query.functions`) {: #json-functions }

`JSONObject(**fields)` строит объект JSON из данных ключей и значений и раскодируется как `dict`:
`JSONObject(name="name", total=F("price") * 2)`. Строковое значение — имя поля; любое другое значение —
выражение или константа. Каждая база записывает значение так, как его хранит `jsonb` PostgreSQL:
логическое значение — как `true`/`false`, `Decimal` и `float` — как число (целочисленный `float` —
как целое, `2`), дата-время с поясом — текстом ISO в UTC (`2020-01-02T03:04:05.5+00:00`), без пояса —
временем на часах, время — со смещением (`12:30:00+00`), байты — как `\x` и шестнадцатеричные цифры,
поле JSON или вложенный `JSONObject` — как вложенный JSON. Ключи объекта возвращаются в порядке базы —
в PostgreSQL в порядке самого `jsonb` (сначала более короткие). Ключ с нулевым байтом даёт
`QueryError`. Фильтруйте такое вычисляемое значение операторами `JSONField`
(`summary__filter={"total__gt": 10}`, `summary__contains={...}`) или читайте путь внутри него через
`F("summary__total")`.

```python
await Book.objects.annotate(card=JSONObject(title="title", author=JSONObject(name="author__name"))).values_list("card")
```
