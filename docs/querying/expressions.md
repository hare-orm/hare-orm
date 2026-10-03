# Expressions

`Q`, `F`, `Case`/`When` and `Exists`/`OuterRef`/`Subquery` from `hare.query.expressions` - everything
you'd pass into `.filter()`/`.annotate()`/`.update()` that isn't a plain value. The database
functions of `hare.query.functions` (`Count`, `Sum`, `Max`, `Min`, `Avg`, string, date and other
functions) are on [Database functions](functions.md), `Window` on
[Aggregates, grouping and window functions](aggregation.md), `RawSQL` on [Raw SQL](raw-sql.md), and
how to write your own function or expression on
[Custom functions and expressions](../extending/custom-functions-and-expressions.md). For the
`QuerySet` methods that consume these, see [QuerySet methods](queryset-methods.md).

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

`Q(...)` joins its conditions with AND, `Q.with_connector(Connector.OR, ...)` with OR. The
constructor takes nothing but conditions, so a filter on any field name fits in it - a field named
`join_type` or `connector` too - and a `dict[str, str]` of filters type-checks as `Q(**filters)`.
A field named like a parameter of a `QuerySet`/`Model` method (`get(using=..., exception=...)`,
`When(then=...)`) is filtered through a `Q`: `Model.objects.get(Q(exception="timeout"))`.

Operators: `&` / `|` (build a new `Q` joining both with AND/OR), `~` (returns a new negated `Q`,
non-mutating), `.negate()` (mutates `_is_negated` in place instead), `bool(q)` (`True` if it has any
filters or a truthy child — `False` for an empty `Q()`).

## `F` {: #f }

```python
class F(Expression):
    def __init__(self, name: str) -> None
```

`name` is a field name, `related__field`, a JSON path (`data__key`, `data__0`), or an annotation
name; `pk` (also `related__pk`) names the primary key. Supports `+ - * / % **` and their reflected
forms — **no `//`**. The same operators work on any expression result, so arithmetic nests and
applies to functions, aggregates and `Case`: `(F("price") + 1) * 2`, `100 - (F("price") - 1)`,
`Coalesce("rating", 0) + 1`, `Length("title") * 2`, `Sum("price") + 1`, `Case(...) + 1` - in
annotations, filters, aggregates and `.update()`.

`annotate(x=F("field"))`/`F("related__field")` is decoded through that field exactly like
`.values("field")` — a `Decimal`, a timezone-aware `datetime`, a `UUID`, an enum member, a parsed
JSON value, a decrypted `EncryptedTextField`/`EncryptedJSONField` — on model instances, in
`.values()`/`.values_list()` and in `union()` results. `F("relation")` is the related row's primary
key, `F("annotation")` keeps that annotation's own type. A JSON path into an `EncryptedJSONField`
(`F("config__url")`) raises `FieldError`: the stored key value is a Fernet token.

`F("data__key")` is the JSON value at the path (`jsonb` on Postgres), read back parsed: an object is a
`dict`, an array a `list`, a boolean a `bool`, a number a number. A filter on such an annotation compares
JSON values - `filter(v=10)` and `filter(v="10")` match the number and the string respectively, a `dict`/
`list` value compares with the object/array, and `__in`/`__not_in` take any number of values (bound as
one array parameter). As with Django's key transforms, `filter(v=None)` (and a `None` inside
`__in`/`__not_in`) matches a JSON `null`, while `v__isnull=True` matches a missing path - a JSON `null`
isn't missing, so `v__isnull=False` matches it; `exclude(v=None)` keeps a missing path.
`__contains`/`__startswith`/`__icontains`/... match the value's text (the unquoted string) and take a
string - a `dict`/`list` raises `QueryError`. `__has_key`/`__has_keys`/`__has_any_keys`/
`__contained_by`/`__filter` test the object or array at the path, as on a whole `JSONField`. Ordering
(`order_by`, `__gt`/`__lt`/`__range`, also `order_by()` of a whole `JSONField`) follows the `jsonb` order
on every backend: object > array > boolean > number > string > null, numbers numerically, an object or
array with more members above one with fewer, and at the top level an empty array below null and a
scalar below an array. Strings compare by the database's collation - by code point on SQLite.

