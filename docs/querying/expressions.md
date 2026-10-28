# Expressions

`Q`, `F`, `Value`, `Case`/`When` and `Exists`/`OuterReference`/`Subquery` from `hare.query.expressions` —
everything you'd pass into `.filter()`/`.annotate()`/`.update()` that isn't a plain value. The database
functions of `hare.query.functions` (`Count`, `Sum`, `Max`, `Min`, `Avg`, string, date and other
functions) are on [Database functions](functions.md), `Window` on
[Aggregates, grouping and window functions](aggregation.md), `RawSQL` on [Raw SQL](raw-sql.md), and how
to write your own function or expression on [Custom functions and
expressions](../extending/custom-functions-and-expressions.md). For the `QuerySet` methods that consume
these, see [QuerySet methods](queryset-methods.md).

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

`Q(...)` joins its conditions with AND, `Q.with_connector(Connector.OR, ...)` with OR. The constructor
takes nothing but conditions, so a filter on any field name fits in it — a field named `join_type` or
`connector` too — and a `dict[str, str]` of filters type-checks as `Q(**filters)`. A field named like a
parameter of a `QuerySet`/`Model` method (`get(does_not_exist_exception=...)`, `When(then=...)`) is
filtered through a `Q`: `Model.objects.get(Q(does_not_exist_exception="timeout"))`.

Operators: `&` / `|` (build a new `Q` joining both with AND/OR), `~` (returns a new negated `Q`,
non-mutating), `.negate()` (mutates `_is_negated` in place instead), `bool(q)` (`True` if it has any
filters, an `Exists` or a truthy child — `False` for an empty `Q()`).

### <a id="condition-value"></a>A condition as a value: `ConditionValue`

```python
class ConditionValue(Expression):
    def __init__(self, condition: Q | Exists) -> None: ...
```

```python
tickets = Ticket.objects.annotate(is_open=Q(closed_at__isnull=True))
reports = Report.objects.annotate(is_shared=Exists(shared_with_user) | Exists(shared_with_team))
await Ticket.objects.filter(due_at__lt=now).update(is_late=Q(closed_at__isnull=True))
```

A condition read as each row's value: true where `filter()` with it takes the row, false where it
doesn't, `None` where SQL's three-valued logic makes it NULL (`Q(desc="x")` on a row whose `desc` is
NULL). A `Q` — `Exists(...) | Exists(...)` is one — given to `annotate()`, `alias()`, `values()`,
`values_list()` or `update()` is made a `ConditionValue`; an annotated condition is filtered and
ordered by as any boolean annotation (`.filter(is_open=True)`). The value is decoded through
`BooleanField(null=True)` — a database writing a condition as `0`/`1` (SQLite, ClickHouse) gives a
`bool` too.

## <a id="f"></a>`F`

```python
class F(CombinableExpression):
    def __init__(self, name: str) -> None: ...
```

`name` is a field name, `related__field`, a JSON path (`data__key`, `data__0`), or an annotation name;
`pk` (also `related__pk`) names the primary key. Supports `+ - * / % **` and their reflected forms — **no
`//`**. The same operators work on the result of an `F()`, a function, an aggregate and `Case` (not on a
`Value(...)`, a literal written as one), so arithmetic nests: `(F("price") + 1) * 2`, `100 - (F("price")
- 1)`, `Coalesce("rating", 0) + 1`, `Length("title") * 2`, `Sum("price") + 1`, `Case(...) + 1` — in
annotations, filters, aggregates and `.update()`. Each operator builds a `CombinedExpression(left,
connector, right)`, its `connector` an `ArithmeticOperator` (`ADD`, `SUB`, `MUL`, `DIV`, `POW`, `MOD`).

`annotate(x=F("field"))`/`F("related__field")` is decoded through that field exactly like
`.values("field")` — a `Decimal`, a timezone-aware `datetime`, a `UUID`, an enum member, a parsed JSON
value, a decrypted `EncryptedTextField`/`EncryptedJSONField` — on model instances, in
`.values()`/`.values_list()` and in `union()` results. `F("relation")` is the related row's primary key,
`F("annotation")` keeps that annotation's own type. A JSON path into an `EncryptedJSONField`
(`F("config__url")`) raises `FieldError`: the stored key value is a Fernet token. A part of a date, time
or datetime is read as `values()` reads it — `F("created__year")`, `F("created__date")`, after a
relation (`F("author__born__year")`) or a range or array path (`F("during__startswith__hour")`,
`F("dates__0__month")`).

