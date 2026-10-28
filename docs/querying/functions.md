# Database functions

The functions of `hare.query.functions` go into `annotate()`, `filter()`, `order_by()`, aggregates and
`update()` like any other expression — `from hare.query.functions import Count, Lower, TruncMonth`. Each
one gives a result of the same Python type on every database, computed by the database itself (where the
databases differ, a function says so below) — but for the statistics `Avg`, `StdDev` and `Variance`, an
exact `Decimal` where the database computes one and a `float` elsewhere (see
[Result type](expressions.md#result-type)); where SQLite has no such function, hare registers its own on
every connection (see [SQLite: functions](../dialects/sqlite/functions.md)).
Window functions are on [Aggregates, grouping and window functions](aggregation.md), the PostgreSQL-only
ones on [PostgreSQL functions](../dialects/postgresql/functions.md). A function given more or fewer
arguments than it takes (`Upper("title", "x")`, `Coalesce("title")`) raises `QueryError` when it is built.

## <a id="aggregates"></a>Aggregates

| Class | SQL | Notes |
|---|---|---|
| `Count`/`Sum`/`Max`/`Min`/`Avg` | `COUNT`/`SUM`/`MAX`/`MIN`/`AVG` | All take `distinct=` and `_filter=` — see [`Count`/`Sum`/`Max`/`Min`/`Avg`](#count-sum-max-min) |
| `StdDev(field, sample=False)`/`Variance(field, sample=False)` | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP` | The population statistic, or the sample one with `sample=True` — see [`StdDev()`/`Variance()`](#stddev-variance) |

### <a id="count-sum-max-min"></a>`Count`/`Sum`/`Max`/`Min`/`Avg`

All take `distinct: bool = False, _filter: Q | Exists | None = None` (an `Exists(...)` is the same as
`Q(Exists(...))`, anything else raises `TypeError`; `Avg("x", distinct=True)` averages the distinct values).
`_filter` is the aggregate's `FILTER (WHERE ...)`: a row failing it is left out of the aggregate (a group
with no matching row gives `0` for `Count`, `None` for the others; `Sum` on ClickHouse gives its own `0` for a
field that isn't nullable).
`Count` of a column of the queried model that holds no NULL (`Count("id")`, `Count("pk")`) counts the rows:
PostgreSQL and ClickHouse write it `COUNT(*)` and don't read the column; a nullable column, `distinct=True`
and a column across a relation are counted as they are. A `_filter` reading an aggregate annotation
(`Count("author_id", _filter=Q(n__gt=1))` next to `n=Count("id")`, also inside `Window(...)`) raises
`FieldError`

### <a id="stddev-variance"></a>`StdDev()`/`Variance()`

The population statistic, or the sample one with `sample=True`; `distinct=`/`_filter=` as for the
aggregates above. `NULL`s are skipped; with no value (with fewer than two for `sample=True`) the result
is `None`. Typed like `Avg`: a `float` for integers and floats; for a `DecimalField` an unrounded
`Decimal` PostgreSQL computes by its own numeric rules, and a `float` on ClickHouse, which computes the
statistics of decimals as floats (`Features.computes_decimal_statistics_exactly`). SQLite has no such
functions — hare registers its own.

```python
await Book.objects.all().annotate(
    book_count=Count("id"), average_price=Avg("price"),
).group_by("author_id").values("author_id", "book_count", "average_price")

# a conditional aggregate:
await Book.objects.aggregate(in_print=Count("id", _filter=Q(in_print=True)))
await Author.objects.aggregate(with_books=Count("id", _filter=Exists(Book.objects.filter(author_id=OuterReference("id")))))
```

## <a id="date-functions"></a>Date functions

| Class | Result |
|---|---|
| `Extract(field, lookup_name, *, tzinfo=None)` | the part as an `int`: `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day`, `hour`, `minute`, `second`, `microsecond` |
| `ExtractYear`/`ExtractIsoYear`/`ExtractQuarter`/`ExtractMonth`/`ExtractWeek`/`ExtractWeekDay`/`ExtractIsoWeekDay`/`ExtractDay`/`ExtractHour`/`ExtractMinute`/`ExtractSecond(field, *, tzinfo=None)` | the same, for one part |
| `Trunc(field, trunc_type, *, tzinfo=None)` | the value truncated to `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute` or `second`; `date`/`time` take a datetime's date or time of day |
| `TruncYear`/`TruncQuarter`/`TruncMonth`/`TruncWeek`/`TruncDay`/`TruncHour`/`TruncMinute`/`TruncSecond`/`TruncDate`/`TruncTime(field, *, tzinfo=None)` | the same, for one truncation |
| `Now()` | the moment of the statement, as a `DatetimeField` value — `STATEMENT_TIMESTAMP()` on PostgreSQL, so two statements of one transaction read different moments (`TransactionNow()` in `hare.dialects.postgresql.functions` is the start of the transaction) |

They work on every database. A `DatetimeField` is read in the current time zone — the one
[`Timezone.override()`](filters.md#time-zone-semantics-on-datetimefield) sets for a block of code, else
the zone `Hare.init()` configures (the local system zone under `use_timezone=False`) — as the
`__year`/`__date` lookups are: with `timezone="Europe/Moscow"`, `2024-01-31 22:30Z` gives `ExtractDay` =
`1` and `TruncMonth` = February 1, 00:00 Moscow time. `tzinfo=` (an IANA name or a `ZoneInfo`) takes one
expression's parts in its own zone instead — `Trunc("lent_at", "month", tzinfo="Europe/Moscow")`; an
unknown zone, or a fixed offset without an IANA name, raises `ConfigurationError`, since the database
converts by the zone's name. `Trunc` keeps the value's type — a datetime in the configured zone, a
`date`, a time of day with its offset — except `TruncDate`/`TruncTime`. `week` starts on Monday;
`week_day` counts 1 (Sunday) to 7, `iso_week_day` 1 (Monday) to 7. A part or type the value has none of
(the hour of a `DateField`, the month of a `TimeField`) raises `FieldError`, and so does an argument that
isn't a date, time or datetime.

```python
await Loan.objects.annotate(month=TruncMonth("lent_at")).group_by("month").annotate(total=Count("id")).values("month", "total")
await Loan.objects.annotate(weekday=ExtractWeekDay("lent_at")).filter(weekday__in=[1, 7])
await Loan.objects.filter(due_at__lt=Now()).update(overdue=True)
```

## <a id="math-functions"></a>Math functions

| Class | Result |
|---|---|
| `Abs(x)`, `Ceil(x)`, `Floor(x)`, `Sign(x)` | the argument's type |
| `Mod(x, y)` | the remainder as the database gives it (`Mod(-7, 3)` is `-1`), as the `%` operator: an integer of two integers, an exact Decimal with the larger of the two scales when a Decimal is involved (`Mod(Decimal("12.345"), 7)` is `Decimal("5.345")`), a float when a float is |
| `Power(x, y)`, `Sqrt(x)`, `Exp(x)`, `Ln(x)`, `Log(base, x)` | a `Decimal` when an argument is one, a float otherwise. PostgreSQL computes a `Decimal` result in exact numeric; ClickHouse computes it in double, so its last digits can differ (`Power(Decimal("12.345"), 2)` is `152.399025` on PostgreSQL and `152.39902500000002` on ClickHouse) |
| `Sin`, `Cos`, `Tan`, `Cot`, `ASin`, `ACos`, `ATan(x)`, `ATan2(y, x)`, `Degrees(x)`, `Radians(x)`, `Pi()` | a float |
| `Random()` | a float from 0 (included) to 1 (excluded), new for every row — `order_by('?')` orders by it |
| `Round(field, precision=0)` | the value rounded to `precision` decimal places, 0 to 1000 — see [`Round()`](#round) |

### <a id="round"></a>`Round()`

The value rounded to `precision` decimal places, 0 to 1000. A Decimal result has exactly `precision`
places, an integer stays an integer, a float a float — also for a literal or an expression
(`Round(Value(3), 1)` is `3`, `Round(F("qty") * 0.5, 1)` a float). A tie is rounded by the database's own
`ROUND`: PostgreSQL rounds a Decimal away from zero (`7.35` → `7.4`, `-7.35` → `-7.4`); SQLite rounds a
double half away from zero, and ClickHouse too to one place or more, but to a whole number half to even
(`2.5` → `2.0`).

Every argument is a field name, an expression or a number literal (`Mod("copies", 3)`,
`Power(2, "edition")`); a bool or any other literal raises `QueryError`. On SQLite they run through
functions hare registers on each connection, which give what SQLite's own math functions give; a NULL
argument gives NULL. An argument outside a function's domain (`Sqrt` of a negative number, `Ln(0)`,
`ASin(2)`, `Mod(x, 0)`) or a result beyond a float (`Exp(1000)`) is each database's own: PostgreSQL raises
`OperationalError` (`cannot take square root of a negative number`, `value out of range: overflow`) and
aborts the surrounding transaction, as any failed statement does; SQLite gives NULL; ClickHouse gives
`nan` or `inf` (an integer `Mod(x, 0)` is ClickHouse's own error). hare doesn't check these.

A function, an aggregate or arithmetic over a field of a type it doesn't apply to raises `FieldError`
before the query runs, on every database: a math function, `Round`,
`StdDev`/`Variance` and `+ - * / % **` take numbers (the arithmetic also dates, moments and durations —
see [Date/time arithmetic](expressions.md#date-time-arithmetic)), a text function (`Upper`, `Length`,
`Trim`, ...) text, `Sum`/`Avg` numbers and durations (`Abs("name")`, `Length("price")`,
`Sum("in_print")` raise). `Max`/`Min` take any field the database orders: a type it has no `min()`/`max()`
of (PostgreSQL's `boolean`, `uuid`, `jsonb`, `macaddr`, ranges and multiranges, `hstore`, `ltree`,
`tsvector`, geometries, `vector`) and a field no query orders by (ClickHouse's `DynamicField`,
`VariantField`) raise `UnSupportedError` before any SQL, and SQLite takes the least and the greatest stored
text of JSON. ClickHouse would compare a JSON value's text (`9` above `100`) —
`Max`/`Min` of a JSON field or key raise `FieldError` there. `Cast()` converts the value where that is meant:
`Sum(Cast("in_print", IntField()))` counts the true values. A PostgreSQL range is no text either:
`Upper("span")` raises rather than run PostgreSQL's `upper()` of a range, its upper bound. A JSON field and a
JSON key (`"data__title"`) are neither text nor a number to these functions and `Sum`/`Avg`: each database
would read the stored JSON its own way — SQLite a key's text with its quotes — so `Upper("data__title")`
raises, and `Upper(Cast("data__title", CharField(max_length=200)))` takes the key's value as text. A field of
another type (binary, an array, ...) isn't checked.

`GeneratedField` uses its `output_field` for these type checks and ordering rules. On ClickHouse,
computed JSON values have the same `Min`/`Max` and `Greatest`/`Least` restrictions as ordinary JSON fields.

```python
await Book.objects.annotate(rounded=Ceil(F("price") / 10) * 10)
await Branch.objects.annotate(distance=Sqrt(Power("x", 2) + Power("y", 2))).filter(distance__lt=5)
```

## <a id="text-functions"></a>Text functions

| Class | Result |
|---|---|
| `Left(text, length)`, `Right(text, length)` | the first/last characters; a negative length drops that many from the other end |
| `Substr(text, position, length=None)` | from a 1-based position, the database's own `substr`: PostgreSQL counts positions before 1 as empty and raises on a negative length; SQLite counts a negative position from the end and takes the characters before the position for a negative length; ClickHouse (`substringUTF8`) refuses position 0, counts a negative position from the end and leaves characters off the end for a negative length |
| `StrIndex(text, substring)` | the 1-based position of the first occurrence, `0` without one — an `int` |
| `Replace(text, old, new="")`, `Repeat(text, count)`, `Reverse(text)` | text |
| `LPad(text, length, fill=" ")`, `RPad(...)` | filled to `length`; a longer text is cut to it |
| `LTrim(text)`, `RTrim(text)` | leading/trailing spaces removed |
| `Chr(code)`, `Ord(text)` | the character of a code point; the first character's code point — each database's own `chr`/`ascii` (`Ord("")` is NULL on SQLite; ClickHouse's `Chr` refuses only a code point no character has, `Chr(0)` is `\x00`) |
| `MD5`, `SHA1`, `SHA224`, `SHA256`, `SHA384`, `SHA512(text)` | the lowercase hex digest of the UTF-8 text |
| `Trim(text)`, `Lower(text)`, `Upper(text)` | the text trimmed of spaces at both ends, in lower case, in upper case — SQLite's own `UPPER()`/`LOWER()` map the ASCII letters only (`Upper("café")` → `CAFé`), ClickHouse with `upperUTF8()`/`lowerUTF8()`, PostgreSQL by its own rules |
| `Length(text)` | the number of characters — an `int` |
| `Concat(field, *args)` | the arguments joined into one text — `CONCAT` — see [`Concat()`](#concat) |

### <a id="concat"></a>`Concat()`

The arguments joined into one text — `CONCAT`. A column's text is its database's own: a boolean column
is `true` on PostgreSQL, `1` on SQLite and `toString()`'s on ClickHouse, a float `-3` on PostgreSQL and
`-3.0` on SQLite; JSON is the database's own text (see `Cast` below). A Python literal is written as its
Python text: a bool as `true`/`false`, a float by `repr()` (`1000000000000000.0`), a `Decimal` with every
digit it has and no exponent (`1.50`), a `date`/`datetime`/`time` as ISO text (an aware datetime in UTC:
`2020-01-02 00:04:05+00:00`), a `uuid.UUID` as its text; a naive datetime (`use_timezone=False`) is
written at the machine's local zone. A `timedelta` literal is written as its microseconds. A `bytes`
literal raises `QueryError`. On SQLite it is written with `||`, so every supported SQLite version runs it.

The first argument is a field name, an expression or a literal; a later string argument is text
(`Replace("title", "-", " ")`) — pass a field there as `F("other")`. A NULL argument gives NULL, except
in `Concat()`, which leaves it out (`Concat("title", F("subtitle"))` of a NULL subtitle is the title) on
every database. `SHA1` needs the `pgcrypto` extension on PostgreSQL. A length, a position or a count has
no 32-bit limit in hare; PostgreSQL's own `LEFT`, `RIGHT`, `SUBSTR`, `LPAD`, `RPAD` and `REPEAT` take a
32-bit one and refuse a larger one with `OperationalError`. Text given as a literal where an integer goes
(`Left("title", "3")`) and a number literal where text goes (`Replace("title", 1)`) raise `QueryError`;
an expression of another type (`Left("title", F("isbn"))`) is left to the database. A result longer than
the database holds in one value raises its own error: PostgreSQL about a gigabyte (`requested length too
large`); on SQLite hare refuses a result longer than SQLite holds in one value (1e9 bytes) before it is
built (`string or blob too big`); ClickHouse refuses a `Repeat` count or an `LPad`/`RPad` length above
1,000,000. `Left`, `Right`, `LPad`, `RPad`, `Repeat`, `Reverse` and the digests are hare's own functions
on SQLite.

## <a id="comparison-functions"></a>Comparison and conversion functions

| Class | Result |
|---|---|
| `Cast(expression, output_field)` | the value converted to `output_field`'s type — an integer, float, Decimal, `CharField`/`TextField`, boolean, date, datetime or time field (any other raises `FieldError`) |
| `Coalesce(field, *default_values)` | the first argument that isn't NULL — `COALESCE`; typed by all its arguments, as on [`Case`](expressions.md#case-when) |
| `Greatest(*values)`, `Least(*values)` | the greatest/least of two or more values; a NULL among them is skipped on PostgreSQL and ClickHouse and gives NULL on SQLite |
| `NullIf(expression, other)` | NULL when the value equals `other`, the value otherwise |
| `Collate(expression, collation)` | the text compared and sorted by a database collation |

`Coalesce()` checks a `GeneratedField` through its `output_field` too: a fractional fallback beside
a computed integer is kept as a fraction, just as beside an ordinary integer column.

`Cast` is the database's own conversion — PostgreSQL's and SQLite's `CAST`, ClickHouse's conversion
functions; SQLite has no date types, so a date or a time is read from its stored text in UTC by hare. A cast
to an integer rounds on PostgreSQL (a Decimal half away from zero, a float half to even: `2.5` → `2`,
`2.50` → `3`) and cuts the fraction off on SQLite and ClickHouse (`2.5` → `2`); a value past the target
field's range raises on PostgreSQL, is kept on SQLite and wraps around on ClickHouse. A Decimal target
rounds half away from zero to its places on PostgreSQL, raising on more integer digits than it holds;
ClickHouse cuts the places off, a float converted as the double it is (`2.675` → `2.67` at 2 places). Text
that isn't a value of the type raises on PostgreSQL, is `NULL` on ClickHouse (a number padded with spaces,
`" 42 "`, too) and on SQLite the number its text starts with (`"4x"` → `4`, `"1e3"` to an integer → `1`),
`0` when it starts with none. A text date is read by PostgreSQL's own parser, as ISO text on SQLite
and by `parseDateTime64BestEffort()` on ClickHouse, which completes a date missing a part (`"2024-01"`),
rolls an impossible clock over (`"25:00"`) and reads a year outside 1900 to 2299 as the nearest day it
holds; a day no calendar has (`"2023-02-29"`) raises everywhere. A boolean is PostgreSQL's own words,
SQLite's value read as a number — true when it isn't `0` — and ClickHouse's `toBool()` (`yes`, `no`, `on`,
`off`, ...). Any other conversion is the database's own (a boolean to a float, a date to a number). A cast to
text is the database's own text: SQLite `CAST(x AS TEXT)` (a float `2.0`, 15 significant digits — 17 from
SQLite 3.53, a boolean `1`/`0`), ClickHouse `toString()` (`1e308`, `-inf`, a Decimal without trailing zeros,
a moment `2024-01-02 03:04:05.678900`), PostgreSQL its own; a `CharField(max_length=n)` target cuts nothing
on SQLite and ClickHouse. JSON becomes the database's own text (PostgreSQL's `jsonb` text `{"a": 1, "bb": [1,
2.5]}`, SQLite the stored text without spaces, ClickHouse its own — see [its
differences](../dialects/clickhouse/differences.md#json)). An aware datetime is read in UTC and a naive one
(`use_timezone=False`) as its wall clock. NULL stays NULL.

`Greatest`/`Least` take field names, expressions or literals (`Greatest("updated_at", "created_at")`,
`Least("price", 100)`). Numbers compare as numbers and the result has their common type. `NullIf`'s `other`
is a literal or an expression — a string there is text. A `Collate` name is a plain name (letters, digits,
`_`, `-`, `.`, `@`), for example `NOCASE` on SQLite or `C`/`und-x-icu` on PostgreSQL.

## <a id="json-functions"></a>JSON functions

`JSONObject(**fields)` builds a JSON object of the given keys and values, decoded as a `dict`:
`JSONObject(title="title", total=F("price") * 2)`. A string value is a field name; any other value is an
expression or a literal. Each database writes a value itself. PostgreSQL writes it as `jsonb` holds it: a
boolean as `true`/`false`, a Decimal and a float as a number (an integral float as an integer, `2`), an aware
datetime as ISO text in UTC (`2020-01-02T03:04:05.5+00:00`), a time with its offset (`12:30:00+00`), bytes as
`\x` and hex. SQLite uses `json_object()`: a boolean as `1`/`0`, a float with 15 significant digits (17 from
SQLite 3.53), a moment or time as its stored text, bytes as uppercase hex. ClickHouse
uses `toJSONString()`: a Decimal without trailing zeros, a `DateTime64` as `2024-02-29 13:45:06.789000`, NaN
as null, bytes as the hex the column holds. A JSON field or a nested `JSONObject` is nested JSON everywhere.
The object's keys come back in the database's order — on PostgreSQL `jsonb`'s own (shorter keys first). A key
holding a null byte raises `QueryError`. Filter such an annotation with the `JSONField` lookups
(`summary__filter={"total__gt": 10}`, `summary__contains={...}`) or read a path of it with
`F("summary__total")`.

```python
await Book.objects.annotate(card=JSONObject(title="title", author=JSONObject(name="author__name"))).values_list("card")
```

`JSONArray(*values)` builds a JSON array of the given values in order, decoded as a `list`:
`JSONArray("title", F("price") * 2, 5)` — `JSONB_BUILD_ARRAY` on PostgreSQL, `json_array` on SQLite. A
string is a field name; values are written as in `JSONObject`, a nested `JSONArray`/`JSONObject` as
nested JSON; `JSONArray()` is `[]`. An aggregate works as an item (`JSONArray(Count("id"), Sum("price"))`
per group).