`F("annotation__key")` is a path into a JSON annotation (a `JSONObject`, an `F("data")`) the same way; a
path into an annotation of another type raises `FieldError`. `values()`, `values_list()` and `order_by()`
take the same paths by name, as Django's key transforms: `values_list("data__owner__name")`,
`values("summary__total")`, `order_by("-data__score")`, and so does `filter()`
(`filter(data__score__gt=10)`), also after a relation (`values("owner__data__plan")`).

A JSON path compared with an expression of another type (`filter(data__rank=F("number"))`,
`filter(number__gt=F("data__rank"))`, `filter(summary__at=F("at"))`) compares JSON values: the
expression is taken as the JSON value `JSONObject` would write for it, so a value compares equal to
the column it was written from, a JSON string `"2"` doesn't equal the number `2`, and a `NULL` column
matches nothing.

A `DateField` compared with a `DatetimeField` - `filter(at__gte=F("day"))`, `filter(day__lt=F("at"))` -
compares the date as the first moment of its day in the configured zone, as a `date` literal does; under
`use_tz=False` as its wall-clock midnight.

```python
await Employee.objects.filter(pk=emp.pk).update(salary=F("salary") + 100)
```

An `F()` expression assigned to a field before `save()` is evaluated by the database, not in Python,
and stays unresolved on the instance afterwards — `emp.salary` is still the expression, not the
number that was written. Calling `save()` again therefore applies it a second time (`salary` goes up
by 100 twice); call `refresh_from_db()` after the first save to read the written value back and drop
the expression. `QuerySet.bulk_update()` doesn't accept expression values at all
(`QueryError`) — use `.update()` or `save()`.

### Date/time arithmetic {: #date-time-arithmetic }

`+` and `-` between date, datetime and timedelta values work the same on SQLite, PostgreSQL
(asyncpg) and PostgreSQL (rust_pg), in annotations, filters, `order_by`, aggregates and `.update()`:

| Expression | Result |
|---|---|
| `DatetimeField ± timedelta` (a literal, or an `F()` on a `TimeDeltaField`) | datetime |
| `DateField ± timedelta` | date |
| `DatetimeField - DatetimeField` | timedelta |
| `DateField - DateField` | timedelta |
| `TimeDeltaField ± TimeDeltaField` / `± timedelta` | timedelta |

```python
await Task.objects.filter(pk=task.pk).update(due_at=F("due_at") + timedelta(days=2))
await Task.objects.annotate(duration=F("finished_at") - F("started_at")).filter(duration__gt=timedelta(hours=1))
await Task.aggregate(longest=Max(F("finished_at") - F("started_at")))   # a timedelta
```

- A datetime shift is a shift of **absolute time**, never of calendar days: adding
  `timedelta(days=1)` across a DST change moves the wall clock by 23 or 25 hours, and the difference
  of two datetimes is the real elapsed time. It is exact to the microsecond.
- A date is treated as midnight: `date ± timedelta` is the date of `midnight ± timedelta`, so a
  sub-day remainder is **floored** (`date + 23 hours` is the same date, `date - 1 hour` is the
  previous date).
- A `NULL` operand gives `NULL`.
- Under `use_tz=False` a naive datetime is local wall-clock time and is shifted as absolute time in
  the system zone.
- On SQLite the arithmetic runs in Python functions registered on every connection (SQLite has no
  interval type and its own date math loses microseconds); such an expression can't be used in an
  index definition. On PostgreSQL it is rendered as native `interval` arithmetic, the datetime
  difference relies on `EXTRACT(EPOCH ...)` (exact on PostgreSQL 14+).
- Anything else that involves a date/time operand - a number (`F("day") + 1`), `datetime * 2`,
  `date + datetime`, `datetime - date`, any `TimeField` arithmetic, a timedelta literal against a
  non-date/time field - raises `FieldError` before anything is written. A `TimeDeltaField` combined
  with a plain number (`F("duration") * 2`) is still ordinary microsecond arithmetic.

### Result type {: #result-type }

An arithmetic result has the same Python type on every backend, decided by its operands:

- any **float** operand (a `FloatField`, a float literal, an `Avg` of integers) makes it a `float` -
  `F("size") * 1.5`, `F("price") * 0.5` and `Sum(F("size") * 1.5)` are floats;
- otherwise any **Decimal** operand (a `DecimalField`, a `Decimal` literal) makes it a `Decimal`;
- otherwise it is an **integer** (`Count`, `Length`, integer fields and literals, and a
  `TimeDeltaField` scaled by a number - `F("duration") * 2` is its whole microseconds).