A field compared with an expression of another type (`filter(code=F("number"))`, text with a number) is
compared as the database compares the two: PostgreSQL refuses it, SQLite compares by its own rules. A
field set to an expression (`update(total=F("price"))`) must hold the same type of value — numbers with
numbers, text with text, a date with a date or a moment — or `FieldError` is raised before the query
runs, on every database: SQLite would keep a value the field reads back none of
(`update(flag=F("count"))` sets a boolean to a number). A text field may be set to any value, as its
text. An update sets a date field only to a date and a moment field only to a moment (see
[Date/time arithmetic](#date-time-arithmetic)). `Cast()` converts the value where that is meant:
`filter(code=Cast(F("number"), CharField(max_length=10)))`. A field of another type (JSON, UUID, an
array, ...) isn't checked.

`F("data__key")` is the JSON value at the path (`jsonb` on PostgreSQL), read back parsed: an object is a
`dict`, an array a `list`, a boolean a `bool`, a number a number. Arithmetic on it raises `FieldError` —
a JSON value is no number to PostgreSQL or ClickHouse: `Cast(F("data__count"), IntField()) + 1`. A filter
on such an annotation compares
JSON values — `filter(v=10)` and `filter(v="10")` match the number and the string respectively, a `dict`/
`list` value compares with the object/array, and `__in`/`__not_in` take any number of values (bound as
one array parameter). `filter(v=None)` (and a `None` inside `__in`/`__not_in`) matches a JSON `null`,
while `v__isnull=True` matches a missing path — a JSON `null` isn't missing, so `v__isnull=False` matches
it; `exclude(v=None)` keeps a missing path. `__contains`/`__startswith`/`__icontains`/... match the
value's text (the unquoted string) and take a string — a `dict`/`list` raises `QueryError`.
`__has_key`/`__has_keys`/`__has_any_keys`/ `__contained_by`/`__filter` test the object or array at the
path, as on a whole `JSONField`. Ordering (`order_by`, `__gt`/`__lt`/`__range`, also `order_by()` of a
whole `JSONField`) is the database's own. PostgreSQL follows the `jsonb` order: object > array > boolean >
number > string > null, numbers numerically, an object or array with more members above one with fewer,
and at the top level an empty array below null and a scalar below an array. SQLite orders the value
`json_extract()` reads: a string as itself, `true`/`false` as 1/0, a JSON `null` as `NULL`, an object or
array as its minified text, numbers before text. ClickHouse orders the `Dynamic` value it reads of the JSON
text (session setting `allow_suspicious_types_in_order_by=1`) by the name of its type first, then by
value (a `Float64` `2.5` before an `Int64` `9`, `Bool` before numbers, strings after); comparing a path
that holds strings and numbers raises ClickHouse's `NO_COMMON_TYPE`. Strings compare by the database's
collation — by code point on SQLite.

`F("annotation__key")` is a path into a JSON annotation (a `JSONObject`, an `F("data")`) the same way; a
path into an annotation of another type raises `FieldError`. `values()`, `values_list()` and `order_by()`
take the same paths by name: `values_list("data__owner__name")`, `values("summary__total")`,
`order_by("-data__score")`, and so does `filter()` (`filter(data__score__gt=10)`), also after a relation
(`values("owner__data__plan")`).

A JSON path compared with an expression of another type (`filter(data__rank=F("number"))`,
`filter(number__gt=F("data__rank"))`, `filter(summary__at=F("at"))`) compares JSON values: the expression
is taken as the JSON value `JSONObject` would write for it, so a value compares equal to the column it
was written from, a JSON string `"2"` doesn't equal the number `2`, and a `NULL` column matches nothing.

A `DateField` compared with a `DatetimeField` — `filter(at__gte=F("day"))`, `filter(day__lt=F("at"))` —
compares the date as the first moment of its day in the configured zone, as a `date` literal does; under
`use_timezone=False` as its wall-clock midnight.

```python
await Book.objects.filter(pk=book.pk).update(price=F("price") + 100)
```

An `F()` expression assigned to a field before `save()` is evaluated by the database, not in Python, and
stays unresolved on the instance afterwards — `book.price` is still the expression, not the number that
was written. Calling `save()` again therefore applies it a second time (`price` goes up by 100 twice);
call `refresh_from_db()` after the first save to read the written value back and drop the expression.
`QuerySet.bulk_update()` doesn't accept expression values at all (`QueryError`) — use `.update()` or
`save()`.

### <a id="date-time-arithmetic"></a>Date/time arithmetic

`+` and `-` between date, datetime and timedelta values work the same on SQLite, PostgreSQL (asyncpg) and
PostgreSQL (rust_pg), in annotations, filters, `order_by`, aggregates and `.update()`:

| Expression | Result |
|---|---|
| `DatetimeField ± timedelta` (a literal, or an `F()` on a `TimeDeltaField`) | datetime |
| `DateField ± timedelta` | date |
| `DatetimeField - DatetimeField` | timedelta |
| `DateField - DateField` | timedelta |
| `TimeDeltaField ± TimeDeltaField` / `± timedelta` | timedelta |
| `TimeDeltaField * number`, `number * TimeDeltaField`, `TimeDeltaField / number` | timedelta |

```python
await Loan.objects.filter(pk=loan.pk).update(due_at=F("due_at") + timedelta(days=2))
await Loan.objects.annotate(duration=F("returned_at") - F("lent_at")).filter(duration__gt=timedelta(days=14))
await Loan.objects.aggregate(longest=Max(F("returned_at") - F("lent_at")))   # a timedelta
```

- A datetime shift is a shift of **absolute time**, never of calendar days: adding `timedelta(days=1)`
  across a DST change moves the wall clock by 23 or 25 hours, and the difference of two datetimes is the
  real elapsed time. It is exact to the microsecond.
- A date is treated as midnight: `date ± timedelta` is the date of `midnight ± timedelta`, so a sub-day
  remainder is **floored** (`date + 23 hours` is the same date, `date - 1 hour` is the previous date).
- A `NULL` operand gives `NULL`.
- A result beyond the dates a database holds is the database's own: SQLite raises `OperationalError`
  (`timestamp out of range`, `date out of range`), ClickHouse gives the nearest moment or day the column
  holds (`DateTime` 1900-01-01 to 2299-12-31 23:00, `DateTime64` from ClickHouse 26.7 the years 1 to
  9999, `Date32` up to 2299-12-31), and on PostgreSQL a
  result after the year 9999, which Python's `datetime` can't hold, raises when it is read.
- Under `use_timezone=False` a naive datetime is local wall-clock time and is shifted as absolute time in
  the system zone.
- On SQLite the arithmetic runs in Python functions registered on every connection (SQLite has no
  interval type and its own date math loses microseconds); such an expression can't be used in an index
  definition. On PostgreSQL it is rendered as native `interval` arithmetic, the datetime difference
  relies on `EXTRACT(EPOCH ...)` (exact on every PostgreSQL hare supports).
- Anything else that involves a date/time operand — a number (`F("day") + 1`), `datetime * 2`,
  `date + datetime`, `datetime - date`, any `TimeField` arithmetic, a timedelta literal against a
  non-date/time field — raises `FieldError` before anything is written. A `TimeDeltaField` scaled by a number
  — a literal or a numeric column, `F("duration") * 2`, `F("duration") * F("count")`, `F("duration") / 3` — is
  a `timedelta`, as in Python: its microseconds are scaled, and a quotient is rounded to the nearest
  microsecond, a half to the even one, as Python divides a `timedelta` and PostgreSQL an `interval`.
  `update(duration=F("duration") * 2)` writes it back. A number added to a duration or taken from it
  (`F("duration") + 1`, `F("duration") - F("count")`) raises `FieldError` — add a `timedelta`; so does any
  other arithmetic of a duration (`3 / F("duration")`, `F("duration") % 2`, `F("duration") ** 2`, a product of
  two durations). A PostgreSQL `IntervalField` follows the same rules: added to or taken from an interval or a
  `timedelta` (`F("lasts") + timedelta(minutes=5)`, the `timedelta` bound as an interval), multiplied by a
  number and divided by one (`F("lasts") * F("count")`). It also shifts a datetime or a date
  (`F("at") + F("lasts")`, `F("day") - F("lasts")`) as PostgreSQL adds an `interval` — a month of it is a
  calendar month, a day a calendar day in the session's zone, not the 30 days and 24 hours its `timedelta`
  reads.

### <a id="result-type"></a>Result type

An arithmetic result's Python type is decided by its operands:

- any **float** operand (a `FloatField`, a float literal, a statistic the database computes as a float)
  makes it a `float` — `F("size") * 1.5`, `F("price") * 0.5` and `Sum(F("size") * 1.5)` are floats;
- otherwise any **Decimal** operand (a `DecimalField`, a `Decimal` literal, a statistic the database
  computes exactly) makes it a `Decimal`;
- otherwise it is an **integer** (`Count`, `Length`, integer fields and literals).

An `Avg`, a `StdDev` or a `Variance` of integers or Decimals is an exact `Decimal` where the database
computes one — PostgreSQL (`Features.computes_integer_statistics_exactly`,
`computes_decimal_statistics_exactly`) — and a `float` elsewhere.

A `Decimal` result has the same scale everywhere: the larger of the operands' scales for `+ - % **`,
their sum for `*` (an integer operand has scale 0). `F("price") + 1` on a `decimal_places=2` field is
`Decimal("2.10")`, `F("price") * Decimal("1.5")` has 3 decimal places, `F("qty") * Decimal("0.5")` on an
`IntField` is `Decimal("1.0")`, `Count("items") * F("price")` is `Decimal("2.20")`. A quotient is the
database's own: on PostgreSQL `F("price") / 3` of `1.10` is the numeric `Decimal("0.36666666666666666667")`,
at PostgreSQL's own division scale (at least 16 significant digits); on ClickHouse it has the dividend's
places, cut, not rounded: `Decimal("0.36")`. When every database must return the same digits, round in
the database: `Round(F("price") / 3, 2)` is `Decimal("0.37")` everywhere. A sum, difference and product
of Decimals is the database's own too — on ClickHouse one past its type's digits wraps around or raises
ClickHouse's own error (see [ClickHouse](../dialects/clickhouse/differences.md#results)). SQLite stores
no exact decimal: a `Decimal` in a query run on SQLite raises `UnSupportedError` — see
[`DecimalField` (SQLite)](../models/field-types.md#decimal-field-sqlite). A division by zero is the
database's own, and hare doesn't check
it: PostgreSQL raises `OperationalError`, SQLite gives NULL, ClickHouse gives `inf`/`nan` for a Decimal or
a float and its own error for integers; a NULL dividend or divisor gives NULL. Between integers `/` is the
database's own: on PostgreSQL and SQLite an integer rounded towards zero — `F("qty") / 2` on `-7` is `-3`,
inside an aggregate too (`Avg(F("qty") / 3)` averages the integer quotients); on ClickHouse the exact
quotient, a fraction too (`-3.5`), read as a float (`Features.divides_integers_exactly`). PostgreSQL's sum
of a `BigIntField` is a `NUMERIC`, so `Sum("big") / 2` keeps its fraction there (`Decimal("2.5")`): the
value of an expression is the one the database gives, never cut to its output field. An integer
sum, difference or product beyond its type is the database's own: PostgreSQL raises
`integer out of range`, the type being the widest operand's (an `IntField` 32 bits, a `BigIntField` or a
literal beyond 32 bits 64); SQLite makes a result past 64 bits a float; ClickHouse wraps it around. An
integer an `update()` writes from such arithmetic, or from a column of a wider type, is the database's
too past the field's column type (`update(n=F("n") + 1)` of `2147483647` in an `IntField`): PostgreSQL's
column refuses it, SQLite keeps the number, ClickHouse wraps it around.

`%` follows the same types. Between integers it is an exact integer on every database (SQLite uses its
integer `%` operator, PostgreSQL `MOD`), so `F("big") % 7` never loses precision. With a `Decimal` it is
an exact `Decimal` remainder on PostgreSQL and ClickHouse — `Decimal("2.00") % Decimal("0.1")` is
`0.00`. With a float it
is a float with the dividend's sign (`F("ratio") % 1.5`). Compound operands keep their grouping:
`F("a") % (F("a") - 3)` is the remainder by `a - 3`.

Arithmetic between two model columns of different types raises `FieldError`, except an integer column
with a float or Decimal column (`F("price") + F("id")`), whose result has the float or Decimal column's
type; a Decimal column with a float column (`F("price") * F("rating")`) raises. A computed value (a
count, a length, another expression, a literal) combines with any numeric column by the rules above.

A bare literal annotation (`annotate(flag=Value(True))`) comes back as its own Python type on every
database — `bool`, `int`, `float`, `Decimal` (with the literal's scale), `date`, `datetime` and `time`
(bound and decoded like a `DatetimeField`/`TimeField` value: a naive `datetime` is read in the configured
zone under `use_timezone=True`, a naive `time` gets its standard offset, and under `use_timezone=False`
an aware one is converted to local time), `timedelta` (bound as its whole microseconds, like a
`TimeDeltaField`), `uuid.UUID` (bound as text, like a `UUIDField`), `dict` (bound as JSON text, like a
`JSONField`) and `bytes`. The same encoding applies to such a literal inside `Case`, `Coalesce` and
`Concat`, and an aggregate or window function over a literal annotation keeps its type (`Sum(Value(1))`
is an `int`). `filter(field=Value(v))` and `update(field=Value(v))` convert and validate `v` through the
field exactly as `filter(field=v)`/`update(field=v)` do (`Value(None)` stays a comparison with `NULL`,
and is the `NULL` written in an update). A text literal in arithmetic with a numeric column
(`F("count") + "5"`) raises the column's `ValidationError`, as its filter does — give the number itself.
A filter on any annotation converts and checks its value through the annotation's field the same way
(`annotate(n=Count("id")).filter(n="1")` raises). Filtering on a literal
annotation (`annotate(x=Value(v)).filter(x=v)`) compares values of the literal's type on every database.

A filter on an annotation whose value is a datetime, date or time — `F("moment")`,
`F("moment") + timedelta(...)`, `Coalesce("moment", ...)`, `Max("moment")`/`Min("moment")` (a `HAVING`
filter) — converts its value the way a filter on the field itself does: a naive datetime is read in the
configured zone, a date's string is parsed, a naive time gets the configured offset.

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

`then`/`default` accept either a real expression (evaluated per-row) or a bare Python literal of any of
the usual field-value types — not just `str`. A branch condition is a `Q`, an `Exists` or keyword
filters.

`When(negate=True, ...)` negates the branch's whole condition, exactly like `~Q(...)`:
`When(a=1, b=2, negate=True)` matches `NOT (a = 1 AND b = 2)`.

```python
await Book.objects.all().annotate(
    price_band=Case(
        When(Q(price__gte=100), then="high"),
        When(Q(price__gte=50), then="mid"),
        default="low",
    )
)
```

A `When` referencing an aggregated/annotated expression correctly resolves into `HAVING` rather than
`WHERE`.

The result type of a `Case` comes from all of its branches together (a `Value(...)` counts as the literal
it wraps, a `None` branch doesn't count):

- numbers of different types widen like arithmetic: a float branch makes the result a `float`
  (`Case(When(..., then=F("size")), default=0.5)` is `3.0`/`0.5`), otherwise a `Decimal` branch makes it
  a `Decimal` with the **largest** scale among the branches — no branch is rounded to another's scale
  (`then=F("price")` with `default=Decimal("0.125")` gives `Decimal("1.100")` and `Decimal("0.125")`),
  otherwise it is an integer;
- any other field is used when every other branch fits it (a `JSONField`, a `TimeDeltaField` beside a
  `timedelta` literal);
- literal-only branches of one type give that type (`bool`, `date`, `datetime`, `timedelta`, `uuid.UUID`,
  ...); `date` literals mixed with `datetime` literals give a `datetime` (the date at midnight);
- a date beside a moment — a `DateField` or a `date` literal beside a `DatetimeField` or a `datetime`
  literal — gives a moment, the date at midnight UTC, as PostgreSQL makes it a `timestamp` (a moment
  isn't cut to its date);
- values of types that don't combine — a number with text, a boolean with a number, a date with a number
  or a duration, text with a time — raise `FieldError` before the query runs, on every database: convert
  them to one type with `Cast()`. A string literal is text here even when it reads as a number
  (`Coalesce("int_null", Value("7"))` raises). A field of another type (JSON, UUID, an array, ...) isn't
  checked.

The same rules type a `Coalesce` over all of its arguments, and refuse the same mixes in `Coalesce`,
`NullIf`, `Greatest` and `Least`: `Coalesce("int_null", F("price"))` is a `Decimal`,
`Coalesce("int_null", 0.5)` a `float`, `Coalesce("price_null", F("price3"))` a `Decimal` with 3 places.

## <a id="exists-outerref-subquery"></a>`Exists` / `OuterReference` / `Subquery`

```python
class Subquery(CombinableExpression, Term):
    def __init__(self, query: AwaitableQuery[Any], *, shares_outer_scope: bool = False) -> None: ...

class OuterReference(CombinableExpression):
    # a direct field of the outer model, or a related__field path
    def __init__(self, field: str) -> None: ...

class Exists(Expression):
    def __init__(self, queryset: QuerySet[Any, Any], *, negated: bool = False) -> None: ...
```

```python
await Author.objects.annotate(
    has_book_in_print=Exists(Book.objects.filter(author_id=OuterReference("id"), in_print=True)),
).filter(has_book_in_print=True)
```

`OuterReference` resolves its name like `F` would on the outer query: a direct field, `pk`, a
`related__field` path, a forward FK/O2O name (`OuterReference("author")` is the `author_id` column; a
composite-key relation raises `FieldError`) or an annotation of the outer query:

```python
Author.objects.annotate(n=Length("name")).annotate(x=Exists(Book.objects.filter(rating__lt=OuterReference("n"))))
```

`OuterReference("pk")` of a model with a composite primary key compares with all of its fields at once:
with `pk` of the same model or with a relation to it
(`Child.objects.filter(parent=OuterReference("pk"))`) — compared with a single field it raises
`FieldError` naming the key's fields. An aggregate annotation works too (`OuterReference("book_count")`
for `book_count=Count("books")`): the subquery is then evaluated per group of the outer query, so the
`Exists`/`Subquery` annotation is an aggregate itself — it stays out of the `GROUP BY`, and a filter on
it goes to `HAVING`. A window function (`Window(...)`) annotation can't be referenced and raises
`QueryError`.

`OuterReference` only works inside a queryset wrapped in `Exists(...)` or `Subquery(...)`. Passing a bare
(un-awaited) `QuerySet` to `field__in=` is wrapped in `Subquery`.

With `shares_outer_scope=True`, an `OuterReference` of the subquery's query refers to the query the
enclosing one is nested in, not to the enclosing one — for a subquery placed between a queryset and its
own filters, which SQL lets reach any enclosing query.

`Exists(..., negated=True)` is the same as `~Exists(...)`. `Exists(...)` is a condition by itself: pass
it straight to `filter()`/`exclude()`, `Q(...)` or `When(...)`, and combine it with `&`/`|`/`~` (with
another `Exists` or a `Q`):

```python
await Author.objects.filter(Exists(Book.objects.filter(author_id=OuterReference("id"), in_print=True)))
await Author.objects.filter(~Exists(Book.objects.filter(author_id=OuterReference("id"))) | Q(name="Anonymous"))
await Author.objects.annotate(status_type=Case(When(Exists(Book.objects.filter(author_id=OuterReference("id"))), then=Value("published")), default=Value("unpublished")))
```

The wrapped queryset may also be a `.values()`/`.values_list()` query — it is embedded with its own
columns, grouping and `HAVING`:

```python
Exists(Book.objects.filter(author_id=OuterReference("id")).group_by("author_id").annotate(total=Sum("rating")).filter(total__gt=10).values("author_id"))
```

Arithmetic with a `Subquery` on either side builds an ORM expression resolved inside the outer query, so
its `OuterReference` keeps working: `Subquery(...) + 1`, `1 + Subquery(...)`, `Subquery(...) * F("qty")`
in `annotate()`, as a filter value (`filter(age__gt=Subquery(...) + 5)`) and in
`update(price=Subquery(...) + 1)` — an integer quotient and the arithmetic of a duration
(`Subquery(...) / 2`, `Subquery(...) + F("delay")`) too.

A queryset embedded into another query — by `Subquery`, `Exists`, `__in`, `with_cte()`, `insert_from()`
or `merge()` — compiles into one SQL text with it, so it runs on the outer query's connection. A
queryset pinned to another connection by `.using()`, or of a model living on another connection than
the outer one, which runs on its own model's, raises `QueryError` — query each connection separately. An
outer query sent to a connection of your choice by `.using()` (a shard) takes the inner one along.

An `.annotate(x=Subquery(...))` over a `values()`/`values_list()` query selecting one column is decoded
through that column's field on every database: a `date`/`datetime` (not text on SQLite), a `Decimal` (not
a float), a `timedelta` for a `TimeDeltaField` (not bare microseconds) — in `values()`, `values_list()`,
full instances and filters on the annotation.
