# Функции базы данных

Функции `hare.query.functions` передаются в `annotate()`, `filter()`, `order_by()`, агрегаты и
`update()`, как любое другое выражение, — `from hare.query.functions import Count, Lower, TruncMonth`.
Каждая даёт на любой базе результат одного и того же типа Python, который считает сама база (где базы
различаются, это сказано у функции ниже); если в SQLite такой функции нет, hare регистрирует свою на
каждом подключении (см.
[SQLite: функции](../dialects/sqlite/functions.ru.md)). Оконные функции описаны в разделе
[Агрегаты, группировка и оконные функции](aggregation.ru.md), функции только для PostgreSQL — в разделе
[Функции PostgreSQL](../dialects/postgresql/functions.ru.md). Функция, которой передано больше или
меньше аргументов, чем она принимает (`Upper("title", "x")`, `Coalesce("title")`), даёт `QueryError`
при создании.

## <a id="aggregates"></a>Агрегаты

| Класс | SQL | Примечания |
|---|---|---|
| `Count`/`Sum`/`Max`/`Min`/`Avg` | `COUNT`/`SUM`/`MAX`/`MIN`/`AVG` | Все принимают `distinct=` и `_filter=` — см. [`Count`/`Sum`/`Max`/`Min`/`Avg`](#count-sum-max-min) |
| `StdDev(field, sample=False)`/`Variance(field, sample=False)` | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP` | Показатель по всей совокупности или по выборке при `sample=True` — см. [`StdDev()`/`Variance()`](#stddev-variance) |

### <a id="count-sum-max-min"></a>`Count`/`Sum`/`Max`/`Min`/`Avg`

Все принимают `distinct: bool = False, _filter: Q | Exists | None = None` (`Exists(...)` — то же, что
`Q(Exists(...))`, всё остальное даёт `TypeError`; `Avg("x", distinct=True)` усредняет различные
значения). `_filter` — это `FILTER (WHERE ...)` агрегата: строка, не прошедшая его, не участвует в
агрегате (группа без подходящих строк даёт `0` для `Count` и `None` для остальных; `Sum` в ClickHouse
даёт собственный `0`). `Count` колонки
запрашиваемой модели, в которой нет NULL (`Count("id")`, `Count("pk")`), считает строки: PostgreSQL и
ClickHouse пишут его как `COUNT(*)` и не читают колонку; колонка, допускающая NULL, `distinct=True` и
колонка через связь считаются как есть. `_filter`, читающий агрегатное вычисляемое значение
(`Count("author_id", _filter=Q(n__gt=1))` рядом с `n=Count("id")`, в том числе внутри `Window(...)`),
даёт `FieldError`

### <a id="stddev-variance"></a>`StdDev()`/`Variance()`

Показатель по всей совокупности или по выборке при `sample=True`; `distinct=`/`_filter=` — как у
агрегатов выше. `NULL` пропускаются; без значений (меньше двух при `sample=True`) результат `None`. Тип —
как у `Avg`: `float` для целых и `float`, не округлённый `Decimal` для `DecimalField`. PostgreSQL считает
результат `Decimal` по своим правилам numeric; в SQLite и ClickHouse hare считает его точно, частное
отсекается (не округляется) до 20 знаков после запятой. В SQLite таких функций нет — hare регистрирует
свои.

```python
await Book.objects.all().annotate(
    book_count=Count("id"), average_price=Avg("price"),
).group_by("author_id").values("author_id", "book_count", "average_price")

# агрегат с условием:
await Book.objects.aggregate(in_print=Count("id", _filter=Q(in_print=True)))
await Author.objects.aggregate(with_books=Count("id", _filter=Exists(Book.objects.filter(author_id=OuterReference("id")))))
```

## <a id="date-functions"></a>Функции даты

| Класс | Результат |
|---|---|
| `Extract(field, lookup_name, *, tzinfo=None)` | часть как `int`: `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day`, `hour`, `minute`, `second`, `microsecond` |
| `ExtractYear`/`ExtractIsoYear`/`ExtractQuarter`/`ExtractMonth`/`ExtractWeek`/`ExtractWeekDay`/`ExtractIsoWeekDay`/`ExtractDay`/`ExtractHour`/`ExtractMinute`/`ExtractSecond(field, *, tzinfo=None)` | то же для одной части |
| `Trunc(field, trunc_type, *, tzinfo=None)` | значение, округлённое вниз до `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute` или `second`; `date`/`time` берут дату или время суток значения даты-времени |
| `TruncYear`/`TruncQuarter`/`TruncMonth`/`TruncWeek`/`TruncDay`/`TruncHour`/`TruncMinute`/`TruncSecond`/`TruncDate`/`TruncTime(field, *, tzinfo=None)` | то же для одного округления |
| `Now()` | момент выполнения команды как значение `DatetimeField` — `STATEMENT_TIMESTAMP()` в PostgreSQL, поэтому две команды одной транзакции читают разные моменты (`TransactionNow()` из `hare.dialects.postgresql.functions` — начало транзакции) |

Они работают на любой базе. `DatetimeField` читается в текущем часовом поясе — том, который задаёт для
блока кода [`Timezone.override()`](filters.ru.md#time-zone-semantics-on-datetimefield), иначе в
настроенном часовом поясе hare (в местном поясе системы при `use_timezone=False`) — так же, как в
операторах фильтра `__year`/`__date`: при `timezone="Europe/Moscow"` значение `2024-01-31 22:30Z` даёт
`ExtractDay` = `1`, а `TruncMonth` — 1 февраля, 00:00 по Москве. `tzinfo=` (имя IANA или `ZoneInfo`)
берёт части одного выражения в своём поясе — `Trunc("lent_at", "month", tzinfo="Europe/Moscow")`;
неизвестный пояс или постоянное смещение без имени IANA дают `ConfigurationError`, потому что база
переводит время по имени пояса. `Trunc` сохраняет тип значения — дата-время в настроенном поясе, `date`,
время суток со смещением, — кроме `TruncDate`/`TruncTime`. Неделя (`week`) начинается с понедельника;
`week_day` считает от 1 (воскресенье) до 7, `iso_week_day` — от 1 (понедельник) до 7. Часть или
округление, которых у значения нет (час у `DateField`, месяц у `TimeField`), дают `FieldError`, как и
аргумент, который не является датой, временем или датой-временем.

```python
await Loan.objects.annotate(month=TruncMonth("lent_at")).group_by("month").annotate(total=Count("id")).values("month", "total")
await Loan.objects.annotate(weekday=ExtractWeekDay("lent_at")).filter(weekday__in=[1, 7])
await Loan.objects.filter(due_at__lt=Now()).update(overdue=True)
```

## <a id="math-functions"></a>Математические функции

| Класс | Результат |
|---|---|
| `Abs(x)`, `Ceil(x)`, `Floor(x)`, `Sign(x)` | тип аргумента |
| `Mod(x, y)` | остаток так, как его даёт база (`Mod(-7, 3)` — `-1`), как у оператора `%`: целое для двух целых, точный `Decimal` с большим из двух масштабов, если участвует `Decimal` (`Mod(Decimal("12.345"), 7)` — `Decimal("5.345")`), `float`, если участвует `float` |
| `Power(x, y)`, `Sqrt(x)`, `Exp(x)`, `Ln(x)`, `Log(base, x)` | `Decimal`, если аргумент `Decimal`, иначе `float`. PostgreSQL считает результат `Decimal` в точном numeric, SQLite и ClickHouse — в double, поэтому последние цифры могут отличаться (`Power(Decimal("12.345"), 2)` — `152.399025` в PostgreSQL и `152.39902500000002` в SQLite и ClickHouse) |
| `Sin`, `Cos`, `Tan`, `Cot`, `ASin`, `ACos`, `ATan(x)`, `ATan2(y, x)`, `Degrees(x)`, `Radians(x)`, `Pi()` | `float` |
| `Random()` | `float` от 0 (включительно) до 1 (не включая), новый для каждой строки — по нему упорядочивает `order_by('?')` |
| `Round(field, precision=0)` | значение, округлённое до `precision` знаков после запятой, от 0 до 1000 — см. [`Round()`](#round) |

### <a id="round"></a>`Round()`

Значение, округлённое до `precision` знаков после запятой, от 0 до 1000. У результата `Decimal` ровно
`precision` знаков, целое остаётся целым, `float` — `float`, в том числе для константы или выражения
(`Round(Value(3), 1)` — `3`, `Round(F("qty") * 0.5, 1)` — `float`). Половина округляется по правилу
собственного `ROUND` базы: PostgreSQL округляет `Decimal` от нуля (`7.35` → `7.4`, `-7.35` → `-7.4`);
SQLite округляет число двойной точности от нуля, ClickHouse тоже — до одного знака и больше, а до целого
— к чётному (`2.5` → `2.0`); SQLite округляет `Decimal` точно по его хранимому тексту, тоже от нуля;
внутри SQL, который хранится в схеме (генерируемая колонка, выражение индекса), SQLite округляет
`Decimal` как число с плавающей точкой.

Каждый аргумент — имя поля, выражение или число (`Mod("quantity", 3)`, `Power(2, "level")`); логическое
значение или любая другая константа дают `QueryError`. В SQLite они работают через функции, которые hare
регистрирует в каждом соединении и которые дают то же, что собственные математические функции SQLite;
аргумент `NULL` даёт `NULL`. Аргумент вне области определения функции (`Sqrt` отрицательного числа,
`Ln(0)`, `ASin(2)`, `Mod(x, 0)`) или результат за пределами `float` (`Exp(1000)`) обрабатывает сама база:
PostgreSQL даёт `OperationalError` (`cannot take square root of a negative number`,
`value out of range: overflow`) и прерывает окружающую транзакцию, как любая неудачная команда; SQLite
даёт `NULL`; ClickHouse даёт `nan` или `inf` (целочисленный `Mod(x, 0)` — собственная ошибка
ClickHouse). hare этого не проверяет.

Функция, агрегат или арифметика над полем типа, к которому они не применяются, дают `FieldError` до
выполнения запроса на любой базе: математические функции,
`Round`, `StdDev`/`Variance` и `+ - * / % **` принимают числа (арифметика — ещё даты, моменты и интервалы
— см. [Арифметика с датой и временем](expressions.ru.md#date-time-arithmetic)), текстовые функции
(`Upper`, `Length`, `Trim` и т. д.) — текст, `Sum`/`Avg` — числа и интервалы (`Abs("name")`,
`Length("price")`, `Sum("in_print")` дают ошибку). `Max`/`Min` принимают любое поле: тип, для которого у
базы нет `min()`/`max()` (`jsonb`, диапазоны, `macaddr` и т. д. в PostgreSQL), отклоняет сама база, а SQLite
берёт наименьший и наибольший сохранённый текст JSON. ClickHouse сравнивал бы текст JSON-значения (`9` больше
`100`) — `Max`/`Min` JSON-поля или JSON-ключа дают там `FieldError`. Где преобразование
задумано, его делает `Cast()`: `Sum(Cast("in_print", IntField()))` считает истинные значения. Диапазон
PostgreSQL тоже не текст: `Upper("span")` даёт ошибку, а не выполняет `upper()` диапазона PostgreSQL — его
верхнюю границу. JSON-поле и JSON-ключ (`"data__title"`) для этих функций и для `Sum`/`Avg` не текст и не
число: каждая база прочитала бы сохранённый JSON по-своему — SQLite текст ключа вместе с кавычками, — поэтому
`Upper("data__title")` даёт ошибку, а `Upper(Cast("data__title", CharField(max_length=200)))` берёт значение
ключа как текст. Поле другого типа (двоичное, массив и т. п.) не проверяется.

```python
await Book.objects.annotate(rounded=Ceil(F("price") / 10) * 10)
await Branch.objects.annotate(distance=Sqrt(Power("x", 2) + Power("y", 2))).filter(distance__lt=5)
```

## <a id="text-functions"></a>Текстовые функции

| Класс | Результат |
|---|---|
| `Left(text, length)`, `Right(text, length)` | первые/последние символы; при отрицательной длине столько символов отбрасывается с другого конца |
| `Substr(text, position, length=None)` | с позиции, считая с 1, собственным `substr` базы: PostgreSQL считает позиции до 1 пустыми, а отрицательную длину отвергает; SQLite считает отрицательную позицию с конца, а при отрицательной длине берёт символы перед позицией; ClickHouse (`substringUTF8`) отвергает позицию 0, считает отрицательную позицию с конца, а при отрицательной длине оставляет символы в конце |
| `StrIndex(text, substring)` | позиция первого вхождения, считая с 1, или `0`, если вхождения нет, — `int` |
| `Replace(text, old, new="")`, `Repeat(text, count)`, `Reverse(text)` | текст |
| `LPad(text, length, fill=" ")`, `RPad(...)` | дополнено до `length`; более длинный текст обрезается до неё |
| `LTrim(text)`, `RTrim(text)` | без пробелов в начале/конце |
| `Chr(code)`, `Ord(text)` | символ по коду; код первого символа — собственные `chr`/`ascii` базы (`Ord("")` в SQLite — `NULL`; `Chr` в ClickHouse отвергает только код, которому не соответствует ни один символ, `Chr(0)` — `\x00`) |
| `MD5`, `SHA1`, `SHA224`, `SHA256`, `SHA384`, `SHA512(text)` | хэш текста в UTF-8 шестнадцатеричными цифрами в нижнем регистре |
| `Trim(text)`, `Lower(text)`, `Upper(text)` | текст без пробелов по краям, в нижнем регистре, в верхнем регистре — собственные `UPPER()`/`LOWER()` SQLite меняют регистр только у букв ASCII (`Upper("café")` → `CAFé`), ClickHouse — через `upperUTF8()`/`lowerUTF8()`, PostgreSQL — по своим правилам |
| `Length(text)` | число символов — `int` |
| `Concat(field, *args)` | аргументы, соединённые в один текст, — `CONCAT` — см. [`Concat()`](#concat) |

### <a id="concat"></a>`Concat()`

Аргументы, соединённые в один текст, — `CONCAT`. Текст колонки — собственный текст её базы: логическая
колонка — `true` в PostgreSQL, `1` в SQLite и результат `toString()` в ClickHouse, `float` — `-3` в
PostgreSQL и `-3.0` в SQLite; JSON — собственный текст базы (см. `Cast` ниже). Константа Python пишется
своим текстом Python: логическое значение — как `true`/`false`, `float` — через `repr()`
(`1000000000000000.0`), `Decimal` — со всеми своими цифрами и без степени (`1.50`),
`date`/`datetime`/`time` — как текст ISO (дата-время с поясом — в UTC: `2020-01-02 00:04:05+00:00`),
`uuid.UUID` — своим текстом; дата-время без пояса (`use_timezone=False`) пишется по местному часовому
поясу машины. Значение
`timedelta` пишется числом микросекунд. Значение `bytes` даёт `QueryError`. В SQLite записывается через
`||`, поэтому работает на любой поддерживаемой версии SQLite.

Первый аргумент — имя поля, выражение или константа; следующий строковый аргумент — это текст
(`Replace("title", "-", " ")`), поле туда передавайте как `F("other")`. Аргумент `NULL` даёт `NULL`,
кроме `Concat()`, который его пропускает (`Concat("title", F("subtitle"))` при `NULL` в subtitle — это
title) на любой базе. `SHA1` в PostgreSQL требует расширения `pgcrypto`. У длины, позиции и числа
повторов в hare нет 32-битного предела; собственные `LEFT`, `RIGHT`, `SUBSTR`, `LPAD`, `RPAD` и `REPEAT`
PostgreSQL берут 32-битное число и отклоняют большее с `OperationalError`. Текстовая константа там, где
ожидается целое число (`Left("title", "3")`), и числовая константа там, где ожидается текст
(`Replace("title", 1)`), дают `QueryError`; выражение другого типа (`Left("title", F("isbn"))`) оставлено
на усмотрение базы. Результат длиннее, чем база хранит в одном значении, даёт её собственную ошибку:
PostgreSQL — около гигабайта (`requested length too large`); в SQLite hare отказывает, если результат
длиннее, чем SQLite хранит в одном значении (1e9 байт), ещё до его построения (`string or blob too big`);
ClickHouse отказывает при числе повторов `Repeat` или длине `LPad`/`RPad` больше 1 000 000. `Left`,
`Right`, `LPad`, `RPad`, `Repeat`, `Reverse` и хэши в SQLite — собственные функции hare.

## <a id="comparison-functions"></a>Функции сравнения и преобразования

| Класс | Результат |
|---|---|
| `Cast(expression, output_field)` | значение, преобразованное к типу `output_field` — целое, `float`, `Decimal`, `CharField`/`TextField`, логическое значение, дата, дата-время или время (любое другое поле даёт `FieldError`) |
| `Coalesce(field, *default_values)` | первый аргумент, который не `NULL`, — `COALESCE`; тип определяют все аргументы, как у [`Case`](expressions.ru.md#case-when) |
| `Greatest(*values)`, `Least(*values)` | наибольшее/наименьшее из двух и более значений; `NULL` среди них PostgreSQL и ClickHouse пропускают, а SQLite даёт `NULL` |
| `NullIf(expression, other)` | `NULL`, если значение равно `other`, иначе само значение |
| `Collate(expression, collation)` | текст, который сравнивается и сортируется по правилу сравнения (collation) базы |

`Cast` преобразует так, как преобразует база, и отказывает там, где база молча подменила бы значение другим.
Приведение к целому в PostgreSQL округляет (`Decimal` — от нуля, `float` — к ближайшему чётному: `2.5` → `2`,
`2.50` → `3`), а в SQLite и ClickHouse отбрасывает дробную часть (`2.5` → `2`); значение вне диапазона
целевого поля (16, 32 или 64 бита) даёт `OperationalError`. Целевой `Decimal` округляет от нуля до своих
знаков и даёт ошибку, если целых цифр больше, чем он вмещает; `float` становится `Decimal` по своим кратчайшим
цифрам (`2.675` → `2.68` при 2 знаках). Текст, не являющийся значением этого типа, даёт ошибку (`"abc"` — это
не `0`; в ClickHouse число, дополненное пробелами, `" 42 "`, тоже даёт ошибку), как и несуществующая дата
(`"2023-02-29"`), текст, не называющий дату целиком (`"12"`, `"2024-01"`), а на ClickHouse — невозможное
время (`"25:00"`, `"12:60"`: сервер перенёс бы его дальше), дата вне хранимых им 1900-01-01 – 2299-12-31
(сервер взял бы ближайшую) и время суток из текста, который не начинается с часов и минут (`""`, `"-1"`).
Логическое значение читается из
текста как `true`, `false`, `1`, `0` (в любом регистре, без
пробелов по краям) в SQLite, через `toBool()` в ClickHouse (`yes`, `no`, `on`, `off` и т. д.) и по собственным
словам PostgreSQL. Любое другое преобразование — собственное преобразование базы (логическое значение во
`float`, дата в число). Приведение к тексту даёт собственный текст базы: SQLite — `CAST(x AS TEXT)` (`float`
`2.0`, 15 значащих цифр — с SQLite 3.53 17, логическое значение `1`/`0`), ClickHouse — `toString()` (`1e308`,
`-inf`, `Decimal` без хвостовых нулей, момент `2024-01-02 03:04:05.678900`), PostgreSQL — свой; целевой
`CharField(max_length=n)` в SQLite и ClickHouse ничего не обрезает. JSON становится собственным текстом базы
(в PostgreSQL — текст `jsonb` `{"a": 1, "bb": [1, 2.5]}`, в SQLite — хранимый текст без пробелов, в ClickHouse
— свой; см. [его отличия](../dialects/clickhouse/differences.ru.md#json)). Дата-время с поясом читается в UTC,
а без пояса (`use_timezone=False`) — как время на часах. `NULL` остаётся `NULL`. SQLite не может хранить NaN,
поэтому преобразование текста `"NaN"` в число там даёт ошибку.

`Greatest`/`Least` принимают имена полей, выражения или константы
(`Greatest("updated_at", "created_at")`, `Least("price", 100)`). Числа сравниваются как числа, и
результат получает их общий тип. `other` у `NullIf` — константа или
выражение; строка там считается текстом. Имя в `Collate` — простое имя (буквы, цифры, `_`, `-`, `.`,
`@`), например `NOCASE` в SQLite или `C`/`und-x-icu` в PostgreSQL.

## <a id="json-functions"></a>Функции JSON

`JSONObject(**fields)` строит объект JSON из данных ключей и значений и раскодируется как `dict`:
`JSONObject(title="title", total=F("price") * 2)`. Строковое значение — имя поля; любое другое значение —
выражение или константа. Каждая база записывает значение сама. PostgreSQL — так, как его хранит `jsonb`:
логическое значение — как `true`/`false`, `Decimal` и `float` — как число (целочисленный `float` — как целое,
`2`), дата-время с поясом — текстом ISO в UTC (`2020-01-02T03:04:05.5+00:00`), время — со смещением
(`12:30:00+00`), байты — как `\x` и шестнадцатеричные цифры. SQLite — через `json_object()`: логическое
значение как `1`/`0`, `float` с 15 значащими цифрами (с SQLite 3.53 — с 17), момент или время — своим хранимым
текстом, байты — шестнадцатеричными цифрами в верхнем регистре, `Decimal` — числом. ClickHouse — через
`toJSONString()`: `Decimal` без хвостовых нулей, `DateTime64` как `2024-02-29 13:45:06.789000`, NaN как null,
байты — теми шестнадцатеричными цифрами, что хранит колонка. Поле JSON или вложенный `JSONObject` — вложенный
JSON везде. Ключи объекта возвращаются в порядке базы — в PostgreSQL в порядке самого `jsonb` (сначала более
короткие). Ключ с нулевым байтом даёт `QueryError`. Фильтруйте такое вычисляемое значение операторами
`JSONField` (`summary__filter={"total__gt": 10}`, `summary__contains={...}`) или читайте путь внутри него
через `F("summary__total")`.

```python
await Book.objects.annotate(card=JSONObject(title="title", author=JSONObject(name="author__name"))).values_list("card")
```

`JSONArray(*values)` строит массив JSON из данных значений по порядку и раскодируется как `list`:
`JSONArray("title", F("price") * 2, 5)` — `JSONB_BUILD_ARRAY` в PostgreSQL, `json_array` в SQLite. Строка
— имя поля; значения записываются как в `JSONObject`, вложенный `JSONArray`/`JSONObject` — как вложенный
JSON; `JSONArray()` — это `[]`. Агрегат тоже может быть элементом (`JSONArray(Count("id"), Sum("price"))`
для каждой группы).