A `Decimal` result has the same scale everywhere: the larger of the operands' scales for
`+ - % **`, their sum for `*` (an integer operand has scale 0). `F("price") + 1` on a
`decimal_places=2` field is `Decimal("2.10")`, `F("price") * Decimal("1.5")` has 3 decimal places,
`F("qty") * Decimal("0.5")` on an `IntField` is `Decimal("1.0")`, `Count("items") * F("price")` is
`Decimal("2.20")`. A quotient is a real division on every backend (`F("price") / 3` on `2.00` is
`0.666...`, never `0`) and is **not** rounded - precision wins over dialect parity: on Postgres
`F("price") / 3` is the exact numeric `Decimal("0.36666666666666666667")`, while SQLite computes in
floating point and returns the nearest Decimal of its double result (`Decimal("0.3666666666666667")`).
When every backend must return the same digits, round in the database: `Round(F("price") / 3, 2)` is
`Decimal("0.37")` everywhere.

`%` follows the same types. Between integers it is an exact integer on every backend (SQLite uses its
integer `%` operator, Postgres `MOD`), so `F("big") % 7` never loses precision. With a `Decimal` it
is an exact `Decimal` remainder on every backend - `Decimal("2.00") % Decimal("0.1")` is `0.00`, SQLite
included (it computes on the operands scaled to whole numbers, up to 15 decimal places). With a float
it is a float with the dividend's sign (`F("ratio") % 1.5`). Compound operands keep their grouping:
`F("a") % (F("a") - 3)` is the remainder by `a - 3`.

Arithmetic between two model columns of different types raises `FieldError`, except an integer
column with a float or Decimal column (`F("price") + F("id")`), whose result has the float or Decimal
column's type, as in Django; a Decimal column with a float column (`F("price") * F("rating")`) raises.
A computed value (a count, a length, another expression, a literal) combines with any numeric column
by the rules above.

