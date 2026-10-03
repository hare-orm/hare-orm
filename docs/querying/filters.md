# Filters and lookups

## The generic set — every "ordinary" field {: #generic-set }

Registered for **every** direct model field, regardless of its Python type:

`exact` (no suffix), `not`, `in`, `not_in`, `isnull`, `not_isnull`, `gte`, `lte`, `gt`, `lt`, `range`
(exactly 2 values), `contains`, `startswith`, `endswith`, `iexact`, `icontains`, `istartswith`,
`iendswith`, `search`, `posix_regex`, `iposix_regex`, and on Postgres `trigram_similar`,
`trigram_word_similar`, `trigram_strict_word_similar` (see
[Text search — trigram similarity](../dialects/postgresql/text-search.md#trigram-similarity-and-unaccent)).

A `CharField`/`TextField`/`CitextField` also takes the `unaccent` transform on Postgres:
`filter(name__unaccent__icontains="saldana")` compares the text without its accents.

`DateField`/`TimeField`/`DatetimeField` additionally get their own date/time extraction suffixes —
see [Date/time extraction suffixes](#date-part-lookups) below; no other field type gets them
(`FieldError`, same as any other unrecognized lookup suffix).

All the `contains`/`startswith`/`endswith`/`iexact`/`i*` variants and `posix_regex`/`iposix_regex`
cast the left side to text first — they're technically callable on a numeric field too, though that's
rarely what you want; a float or decimal reads as Postgres writes it on every backend (`2`, not `2.0`).
A `CitextField` stays `citext`, so these match regardless of case (`posix_regex` included).

```python
await Book.objects.filter(title__icontains="dispossessed")
await Book.objects.filter(published_at__year=1974)
await Author.objects.filter(created_at__range=(start, end))
await Author.objects.filter(created_at__range=(start, None))   # open-ended: created_at >= start
await Author.objects.filter(created_at__range=(None, None))    # every non-NULL row
```

`range` accepts `None` for either bound (or both) for an open-ended check instead of a full
`BETWEEN` — pass the tuple through as-is, don't special-case `None` yourself.
`in`/`not_in` take any iterable, like Django — a list, tuple, set, `frozenset`, `range`, generator,
`dict.keys()`/`.values()` — read exactly once; a string or bytes value raises `UnSupportedError`
instead of matching character by character. `field=None` and `field__iexact=None` mean
`field__isnull=True`.
`isnull`/`not_isnull` require an actual `bool` — passing a string (e.g. a raw, unparsed query
parameter) raises `UnSupportedError` rather than silently evaluating truthiness.

`posix_regex`/`iposix_regex` need a connection with regular expressions
(`features.supports_posix_regex`): every Postgres connection, and a SQLite one whose DB_URL has
`?install_regexp_functions=true`. On any other connection a filter using them raises
`UnSupportedError` before the query runs.

!!! warning "`posix_regex`/`iposix_regex` on SQLite: never pass unvalidated input"
    Postgres implements these with its native `~`/`~*` operators, but SQLite has no built-in regex
    engine — hare-orm falls back to Python's own `re.search()` there. Python's `re` isn't resistant
    to catastrophic backtracking: a pathological pattern can hang the whole connection matching
    against ordinary-length column data, regardless of the pattern's own length (ReDoS). Fine for a
    pattern you wrote yourself; never forward a user-supplied pattern to this lookup on SQLite.

    Translating the pattern costs time and memory of its own, before any row is matched. Python's
    `re` has no POSIX classes, so each `[[:upper:]]`/`[[:punct:]]`/... becomes an explicit list of
    every matching Unicode range - several thousand characters of `re` source per class. A pattern of
    under 1000 characters made of such classes (`[[:upper:]]` repeated 90 times) grows to over
    600000 characters and takes about a second and several megabytes to compile, on every distinct
    pattern (only the most recent patterns are cached). `iposix_regex` also expands every letter and
    range into its case variants. Limit the length and content of any pattern you didn't write, or
    run such matching on Postgres.

    On SQLite the pattern is matched like a Postgres (UTF8) advanced regular expression: `.` also
    matches a newline, `$` matches only the end of the text, and the POSIX classes (`[[:upper:]]`,
    `[[:alpha:]]`, `[[:space:]]`, ...) cover the whole Unicode range (`[[:digit:]]` is `0-9`, as on
    Postgres). `iposix_regex` matches a pattern character against itself and its one-character
    uppercase and lowercase forms, like `~*`: `i` matches `I` but not the Turkish `İ`, while `İ`
    matches `i`; `[[:upper:]]`/`[[:lower:]]` match any letter.

`iexact`/`icontains`/`istartswith`/`iendswith` and `Upper()`/`Lower()` map case character by
character on both dialects, like Postgres: a character whose full case mapping would expand keeps
its one-character mapping or stays unchanged (`ß` and `ﬀ` stay, so `straße` doesn't match
`STRASSE`; `İ` lowercases to `i`).

## Date/time extraction suffixes {: #date-part-lookups }

`DateField`, `TimeField`, and `DatetimeField` each get only the extraction suffixes that make
sense for what they actually store:

| Field | Suffixes |
|---|---|
| `DateField` | `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day` |
| `TimeField` | `hour`, `minute`, `second`, `microsecond` |
| `DatetimeField` | all twelve, plus `date` and `time` |

`week` and `iso_year` follow ISO-8601, `week_day` counts 1 (Sunday) to 7 (Saturday) and
`iso_week_day` 1 (Monday) to 7, as in Django. A `DatetimeField`'s `date`/`time` compare its date or
time of day (a `date`/`time` value or its ISO text). Every suffix also takes a comparison after it:
`not`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in` and `range` (`published_at__year__gte=2020`,
`starts_at__date__range=(start, end)`, `created__week_day__in=[1, 7]`).

A suffix outside a field's own set (`a_date_field__hour=...`, `a_time_field__year=...`) isn't
registered at all — `FieldError`, the same as any other unrecognized lookup suffix, rather than
reaching the database with a meaningless extraction. No other field type gets any of these
either.

```python
await Book.objects.filter(published_at__year=1974)
await Event.objects.filter(scheduled_on__week=3)
await Shift.objects.filter(starts_at__hour=9)
await Order.objects.filter(created_at__date=date(2026, 1, 5), created_at__hour__lt=12)
```

### Time zone semantics on `DatetimeField` {: #time-zone-semantics-on-datetimefield }

A `DatetimeField` extraction reads the **wall-clock value hare-orm itself reads/writes** for the
field, not necessarily whatever zone the database session happens to report values in:

- **`use_tz=True`**: extracts in the configured `timezone` (`Hare.init(timezone=...)`) — the same
  zone `DatetimeField.from_db_value()` converts every read value into.
- **`use_tz=False`**: extracts in wall-clock time as originally written — the same naive value
  `DatetimeField.to_db_value()`/`from_db_value()` round-trip. On SQLite this is automatic (the
  column is stored as that same naive text, nothing to convert). On **Postgres**, the column is
  still `TIMESTAMPTZ` even under `use_tz=False` — Postgres itself always extracts in its own
  session zone otherwise, which silently disagrees with the local wall-clock value the writing
  process actually used whenever the two differ. hare-orm compensates by qualifying the
  extraction with the **local system time zone** (`AT TIME ZONE <IANA zone>`) instead of a fixed
  offset — a fixed offset would go wrong for a date on the other side of a DST transition from
  today.

  Determining the local system's IANA zone name requires the optional `tzlocal` dependency:

  ```bash
  pip install hare-orm[tzlocal]
  ```

  Without it, a `use_tz=False` date-part lookup against a `DatetimeField` on Postgres raises
  `ConfigurationError` with installation guidance — only on that specific combination (Postgres +
  `use_tz=False` + an extraction lookup); nothing else needs `tzlocal`, including `use_tz=True` and
  SQLite.

To take the parts in another zone for a block of code - a user's own zone, say - wrap it in
`Timezone.override()` (`from hare.utils import Timezone`), like Django's `timezone.override()`:

```python
with Timezone.override("Europe/Moscow"):
    orders_today = await Order.objects.filter(created_at__date=moscow_today).count()
```

Inside the block (and the tasks it starts) `Timezone.name()` returns that zone, so the date and time
lookups (`__date`, `__year`, `__hour`, ...), `Extract*` and `Trunc*` take their parts in it and a naive
datetime is read in it; datetimes still come back as the same moments. The zone is an IANA name or a
`ZoneInfo` - an unknown one, or a fixed offset without an IANA name, raises `ConfigurationError`. One
expression takes its own zone with `tzinfo=` - see
[Date functions](functions.md#date-functions).

## Forward `ForeignKeyField` / `OneToOneField` {: #forward-foreignkeyfield-onetoonefield }

A lookup on the relation's own name works on its key column, Django-style: `author__isnull=True`,
`author__in=[author1, author2]` (instances or key values, mixed freely), `author__not_in=[...]`,
`author__gt=5`, `author__in=Author.objects.filter(...)` - in `filter()`, `exclude()`, `Q` and `When`, the same
as `author_id__...`. An instance is replaced by its `to_field` value (the primary key unless the
relation sets `to_field=`); an unsaved instance or an instance of another model raises `QueryError`,
and so does such a lookup on a relation with a composite key (filter each key column instead). A name
that is a field of the related model (`author__name`) still filters through the join.

## Large `__in` lists {: #large-in-lists }

A long `__in`/`__not_in` list is bound as one parameter, so its length is not limited by the driver's
bind-parameter ceiling: Postgres uses `= ANY($1::type[])` from 20 values, SQLite
`IN (SELECT value FROM json_each(?))` from 20 values (`SQLITE_IN_JSON_ARRAY_THRESHOLD`), with the
same matching as the plain `IN (...)` form for integers, text, floats, `Decimal`, dates, datetimes,
`UUID`, `bool` and `bytes`. Such a list keeps one statement plan whatever its length. The same goes for a many-to-many or backward-FK lookup (`tags__in=`,
`children__not_in=`) and for `__in` on an annotation: one with no known field type (`RawSQL`, a
`Count()` compared in `HAVING`) is typed on Postgres by its values - integers as `BIGINT`, text as
`TEXT`, and so on; a list mixing types keeps the plain form. `Length()` and `ExtractYear()`/
`ExtractMonth()`/`ExtractDay()` are integers and `StringAgg()` is text whatever their argument, so
`__in` on them binds integers/text.

A composite primary key is filtered through `pk` like a single-column one: `pk=(1, 2)`, `pk__in=` a list of
tuples, a queryset, a `values_list("pk", flat=True)` or a union (`(a, b) IN (SELECT ...)`), and a relation
to it compares the whole key - `bay=obj`, `bay=(1, 2)`, `bay__in=`, `bay__not`, `bay__isnull`;
`update(bay=obj)` (or a tuple, or `None`) writes every key column. A composite primary key's
`pk__in=[(a, b), ...]` is one row-value check, `(a, b) IN ((1, 2), ...)`;
a long list is bound as one array per column on Postgres
(`IN (SELECT * FROM unnest($1::int[], $2::int[]))`, from 20 rows) and as one JSON array of rows on
SQLite (`IN (SELECT json_extract(value, '$[0]'), ... FROM json_each(?))`, from 20 values), so its
length isn't limited either. A composite many-to-many target's `__in`/`__not_in` works the same way.
Many-to-many `add()`/`remove()` and the `on_delete=PROTECT` check of a bulk delete handle any number
of objects too - `add()` splits its `INSERT` into as many statements as the bind-parameter ceiling
needs, in one transaction.

## `ManyToManyField` {: #manytomanyfield }

`exact`, `not`, `in`, `not_in` (all resolved through the through-table; for a target with a
composite primary key each value is its pk tuple or an instance). `isnull`/`not_isnull` are always available, in `filter()`,
`exclude()` and `Q`: `tags__isnull=True` matches a row with no linked related row. A link counts only
when both the related row and (for a `through=Model`) the through row pass their own soft-delete
and tenant scope, the same way a reverse FK lookup skips a soft-deleted related row.

## Backward FK relations {: #backward-fk-relations }

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull`. For an owner with a composite primary key a value is
an instance or its pk tuple (`cemps=obj`, `cemps__in=[obj, (1, 2)]`), compared column by column.

Like Django, `relation=None` on a to-many relation (many-to-many or reverse FK) is `relation__isnull=True` -
the rows with no related row; `relation__not=None` is `relation__isnull=False`.

A lookup on a to-many relation's own name (`tags=tag`, `tags__in=[...]`, `tags__not=...`, `books__isnull=...`)
is the same lookup on the related primary key (`tags__id=tag.id`), like Django: within one `.filter()` call it
shares the `JOIN` of the other lookups through the relation (`filter(tags=1, tags__name="y")` needs one tag
matching both), a separate `.filter()` call gets its own `JOIN` (`filter(tags=1).filter(tags=2)` matches a row
linked to both - also further along a path, `Task.objects.filter(emp__tags=1).filter(emp__tags=2)`), and the aggregate fan-out checks see it like any other `JOIN` - `filter(tags__in=[...])
.annotate(n=Count("tags"))` counts only the matching tags. A many-to-many lookup compares the through
table's link column, so the related table itself isn't joined.

## `JSONField` {: #jsonfield }

A completely separate set (the generic set above does **not** apply): `exact`, `not`, `in`,
`not_in`, `isnull`, `not_isnull`, `gt`, `gte`, `lt`, `lte`, `range` (in `jsonb` order - see
[`F("data__key")`](expressions.md)), `contains`, `contained_by`, `has_key`, `has_keys`,
`has_any_keys`, `filter` (a nested dict-path lookup).

Any other name after the field is a key, as Django's key transforms: `filter(data__owner__name="a")`,
`filter(data__score__gt=10)`, `filter(data__tags__0="x")`, `exclude(data__owner__isnull=True)` read the
JSON value at the path and take its lookups, the same as a filter on
[`F("data__owner__name")`](expressions.md). A lookup name right after the field
(`data__contains`) is that lookup of the whole value; a key named like a lookup is reached through
`__filter` (`data__filter={"contains": 1}`). The same paths work into a JSON-valued annotation
(`annotate(summary=JSONObject(...)).filter(summary__total__gte=10)`) and after a relation
(`filter(owner__data__plan="pro")`). Every one of them works the same on Postgres and SQLite: on
SQLite they follow the jsonb rules through `json_extract()` and a few built-in SQL functions hare
registers on each connection. `__filter={...}` supports nested operator keys:
`not`, `isnull`, `not_isnull`, `has_key`, `has_keys`, `has_any_keys`, `in`, `not_in`, `gte`, `gt`,
`lte`, `lt`, `range`, `contains`, `startswith`, `endswith`, `iexact`, `icontains`, `istartswith`,
`iendswith`, `year`, `quarter`, `month`, `week`, `day`, `hour`, `minute`, `second`.

The comparison type follows the value: a number compares numerically, a string as text
(`{"name__gt": "b"}`), a bool as a boolean, a `date`/`datetime` as a timestamp. A
timezone-aware `datetime` compares as `timestamptz`, keeping the stored string's own UTC offset;
a naive one compares the wall-clock digits, ignoring any stored offset. A stored value of a
different JSON type never matches - including a string under a date comparison that isn't an
ISO-8601 date/datetime (or names a day its month doesn't have): such a row simply doesn't match.
A `dict`/`list` value compares the jsonb value at the path (`{"a": {"x": 1}}`, `{"a__in": [[1, 2]]}`);
only `exact`/`not`/`in`/`not_in` accept one. `contains`/`startswith`/`icontains`/... take a
string (another value raises `QueryError`) and match only a stored string. An operator can also be written as a nested
single-key dict - `{"enabled": {"not": False}}` is the same as `{"enabled__not": False}` (a
one-key dict whose key is an operator name is always read as an operator; match such a literal
object with `__contains` instead). The values of one `in`/`not_in` list must all be of one type,
otherwise `QueryError` is raised - except `None`, which matches a JSON `null` or a missing key
(`{"a__in": [1, None]}` matches the number `1`, a `null` and no `a` at all; `not_in` drops them).
A digit path segment is an object key or an array index; as an index it only matches inside an
array, never a scalar. A negative index counts from the array's end (`{"scores__-1": 30}`, also
`F("data__scores__-1")`).

`exact`/`not`/`in`/`not_in` on the whole column compare JSON values on both dialects - key order,
whitespace and `1` vs `1.0` don't matter (SQLite canonicalizes both sides, Postgres compares jsonb);
`None` in an `in`/`not_in` list stands for a SQL `NULL` column. `contains`/`contained_by` follow
jsonb `@>`/`<@`: an object contains another when every key of the other is there with a contained
value, an array contains another when each of its elements is contained in some element (order and
repeats don't matter), and only a top-level array also contains a bare scalar
(`data__contains="bar"` matches `["foo", "bar"]`).

```python
await Widget.objects.filter(values__contains=["approved"])
await Widget.objects.filter(config__filter={"enabled": {"not": False}})
```

### Key existence: `has_key` / `has_keys` / `has_any_keys` {: #key-existence }

Postgres jsonb `?` / `?&` / `?|`, with the same results on SQLite. They test the **top-level** keys
of the stored object (a nested key doesn't count):

| Lookup | Value | Matches rows where |
|---|---|---|
| `data__has_key="breed"` | a string | the key exists |
| `data__has_keys=["a", "b"]` | list of strings | **all** the keys exist (an empty list matches every row that has a document) |
| `data__has_any_keys=["a", "b"]` | list of strings | **at least one** key exists (an empty list matches nothing) |

Inside `__filter`, the same three work as operator keys on a nested path —
`data__filter={"pet__has_key": "breed"}` tests the object at `data->'pet'`, and a bare
`data__filter={"has_key": "pet"}` tests the column itself. A key that exists with a JSON `null`
value counts as existing. A SQL `NULL` column (no document at all) never matches any of the three.
Note that, as in Postgres itself, `?` also matches a string element of a top-level array or a
top-level scalar string equal to the key.

```python
await Pet.objects.filter(data__has_key="breed")
await Pet.objects.filter(data__has_keys=["breed", "age"])
await Pet.objects.filter(data__has_any_keys=["breed", "color"])
await Pet.objects.filter(data__filter={"owner__has_key": "phone"})
```

!!! warning "`__isnull` on a JSON path means no value there: JSON `null` and an absent key both count"
    `__filter={"key__isnull": True}` (and `{"key": None}`) is built on the `->>` text extraction,
    which returns SQL `NULL` in **three** situations: the key exists with JSON `null`, the key is
    absent from the object, and the whole JSON column is SQL `NULL`. `isnull=True` matches all
    three; `isnull=False` matches only a present, non-null value. This is the established
    semantics, not a bug (Django's jsonb lookups share the ambiguity). To tell the cases apart,
    combine `isnull` with `has_key`:

    ```python
    # key present, value is JSON null
    Q(data__filter={"key__isnull": True}) & Q(data__has_key="key")
    # key absent from the object (or no document at all)
    Q(data__filter={"key__isnull": True}) & ~Q(data__has_key="key")
    # key present with a real (non-null) value
    Q(data__filter={"key__isnull": False})
    ```

## Custom lookups {: #custom-lookups }

Register your own with [`Field.register_lookup()`](../extending/custom-lookups.md) — the
hardcoded suffixes above always take priority, a custom lookup can't shadow one of them. Every lookup
of a field, with the value it takes and the dialects that run it, is listed by
[`get_lookups()`](describing-filters.md#get_lookups).

`Q`, `F`, `Case`/`When`, `Exists`/`OuterRef`/`Subquery`, `RawSQL`, window functions, and
`hare.query.functions` (`Count`/`Sum`/`Max`/`Min`/`Avg`/string functions/writing your own) are
described on their own page — see [Expressions](expressions.md).

## NULL semantics {: #null-semantics }

`exclude()`/`~Q(...)` follow Django's own documented `exclude()` semantics: a row stays in the
result **unless the excluded condition is definitely `TRUE`** — both `FALSE` and `NULL`/`UNKNOWN`
(SQL's three-valued logic) count as "not matched", not just `FALSE`. Concretely, for a nullable
column:

```python
await Player.objects.exclude(nick="n1")   # keeps a row where nick IS NULL - NULL isn't "n1"
await Player.objects.exclude(score__gt=1) # keeps a row where score IS NULL - NULL isn't > 1
await Player.objects.exclude(team=some_team)          # keeps a row where team IS NULL (no team at all)
await Player.objects.exclude(team__rank=1)            # same - crossing the relation, already NULL-safe
```

This applies uniformly to a direct column and to a relation crossed by the lookup. It also applies
to a compound condition (`exclude(Q(a) | Q(b))`) as a whole, not leaf-by-leaf: the row stays unless
the WHOLE combined condition evaluates to `TRUE`.

A non-nullable column is unaffected — there's no NULL row for it to ever keep, so the generated SQL
is unchanged.

An aggregate annotation filtered in `HAVING` follows the same rule: `annotate(total=Sum("books__pages"))
.exclude(total__gt=500)` keeps an author without books, whose `SUM` is `NULL`.

The same "treat NULL like Python's `not in`/`!=` would" principle extends to two related lookups:

- `field__not_in=<queryset/Subquery>` / `exclude(field__in=<queryset/Subquery>)`: a NULL row inside
  the subquery's own result doesn't empty the whole result (as SQL's own `NOT IN (..., NULL)`, which
  is always `UNKNOWN`, would) — a value that genuinely isn't in the subquery's non-NULL results is
  kept, matching `x not in [1, None]` in plain Python. `field__in`/`.filter()` with a literal Python
  list handle an embedded `None` the same way.
- `field__not=F(other_field)` (or any other expression on the right-hand side): compared via SQL's
  `IS DISTINCT FROM` (`IS NOT` on SQLite, same semantics) instead of a plain `<>` — NULL-safe in
  both directions, so a row is kept whenever the two sides genuinely differ, including when only
  one of them is NULL, and excluded when both are NULL (two NULLs aren't "distinct"). A literal
  value on the right (`field__not="foo"`) is unaffected - only an expression triggers this.

## Field names from user input {: #field-names-from-user-input }

Values passed to `filter()`/`exclude()` are always bound as parameters, but field **names** -
`order_by()`, `values()`/`values_list()`, `filter()`/`exclude()` keys, `select_related()`/
`prefetch_related()`, `only()` - are trusted: hare-orm resolves every relation path you give it and
puts no limit on its length. Every hop adds a JOIN (a many-to-many hop two), and a path crossing
several to-many relations multiplies the rows the database has to produce, so a name taken straight
from a request (`?sort=company__docs__company__docs__...`) lets a client make the database do an
arbitrary amount of work - and read fields you never meant to expose (`?sort=owner__password_hash`
leaks it through the ordering). Like Django, there is no built-in depth limit; map user input to
field names through a whitelist instead:

```python
SORTABLE_FIELDS = {"title": "title", "-title": "-title", "created": "created_at", "-created": "-created_at"}

async def list_articles(sort: str) -> list[Article]:
    ordering = SORTABLE_FIELDS.get(sort)
    if ordering is None:
        raise ValueError(f"unsupported sort key {sort!r}")
    return await Article.objects.all().order_by(ordering)
```

For each whitelisted key, [`get_lookup_info()`](describing-filters.md) tells what it crosses and the
type of value it takes, so a query parameter can be parsed before it reaches `.filter()`.

On SQLite a query joining more than 64 tables fails with an `OperationalError` that says so.

A name written into the SQL text - an output name of `values()`/`annotate()`, a JSON key of a path
(`data__key`, `F("data__key")`, `data__filter={...}`) - can't contain a null byte (`"\x00"`): it raises
`ValidationError` before the statement is sent, on every backend. On PostgreSQL a raw SQL text holding
one (`RawSQL(...)`, `execute(...)`) raises `ValidationError` too, instead of a driver error that
looks like a lost connection; pass such a value as a parameter.
