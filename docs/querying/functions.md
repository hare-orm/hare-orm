# Database functions

The functions of `hare.query.functions` go into `annotate()`, `filter()`, `order_by()`, aggregates and
`update()` like any other expression — `from hare.query.functions import Count, Lower, TruncMonth`. Each
one gives the same result, of the same Python type, on every backend; where SQLite has no such function,
hare registers its own on every connection. Window functions are on
[Aggregates, grouping and window functions](aggregation.md), the PostgreSQL-only ones on
[PostgreSQL functions](../dialects/postgresql/functions.md).

## <a id="aggregates"></a>Aggregates

| Class | SQL | Notes |
|---|---|---|
| `Count`/`Sum`/`Max`/`Min`/`Avg` | `COUNT`/`SUM`/`MAX`/`MIN`/`AVG` | All take <code>distinct: bool = False, &#95;filter: Q &#124; Exists &#124; None = None</code> (an `Exists(...)` is the same as `Q(Exists(...))`, anything else raises `TypeError`; `Avg("x", distinct=True)` averages the distinct values). `_filter` is the aggregate's `FILTER (WHERE ...)`: a row failing it is left out of the aggregate (a group with no matching row gives `0` for `Count`, `None` for the others). `Count` of a column of the queried model that holds no NULL (`Count("id")`, `Count("pk")`) counts the rows: PostgreSQL and ClickHouse write it `COUNT(*)` and don't read the column; a nullable column, `distinct=True` and a column across a relation are counted as they are. Like Django, a `_filter` reading an aggregate annotation (`Count("dept_id", _filter=Q(n__gt=1))` next to `n=Count("id")`, also inside `Window(...)`) raises `FieldError` |
| `StdDev(field, sample=False)`/`Variance(field, sample=False)` | `STDDEV_POP`/`STDDEV_SAMP`/`VAR_POP`/`VAR_SAMP` | The population statistic, or the sample one with `sample=True`; `distinct=`/`_filter=` as for the aggregates above. `NULL`s are skipped; with no value (with fewer than two for `sample=True`) the result is `None`. Typed like `Avg`: a `float` for integers and floats, an unrounded `Decimal` for a `DecimalField`. SQLite has no such functions — hare registers its own, with the same results. |

```python
await Employee.objects.all().annotate(
    emp_count=Count("id"), avg_salary=Avg("salary"),
).group_by("department_id").values("department_id", "emp_count", "avg_salary")

# a conditional aggregate:
await Team.objects.annotate(active_count=Count("id", _filter=Q(status="active")))
await Team.objects.aggregate(with_members=Count("id", _filter=Exists(Member.objects.filter(team_id=OuterReference("id")))))
```

## <a id="date-functions"></a>Date functions

| Class | Result |
|---|---|
| `Extract(field, lookup_name, *, tzinfo=None)` | the part as an `int`: `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day`, `hour`, `minute`, `second`, `microsecond` |
| `ExtractYear`/`ExtractIsoYear`/`ExtractQuarter`/`ExtractMonth`/`ExtractWeek`/`ExtractWeekDay`/`ExtractIsoWeekDay`/`ExtractDay`/`ExtractHour`/`ExtractMinute`/`ExtractSecond(field, *, tzinfo=None)` | the same, for one part |
| `Trunc(field, trunc_type, *, tzinfo=None)` | the value truncated to `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute` or `second`; `date`/`time` take a datetime's date or time of day |
| `TruncYear`/`TruncQuarter`/`TruncMonth`/`TruncWeek`/`TruncDay`/`TruncHour`/`TruncMinute`/`TruncSecond`/`TruncDate`/`TruncTime(field, *, tzinfo=None)` | the same, for one truncation |
| `Now()` | the moment of the statement, as a `DatetimeField` value — `STATEMENT_TIMESTAMP()` on PostgreSQL, so two statements of one transaction read different moments (`TransactionNow()` in `hare.dialects.postgresql.functions` is the start of the transaction) |