A bare literal annotation (`annotate(flag=Value(True))`) comes back as its own Python type on every
backend - `bool`, `int`, `float`, `Decimal` (with the literal's scale), `date`, `datetime` and
`time` (bound and decoded like a `DatetimeField`/`TimeField` value: a naive `datetime` is read in the
configured zone under `use_tz=True`, a naive `time` gets its standard offset, and under `use_tz=False`
an aware one is converted to local time), `timedelta` (bound as its whole microseconds, like a
`TimeDeltaField`), `uuid.UUID` (bound as text, like a `UUIDField`), `dict` (bound as JSON text, like
a `JSONField`) and `bytes`. The same encoding applies to such a literal inside `Case`, `Coalesce`
and `Concat`, and an aggregate or window function over a literal annotation keeps its type
(`Sum(Value(1))` is an `int`). `filter(field=Value(v))` and `update(field=Value(v))` convert and
validate `v` through the field exactly as `filter(field=v)`/`update(field=v)` do (`Value(None)` stays a
comparison with `NULL`). A text literal in arithmetic with a numeric column (`F("count") + "5"`) is
converted to that column's type of number; one that isn't a number raises the column's
`ValidationError`. Filtering on a literal annotation
(`annotate(x=Value(v)).filter(x=v)`) compares values of the literal's type on every backend.

A filter on an annotation whose value is a datetime, date or time - `F("moment")`, `F("moment") +
timedelta(...)`, `Coalesce("moment", ...)`, `Max("moment")`/`Min("moment")` (a `HAVING` filter) -
converts its value the way a filter on the field itself does: a naive datetime is read in the
configured zone, a date's string is parsed, a naive time gets the configured offset.

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

`then`/`default` accept either a real expression (evaluated per-row) or a bare Python literal of any
of the usual field-value types — not just `str`.

`When(negate=True, ...)` negates the branch's whole condition, exactly like `~Q(...)`:
`When(a=1, b=2, negate=True)` matches `NOT (a = 1 AND b = 2)`.

```python
await Employee.objects.all().annotate(
    salary_band=Case(
        When(Q(salary__gte=100_000), then="high"),
        When(Q(salary__gte=50_000), then="mid"),
        default="low",
    )
)
```

A `When` referencing an aggregated/annotated expression correctly resolves into `HAVING` rather than
`WHERE`.

The result type of a `Case` comes from all of its branches together (a `Value(...)` counts as
the literal it wraps, a `None` branch doesn't count):

- numbers of different types widen like arithmetic: a float branch makes the result a `float`
  (`Case(When(..., then=F("size")), default=0.5)` is `3.0`/`0.5`), otherwise a `Decimal` branch
  makes it a `Decimal` with the **largest** scale among the branches - no branch is rounded to
  another's scale (`then=F("price")` with `default=Decimal("0.125")` gives `Decimal("1.100")` and
  `Decimal("0.125")`), otherwise it is an integer;
- any other field is used when every other branch fits it (a `JSONField`, a `TimeDeltaField` beside a
  `timedelta` literal);
- literal-only branches of one type give that type (`bool`, `date`, `datetime`, `timedelta`,
  `uuid.UUID`, ...); `date` literals mixed with `datetime` literals give a `datetime` (the date at
  midnight).

The same rules type a `Coalesce` over all of its arguments: `Coalesce("int_null", F("price"))` is a
`Decimal`, `Coalesce("int_null", 0.5)` a `float`, `Coalesce("price_null", F("price3"))` a `Decimal`
with 3 places.

## `Exists` / `OuterRef` / `Subquery` {: #exists-outerref-subquery }

```python
class Subquery(Term):
    def __init__(self, query: AwaitableQuery) -> None

class OuterRef(Expression):
    def __init__(self, field: str) -> None   # a direct field of the outer model, or a related__field path

class Exists(Expression):
    def __init__(self, queryset: QuerySet | ValuesQuery | ValuesListQuery) -> None
```

```python
await Team.objects.annotate(
    has_open_membership=Exists(Membership.objects.filter(team_id=OuterRef("id"), status="open")),
).filter(has_open_membership=True)
```

`OuterRef` resolves its name like `F` would on the outer query: a direct field, `pk`, a
`related__field` path, a forward FK/O2O name (`OuterRef("author")` is the `author_id` column; a
composite-key relation raises `FieldError`) or an annotation of the outer query
(`Author.objects.annotate(n=Length("name")).annotate(x=Exists(Book.objects.filter(rating__lt=OuterRef("n"))))`).
An aggregate annotation works too (`OuterRef("book_count")` for `book_count=Count("books")`): the
subquery is then evaluated per group of the outer query, so the `Exists`/`Subquery` annotation is
an aggregate itself - it stays out of the `GROUP BY`, and a filter on it goes to `HAVING`. A
window function (`Window(...)`) annotation can't be referenced and raises `QueryError`.

`OuterRef` only works inside a queryset wrapped in `Exists(...)` or `Subquery(...)`. Passing a bare (un-awaited)
`QuerySet` to `field__in=` auto-wraps it in `Subquery`, Django-style.

`Exists(...)` is a condition by itself, like in Django: pass it straight to `filter()`/`exclude()`,
`Q(...)` or `When(...)`, and combine it with `&`/`|`/`~` (with another `Exists` or a `Q`):

```python
await Team.objects.filter(Exists(Membership.objects.filter(team_id=OuterRef("id"), status="open")))
await Team.objects.filter(~Exists(Membership.objects.filter(team_id=OuterRef("id"))) | Q(name="Staff"))
await Team.objects.annotate(status_type=Case(When(Exists(Membership.objects.filter(team_id=OuterRef("id"))), then=Value("busy")), default=Value("idle")))
```

The wrapped queryset may also be a `.values()`/`.values_list()` query - it is embedded with its own
columns, grouping and `HAVING`
(`Exists(Book.objects.filter(author_id=OuterRef("id")).group_by("author_id").annotate(total=Sum("rating")).filter(total__gt=10).values("author_id"))`).

Arithmetic with a `Subquery` on either side builds an ORM expression resolved inside the outer
query, so its `OuterRef` keeps working: `Subquery(...) + 1`, `1 + Subquery(...)`,
`Subquery(...) * F("qty")` in `annotate()`, as a filter value (`filter(age__gt=Subquery(...) + 5)`)
and in `update(price=Subquery(...) + 1)`.

An `.annotate(x=Subquery(...))` over a `values()`/`values_list()` query selecting one column is
decoded through that column's field on every backend: a `date`/`datetime` (not text on SQLite), a
`Decimal` (not a float), a `timedelta` for a `TimeDeltaField` (not bare microseconds) - in
`values()`, `values_list()`, full instances and filters on the annotation.