They work on every backend. A `DatetimeField` is read in the current time zone — the one
[`Timezone.override()`](filters.md#time-zone-semantics-on-datetimefield) sets for a block of
code, else Hare's configured one (the local system zone under `use_timezone=False`) — as the
`__year`/`__date` lookups are: with `timezone="Europe/Moscow"`, `2024-01-31 22:30Z` gives
`ExtractDay` = `1` and `TruncMonth` = February 1, 00:00 Moscow time. `tzinfo=` (an IANA name or a
`ZoneInfo`) takes one expression's parts in its own zone instead —
`Trunc("created_at", "month", tzinfo="Europe/Moscow")`; an unknown zone, or a fixed offset without an
IANA name, raises `ConfigurationError`, since the database converts by the zone's name. `Trunc`
keeps the value's type — a datetime in the configured zone, a `date`, a time of day with its offset —
except `TruncDate`/`TruncTime`. `week` starts on Monday; `week_day`
counts 1 (Sunday) to 7, `iso_week_day` 1 (Monday) to 7. A part or type the value has none of (the
hour of a `DateField`, the month of a `TimeField`) raises `FieldError`, and so does an argument that
isn't a date, time or datetime.

```python
await Order.objects.annotate(month=TruncMonth("created_at")).group_by("month").annotate(total=Count("id")).values("month", "total")
await Order.objects.annotate(weekday=ExtractWeekDay("created_at")).filter(weekday__in=[1, 7])
await Session.objects.filter(expires_at__lt=Now()).delete()
```

## <a id="math-functions"></a>Math functions

| Class | Result |
|---|---|
| `Abs(x)`, `Ceil(x)`, `Floor(x)`, `Sign(x)` | the argument's type |
| `Mod(x, y)` | the remainder with `x`'s sign (`Mod(-7, 3)` is `-1`); an integer of two integers |
| `Power(x, y)`, `Sqrt(x)`, `Exp(x)`, `Ln(x)`, `Log(base, x)` | a `Decimal` when an argument is one, a float otherwise |
| `Sin`, `Cos`, `Tan`, `Cot`, `ASin`, `ACos`, `ATan(x)`, `ATan2(y, x)`, `Degrees(x)`, `Radians(x)`, `Pi()` | a float |
| `Random()` | a float from 0 (included) to 1 (excluded), new for every row — `order_by('?')` orders by it |
| `Round(field, precision=0)` | the value rounded to `precision` decimal places, 0 to 1000. A Decimal result has exactly `precision` places on every backend, an integer stays an integer, a float a float — also for a literal or an expression (`Round(Value(3), 1)` is `3`, `Round(F("qty") * 0.5, 1)` a float). PostgreSQL rounds the exact numeric value; SQLite rounds its double, so a value with no exact binary form (`2.675`) can round the other way there. |

Every argument is a field name, an expression or a number literal (`Mod("quantity", 3)`,
`Power(2, "level")`); a bool or any other literal raises `QueryError`. They work the same on every
backend — on SQLite through functions hare registers on each connection — and a NULL argument gives
NULL. An argument outside a function's domain (`Sqrt` of a negative number, `Ln(0)`, `ASin(2)`,
`Mod(x, 0)`) raises `OperationalError`; on PostgreSQL that aborts the surrounding transaction, as any
failed statement does.

```python
await Product.objects.annotate(rounded=Ceil(F("price") / 10) * 10)
await Point.objects.annotate(distance=Sqrt(Power("x", 2) + Power("y", 2))).filter(distance__lt=5)
```

## <a id="text-functions"></a>Text functions

| Class | Result |
|---|---|
| `Left(text, length)`, `Right(text, length)` | the first/last characters; a negative length drops that many from the other end |
| `Substr(text, position, length=None)` | from a 1-based position (positions before 1 count as empty, as on PostgreSQL); a negative length raises `OperationalError` |
| `StrIndex(text, substring)` | the 1-based position of the first occurrence, `0` without one — an `int` |
| `Replace(text, old, new="")`, `Repeat(text, count)`, `Reverse(text)` | text |
| `LPad(text, length, fill=" ")`, `RPad(...)` | filled to `length`; a longer text is cut to it |
| `LTrim(text)`, `RTrim(text)` | leading/trailing spaces removed |
| `Chr(code)`, `Ord(text)` | the character of a code point; the first character's code point (`0` of an empty text) |
| `MD5`, `SHA1`, `SHA224`, `SHA256`, `SHA384`, `SHA512(text)` | the lowercase hex digest of the UTF-8 text |
| `Trim(text)`, `Lower(text)`, `Upper(text)` | the text trimmed of spaces at both ends, in lower case, in upper case |
| `Length(text)` | the number of characters — an `int` |
| `Concat(field, *args)` | the arguments joined into one text — `CONCAT`. PostgreSQL casts every arg to `::text`. Every argument is concatenated as the same text on every backend: a bool as `true`/`false`; a `date`/`datetime` literal or `DatetimeField` as ISO text (an aware datetime in UTC: `2020-01-02 00:04:05+00:00`); a float as PostgreSQL writes it (`2`, `2.25`, `1e+15`); a Decimal value with a known scale with exactly that many places (`F("price") * 2` is `2.20`); a `timedelta` literal as its microseconds and a `uuid.UUID` as its text. A `bytes` literal raises `QueryError`. On SQLite it is written with <code>&#124;&#124;</code>, so every supported SQLite version runs it. |

The first argument is a field name, an expression or a literal; a later string argument is text
(`Replace("name", "-", " ")`) — pass a field there as `F("other")`. Every function behaves as on
PostgreSQL on every backend, and a NULL argument gives NULL. `SHA1` needs the `pgcrypto` extension on
PostgreSQL.

## <a id="comparison-functions"></a>Comparison and conversion functions

| Class | Result |
|---|---|
| `Cast(expression, output_field)` | the value converted to `output_field`'s type — an integer, float, Decimal, `CharField`/`TextField`, boolean, date, datetime or time field (any other raises `FieldError`) |
| `Coalesce(field, *default_values)` | the first argument that isn't NULL — `COALESCE`; typed by all its arguments, as on [`Case`](expressions.md#case-when) |
| `Greatest(*values)`, `Least(*values)` | the greatest/least of two or more values, skipping NULLs |
| `NullIf(expression, other)` | NULL when the value equals `other`, the value otherwise |
| `Collate(expression, collation)` | the text compared and sorted by a database collation |

`Cast` follows PostgreSQL's rules on every backend: a float rounds half to even to an integer
(`2.5` → `2`) and a Decimal half away from zero (`2.50` → `3`), text is trimmed and must spell a value
of the type (`" 42 "` → `42`, `"abc"` raises `OperationalError`), a boolean casts only to a 32-bit
`IntField`, a Decimal target raises on more integer digits than it holds, a `CharField(max_length=n)`
target cuts the text to `n` characters, a float becomes text as PostgreSQL writes it (`1e+15`), an
aware datetime is read in UTC and a naive one (`use_timezone=False`) as its wall clock. SQLite can't store NaN, so casting the text `"NaN"` to a number raises there.

`Greatest`/`Least` take field names, expressions or literals (`Greatest("updated_at",
"created_at")`, `Least("price", 100)`) and give NULL only when every value is NULL — SQLite's own
multi-argument `max()`/`min()` would give NULL for any NULL. Numbers compare as numbers and the
result has their common type. `NullIf`'s `other` is a literal or an expression — a string there is
text. A `Collate` name is a plain name (letters, digits, `_`, `-`, `.`, `@`), for example `NOCASE` on
SQLite or `C`/`und-x-icu` on PostgreSQL.

## <a id="json-functions"></a>JSON functions

`JSONObject(**fields)` builds a JSON object of the given keys and values, decoded as a `dict`:
`JSONObject(name="name", total=F("price") * 2)`. A string value is a field name; any other value is an
expression or a literal. Every backend writes a value as PostgreSQL's `jsonb` holds it: a boolean as
`true`/`false`, a Decimal and a float as a number (an integral float as an integer, `2`), an aware
datetime as ISO text in UTC (`2020-01-02T03:04:05.5+00:00`), a naive one as its wall clock, a time with
its offset (`12:30:00+00`), bytes as `\x` and hex, a JSON field or a nested `JSONObject` as nested
JSON. The object's keys come back in the database's order — on PostgreSQL `jsonb`'s own (shorter keys
first). A key holding a null byte raises `QueryError`. Filter such an annotation with the `JSONField`
lookups (`summary__filter={"total__gt": 10}`, `summary__contains={...}`) or read a path of it with
`F("summary__total")`.

```python
await Book.objects.annotate(card=JSONObject(title="title", author=JSONObject(name="author__name"))).values_list("card")
```

`JSONArray(*values)` builds a JSON array of the given values in order, decoded as a `list`:
`JSONArray("title", F("price") * 2, 5)` — `JSONB_BUILD_ARRAY` on PostgreSQL, `json_array` on SQLite. A
string is a field name; values are written as in `JSONObject`, a nested `JSONArray`/`JSONObject` as
nested JSON; `JSONArray()` is `[]`. An aggregate works as an item (`JSONArray(Count("id"),
Sum("price"))` per group).
