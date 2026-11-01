# Filters and lookups

A filter is a keyword argument `field__lookup=value` of `filter()`, `exclude()`, `get()` or `Q()`: the
path names a field, through relations when needed, and the lookup at its end says how the value is
compared. This page lists the lookups every field has and the ones a field type adds, how relations,
`FilteredRelation`, `Lateral`, `AsofJoin`, `JsonTable` and JSON fields are filtered, and how `NULL`
compares.

A value is taken as it is at the call: a list or set given to `filter()`, `exclude()` or `Q()` is copied,
and a dict whole, so changing it before the query runs changes no query — the same for the values of
`update()`, its lists copied whole too.

## <a id="generic-set"></a>The generic set of every ordinary field

Registered for **every** direct model field, regardless of its Python type:

`exact` (no suffix), `not`, `in`, `not_in`, `isnull`, `not_isnull`, `gte`, `lte`, `gt`, `lt`, `range`
(exactly 2 values), `contains`, `startswith`, `endswith`, `iexact`, `icontains`, `istartswith`,
`iendswith`, `search`, `posix_regex`, `iposix_regex`, and on PostgreSQL `trigram_similar`,
`trigram_word_similar`, `trigram_strict_word_similar` (see [Trigram similarity and
unaccent](../dialects/postgresql/trigrams.md)).

A `CharField`/`TextField`/`CitextField` also takes the `unaccent` transform on PostgreSQL:
`filter(name__unaccent__icontains="saldana")` compares the text without its accents.

`DateField`/`TimeField`/`DatetimeField` additionally get their own date/time extraction suffixes — see
[Date/time extraction suffixes](#date-part-lookups) below; no other field type gets them (`FieldError`,
same as any other unrecognized lookup suffix).

A key with an empty part — `name__`, `__name`, `author____name` — raises `FieldError` on every database,
in `filter()`, `exclude()`, `distinct()` and `group_by()` as in `order_by()` and `values()`.

All the `contains`/`startswith`/`endswith`/`iexact`/`i*` variants and `posix_regex`/`iposix_regex` cast the
left side to text first — they're technically callable on a numeric field too, though that's rarely what you
want; a float, decimal or boolean is matched as the database's own text of the value (`CAST ... AS
VARCHAR`): a float is `2` on PostgreSQL and `2.0` on SQLite, a boolean `true`/`false` on PostgreSQL and
`1`/`0` on SQLite. A
`CitextField` stays `citext`, so these match regardless of case (`posix_regex` included).

`contains`/`startswith`/`endswith` and the `i...` variants also take a column or an expression —
`filter(body__icontains=F("title"))`, `filter(path__startswith=Concat(F("root"), Value("/")))`: the
pattern is built in SQL from the value's text, its `%`, `_` and `\` matched literally as in a string
value. A row whose value is NULL matches none of them.

```python
await Book.objects.filter(title__icontains="dispossessed")
await Book.objects.filter(published_at__year=1974)
await Author.objects.filter(created_at__range=(start, end))
await Author.objects.filter(created_at__range=(start, None))   # open-ended: created_at >= start
await Author.objects.filter(created_at__range=(None, None))    # every non-NULL row
```

`range` accepts `None` for either bound (or both) for an open-ended check instead of a full `BETWEEN` —
pass the tuple through as-is, don't special-case `None` yourself. `in`/`not_in` take any iterable — a
list, tuple, set, `frozenset`, `range`, generator, `dict.keys()`/`.values()` — read exactly once; a
string or bytes value raises `UnSupportedError` instead of matching character by character. `field=None`
and `field__iexact=None` mean `field__isnull=True`, `field__not=None` — `field__isnull=False`.
`field__not=value` keeps a row whose value is NULL, as `exclude(field=value)` does (see
[NULL semantics](#null-semantics)). `isnull`/`not_isnull` require an actual `bool` — passing a string
(e.g. a raw, unparsed query parameter) raises `UnSupportedError` rather than silently evaluating
truthiness.

`posix_regex`/`iposix_regex` need a connection with regular expressions
(`features.supports_posix_regex`): every PostgreSQL connection, and a SQLite one whose database URL has
`?install_regexp_functions=true`. On any other connection a filter using them raises `UnSupportedError`
before the query runs.

On SQLite the pattern is a Python `re` pattern, with `re`'s syntax and semantics — PostgreSQL's regular
expression syntax is not translated (`[[:digit:]]`, `\y` and `(?n)` are not available there), and
`iposix_regex` is `re.IGNORECASE`. An invalid pattern raises `OperationalError` with Python's message
(`invalid regular expression: ...`).

> [!WARNING]
> **`posix_regex`/`iposix_regex` on SQLite: never pass unvalidated input**
>
> PostgreSQL implements these with its native `~`/`~*` operators, but SQLite has no built-in regex engine
> — hare falls back to Python's own `re.search()` there. Python's `re` isn't resistant to catastrophic
> backtracking: a pathological pattern can hang the whole connection matching against ordinary-length
> column data, regardless of the pattern's own length (ReDoS). Fine for a pattern you wrote yourself;
> never forward a user-supplied pattern to this lookup on SQLite.
> Limit the length and content of any pattern you didn't write, or run such matching on PostgreSQL.

`iexact`/`icontains`/`istartswith`/`iendswith` and `Upper()`/`Lower()` map case as the database does:
PostgreSQL by its own rules, SQLite with its `UPPER()`/`LOWER()` and `LIKE`, which fold the ASCII
letters only (`iexact="ПРИВЕТ"` doesn't match `привет`), and ClickHouse with `upperUTF8()`/`lowerUTF8()`.

## <a id="date-part-lookups"></a>Date/time extraction suffixes

`DateField`, `TimeField`, and `DatetimeField` each get only the extraction suffixes that make sense for
what they actually store:

| Field | Suffixes |
|---|---|
| `DateField` | `year`, `iso_year`, `quarter`, `month`, `week`, `week_day`, `iso_week_day`, `day` |
| `TimeField` | `hour`, `minute`, `second`, `microsecond` |
| `DatetimeField` | all twelve, plus `date` and `time` |

`week` and `iso_year` follow ISO-8601, `week_day` counts 1 (Sunday) to 7 (Saturday) and `iso_week_day` 1
(Monday) to 7. A `DatetimeField`'s `date`/`time` compare its date or time of day (a `date`/`time` value
or its ISO text). Every suffix also takes a comparison after it: `not`, `gt`, `gte`, `lt`, `lte`, `in`,
`not_in` and `range` (`published_at__year__gte=2020`, `starts_at__date__range=(start, end)`,
`created__week_day__in=[1, 7]`). The value of a part is an `int` — another type raises `UnSupportedError`,
and an int past 32 bits, which no part of a date reaches, `ValidationError`.

A suffix outside a field's own set (`a_date_field__hour=...`, `a_time_field__year=...`) isn't registered
at all — `FieldError`, the same as any other unrecognized lookup suffix, rather than reaching the
database with a meaningless extraction. No other field type gets any of these either.

```python
await Book.objects.filter(published_at__year=1974)
await Book.objects.filter(published_at__week=3)
await Loan.objects.filter(lent_at__hour=9)
await Loan.objects.filter(lent_at__date=date(2026, 1, 5), lent_at__hour__lt=12)
```

### <a id="time-zone-semantics-on-datetimefield"></a>Time zone semantics on `DatetimeField`

A `DatetimeField` extraction reads the **wall-clock value hare itself reads/writes** for the field, not
necessarily whatever zone the database session happens to report values in:

- **`use_timezone=True`**: extracts in the configured `timezone` (`Hare.init(timezone=...)`) — the same
  zone `DatetimeField.from_db_value()` converts every read value into.
- **`use_timezone=False`**: extracts in wall-clock time as originally written — the same naive value
  `DatetimeField.to_db_value()`/`from_db_value()` round-trip. On SQLite this is automatic (the column is
  stored as that same naive text, nothing to convert). On **PostgreSQL**, the column is still
  `TIMESTAMPTZ` even under `use_timezone=False` — PostgreSQL itself always extracts in its own session
  zone otherwise, which silently disagrees with the local wall-clock value the writing process actually
  used whenever the two differ. hare compensates by qualifying the extraction with the **local system
  time zone** (`AT TIME ZONE <IANA zone>`) instead of a fixed offset — a fixed offset would go wrong for
  a date on the other side of a DST transition from today.

  Determining the local system's IANA zone name requires the optional `tzlocal` dependency:

  ```bash
  pip install hare-orm[tzlocal]
  ```

  Without it, under `use_timezone=False`, what has to name the machine's zone raises
  `ConfigurationError` with installation guidance: on PostgreSQL a date-part lookup against a
  `DatetimeField`; on ClickHouse a date-part lookup, `Trunc()`/`Extract()` of a naive value and `Now()`
  for a `DateField`/`TimeField`. Nothing else needs `tzlocal` — `use_timezone=True` and SQLite
  included.

To take the parts in another zone for a block of code — a user's own zone, say — wrap it in
`Timezone.override()` (`from hare.time import Timezone`):

```python
with Timezone.override("Europe/Moscow"):
    loans_today = await Loan.objects.filter(lent_at__date=moscow_today).count()
```

Inside the block (and the tasks it starts) `Timezone.name()` returns that zone, so the date and time
lookups (`__date`, `__year`, `__hour`, ...), `Extract*` and `Trunc*` take their parts in it and a naive
datetime is read in it; datetimes still come back as the same moments. The zone is an IANA name or a
`ZoneInfo` — an unknown one, or a fixed offset without an IANA name, raises `ConfigurationError`. One
expression takes its own zone with `tzinfo=` — see [Date functions](functions.md#date-functions).

## <a id="forward-foreignkeyfield-onetoonefield"></a>Forward `ForeignKeyField` / `OneToOneField`

A lookup on the relation's own name works on its key column: `author__isnull=True`, `author__in=[author1,
author2]` (instances or key values, mixed freely), `author__not_in=[...]`, `author__gt=5`,
`author__in=Author.objects.filter(...)` — in `filter()`, `exclude()`, `Q` and `When`, the same as
`author_id__...`. An instance is replaced by its `to_field` value (the primary key unless the relation
sets `to_field=`); an unsaved instance or an instance of another model — a concrete subclass of the
related model too, whose rows are another table's — raises `QueryError` (on a to-many relation as well),
and so does such a lookup on a relation with a composite key (filter each key column instead). A name
that is a field of the related model (`author__name`) still filters through the join.

## <a id="large-in-lists"></a>Large `__in` lists

A long `__in`/`__not_in` list is bound as one parameter, so its length is not limited by the driver's
bind-parameter ceiling: PostgreSQL uses `= ANY($1::type[])` from 20 values, SQLite
`IN (SELECT value FROM json_each(?))` from 20 values (`SQLITE_IN_JSON_ARRAY_THRESHOLD`), with the same
matching as the plain `IN (...)` form for integers, text, floats, `Decimal`, dates, datetimes, `UUID`,
`bool` and `bytes`. Such a list keeps one statement plan whatever its length. The same goes for a
many-to-many or backward-FK lookup (`tags__in=`, `children__not_in=`) and for `__in` on an annotation:
one with no known field type (`RawSQL`, a `Count()` compared in `HAVING`) is typed on PostgreSQL by its
values — integers as `BIGINT`, text as `TEXT`, and so on; a list mixing types keeps the plain form.
`Length()` and `ExtractYear()`/ `ExtractMonth()`/`ExtractDay()` are integers and `StringAgg()` is text
whatever their argument, so `__in` on them binds integers/text.

A composite primary key is filtered through `pk` like a single-column one: `pk=(1, 2)`, `pk__in=` a list
of tuples, a queryset, a `values_list("pk", flat=True)` or a union (`(a, b) IN (SELECT ...)`), `pk__not=`
and `pk__not_in=` their negation, and a relation to it compares the whole key — `bay=obj`, `bay=(1, 2)`,
`bay__in=`, `bay__not`, `bay__isnull` (and the same through `bay__pk`: `bay__pk__not`, `bay__pk__not_in`
keep a row without a `bay`, as `bay__not` does); `update(bay=obj)` (or a tuple, or `None`) writes every
key column. A composite primary key's `pk__in=[(a, b), ...]` is one row-value check,
`(a, b) IN ((1, 2), ...)`; a long list is bound as one array per column on PostgreSQL
(`IN (SELECT * FROM unnest($1::int[], $2::int[]))`, from 20 rows) and as one JSON array of rows on SQLite
(`IN (SELECT json_extract(value, '$[0]'), ... FROM json_each(?))`, from 20 values), so its length isn't
limited either. A list of a relation's keys holding a key with a `None` part (a nullable relation's
`(None, None)`) or an expression is compared key by key instead — one condition per key, joined with
`OR` — and its values are bound one by one: a long one is a long statement, which a database bounding the
depth of an expression (SQLite) may refuse. Within a composite relation's membership list, name every
component: use `(None, None)` for a two-column NULL key; a bare `None` raises `QueryError`.
A composite many-to-many target's `__in`/`__not_in` works the
same way. Many-to-many
`add()`/`remove()` and the `on_delete=PROTECT` check of a bulk delete handle any number of objects too —
`add()` splits its `INSERT` into as many statements as the bind-parameter ceiling needs, in one
transaction.

## <a id="manytomanyfield"></a>`ManyToManyField`

`exact`, `not`, `in`, `not_in` (all resolved through the through-table; for a target with a composite
primary key each value is its pk tuple or an instance). `isnull`/`not_isnull` are always available, in
`filter()`, `exclude()` and `Q`: `tags__isnull=True` matches a row with no linked related row,
`tags__isnull=False` a row with at least one — an `EXISTS` test, so each row comes once, unlike
`tags__id__isnull=False` or `books__isnull=False`, which join the related rows. A link counts only when
both the related row and (for a `through=Model`) the through row pass their own soft-delete and tenant
scope, the same way a reverse FK lookup skips a soft-deleted related row.

## <a id="backward-fk-relations"></a>Backward FK relations

`exact`, `not`, `in`, `not_in`, `isnull`, `not_isnull`. For an owner with a composite primary key a value
is an instance or its pk tuple (`cemps=obj`, `cemps__in=[obj, (1, 2)]`), compared column by column.

`relation=None` on a to-many relation (many-to-many or reverse FK) is `relation__isnull=True` — the rows
with no related row; `relation__not=None` is `relation__isnull=False`.

A lookup on a to-many relation's own name (`tags=tag`, `tags__in=[...]`, `tags__not=...`,
`books__isnull=...`) is the same lookup on the related primary key (`tags__id=tag.id`): within one
`.filter()` call it shares the `JOIN` of the other lookups through the relation
(`filter(tags=1, tags__name="y")` needs one tag matching both), a separate `.filter()` call gets its own
`JOIN` (`filter(tags=1).filter(tags=2)` matches a row linked to both — also further along a path,
`Review.objects.filter(book__tags=1).filter(book__tags=2)`), and the aggregate fan-out checks see it like
any other `JOIN` — `filter(tags__in=[...]) .annotate(n=Count("tags"))` counts only the matching tags. A
many-to-many lookup compares the through table's link column, so the related table itself isn't joined.

## <a id="filtered-relation"></a>`FilteredRelation`

`FilteredRelation(relation_name, *, condition=Q(...))` (`hare.query.expressions`) joins a relation under
a name of its own with the condition in the `JOIN`'s `ON` clause: the name then reads only the related
rows that match it, while a row with none still comes, its values `NULL` — a `LEFT JOIN`:

```python
from hare.query.expressions import FilteredRelation, Q

in_print = FilteredRelation("books", condition=Q(books__in_print=True))
await Author.objects.alias(in_print=in_print).annotate(
    in_print_count=Count("in_print")
).values("name", "in_print_count")
await Author.objects.alias(in_print=in_print).filter(in_print__title__icontains="dune")
await Author.objects.alias(in_print=in_print).values("name", "in_print__title").order_by("in_print__title")
```

- Give it a name with `alias()` — `annotate()` does the same: it is a `JOIN`, never a selected value.
- The name starts paths like a relation does — in filters (`in_print__title`, `in_print__isnull=False`,
  `Q`, `exclude()`), `F()`, aggregates (`Count("in_print")`), `values()` and `order_by()`; a path may go
  on through the related model's relations (`in_print__tags__name`). A function given the path as a
  string reads it the same way (`Upper("in_print__title")`, `Max(Length("in_print__title"))`). The name
  alone reads the related primary key; a composite one has no single column, so reading or comparing the
  name alone raises `QueryError` naming its components — `__isnull` works.
- `relation_name` is a relation of the model or a path of them (`"books__reviews"` — the condition is on
  the last one); a forward, reverse or many-to-many relation alike. For a many-to-many relation the
  condition keeps the links to the rows it leaves out out of the `JOIN` too, so such a link adds no row.
- The relation's own name keeps its own `JOIN` — `filter(books__title="x")` beside the filtered name
  joins the table twice; the aggregate fan-out checks treat the filtered name as a to-many `JOIN` of its
  own (`Count("in_print")` beside `Count("books")` needs `distinct=True`).
- `condition` reads the relation's own fields, written through its name (`Q(books__in_print=True)`,
  `Q(books__price__lt=F("books__list_price"))`); a key or `F()` not starting with it raises `QueryError`,
  as does a condition through a further relation or an `Exists(...)`. A `relation_name` that isn't a
  relation raises `FieldError`, checked first. Both are checked when `alias()`/`annotate()` names the
  `FilteredRelation`, even one the query never reads.
- A query with a filtered relation keeps no [plan](query-plan-cache.md): its condition is part of a
  `JOIN`.

## <a id="lateral"></a>`Lateral`

`Lateral(queryset)` (`hare.query.expressions`) joins a `values()`/`values_list()` queryset as a `LATERAL`
subquery under a name of its own: the subquery runs for each row of the query, reads that row through
`OuterReference()`, and gives it several columns at once — what a `Subquery` annotation gives one column
of. A row the subquery returns nothing for still comes, its columns `NULL` — a
`LEFT JOIN LATERAL ... ON TRUE`:

```python
from hare.query.expressions import Lateral, OuterReference

best = Lateral(
    Book.objects.filter(author=OuterReference("pk")).order_by("-rating", "id").values("name", "rating")[:1]
)
await Author.objects.alias(best=best).values("name", "best__name", "best__rating")
# [{"name": "Tolkien", "best__name": "Rings", "best__rating": 5.0},
#  {"name": "Nobody", "best__name": None, "best__rating": None}, ...]
await Author.objects.alias(best=best).filter(best__rating__gte=4.5).order_by("-best__rating")
authors = await Author.objects.alias(best=best).annotate(best_name=F("best__name"), loud=Upper("best__name"))
```

The query it builds:

```sql
SELECT "author"."name", "best"."name", "best"."rating" FROM "author"
LEFT OUTER JOIN LATERAL (
  SELECT "name", "rating" FROM "book" WHERE "author_id" = "author"."id"
  ORDER BY "rating" DESC, "id" ASC LIMIT 1
) "best" ON TRUE
```

- Give it a name with `alias()` — `annotate()` does the same: it is a `JOIN`, never a selected value.
  Select a column on model instances by annotating it (`annotate(best_name=F("best__name"))`).
- `<name>__<column>` reads a column the queryset selects — its `values()` names or its `values_list()`
  fields — in filters (`best__rating__gte=4.5`, `best__name__isnull=True`, `Q`, `exclude()`), `F()`,
  functions (`Upper("best__name")`), aggregates (`Sum("best__rating")`), `values()`/`values_list()` and
  `order_by()`. A column it doesn't select raises `FieldError`, and so does the name alone in
  `values()`/`values_list()`.
- The columns keep their types: a model field's values decode as that field's, an annotation's as its
  output field's (`Count(...)` as an integer).
- A row comes once for each row the subquery returns — slice it (`[:1]`) for one; `[:3]` gives the three
  best books of each author as three rows, as a `JOIN` of a to-many relation would. With an aggregate
  inside (`values("author").annotate(books=Count("id")).values("books")`) the subquery returns one row.
  An unsliced `Lateral` or one of more rows repeats the rows even when nothing reads its name.
- Several `Lateral`s, and a `Lateral` beside a `FilteredRelation`, join side by side.
- `queryset` has to be a `values()`/`values_list()` queryset — another value raises `QueryError` when the
  `Lateral` is made.
- PostgreSQL only (`features.supports_lateral`): on another database a query reading the name raises
  `UnSupportedError` before any SQL is sent. A query with a `Lateral` keeps no
  [plan](query-plan-cache.md): its subquery is part of a `JOIN`.

## <a id="asof-join"></a>`AsofJoin`

`AsofJoin(source, on=Q(...))` (`hare.query.expressions`) joins a model's rows `ASOF` under a name of its
own: each row of the query gets the one row of `source` closest to it by an inequality, among the rows
equal to it by the other conditions — the last quote of a symbol at or before each trade. A row with no
such row still comes, its values `NULL` — an `ASOF LEFT JOIN`. `source` is a model — its class or its
`"app.Model"` label — or a queryset of its rows:

```python
from hare.query.expressions import AsofJoin, OuterReference, Q

quote = AsofJoin(Quote, on=Q(symbol=OuterReference("symbol"), quoted_at__lte=OuterReference("traded_at")))
await Trade.objects.alias(quote=quote).values("id", "quote__price")
# [{"id": 1, "quote__price": Decimal("1.00")}, {"id": 3, "quote__price": None}, ...]
await Trade.objects.alias(quote=quote).filter(quote__price__gt=1).order_by("traded_at")
await Trade.objects.alias(quote=AsofJoin(Quote.objects.filter(source="exchange"), on=...)).values("quote__price")
```

The query it builds:

```sql
SELECT "trade"."id", "quote"."price" FROM "trade"
ASOF LEFT JOIN "quote" "quote" ON "quote"."symbol" = "trade"."symbol" AND "quote"."quoted_at" <= "trade"."traded_at"
```

- `on` compares fields of the joined model with fields of the queried row, each read by
  `OuterReference()`: at least one equality (`symbol=OuterReference("symbol")`) and exactly one
  inequality — `__lt`, `__lte`, `__gt` or `__gte` — of the field the closest row is found by. Anything
  else — a plain value, another lookup, `|`, `~`, a nested `Q`, a field of a relation — raises
  `QueryError`.
- `source` is the joined model, or a queryset of its rows (`Quote.objects.filter(...)`), joined as a
  subquery; a `values()` queryset raises `QueryError`.
- Give it a name with `alias()` — `annotate()` does the same: it is a `JOIN`, never a selected value.
  `<name>__<field>` reads a field of the joined row in filters, `F()`, functions, aggregates,
  `values()`/`values_list()` and `order_by()`; the name alone reads its primary key (`quote__isnull=True`
  finds the rows with no closest row) — of a composite one only `__isnull`, anything else raising
  `QueryError` naming its components.
- At most one row is joined to each row: it repeats none, and `count()` counts the queried rows.
- Only on a database with `features.supports_asof_join` — ClickHouse: on another one a query reading the
  name raises `UnSupportedError` before any SQL is sent. A query with an `AsofJoin` keeps its
  [plan](query-plan-cache.md).

## <a id="array-join"></a>`ArrayJoin`

```python
class ArrayJoin:
    def __init__(self, array: str | F | Expression, *, left: bool = False) -> None: ...
```

`ArrayJoin(array)` (`hare.query.expressions`) repeats each row with each element of an array — `ARRAY JOIN`
— under the name `alias()`/`annotate()` gives it:

```python
from hare.query.expressions import ArrayJoin

tagged = Book.objects.alias(tag=ArrayJoin("tags"))   # tags = fields.ArrayField(fields.CharField(max_length=20))
await tagged.filter(tag__startswith="fan").values_list("id", "tag")
# [(1, "fantasy"), (4, "fantasy")]
await tagged.count()   # one row per element: 3 books with 2, 0 and 1 tags give 3
```

```sql
SELECT "book"."id","tag" FROM "book" ARRAY JOIN "book"."tags" AS "tag" WHERE toString("tag") LIKE 'fan%'
```

- `<name>` reads the element in filters (with the element field's lookups), expressions, `values()` and
  `order_by()`; `<name>__<path>` reads inside it as a path of the array's `base_field` does
  (`item__sku` of an array of named tuples).
- The rows are repeated whether or not anything reads the name: `count()`, `exists()`, the rows of
  `values()` and an aggregate read the repeated rows, as a JOIN of a to-many relation is read.
- A row with an empty array comes with none of its elements; with `left=True` it comes once, its element
  the default of the element's type (`""`, `0`).
- `array` is a field name, `F()` or an expression of an `ArrayField`; anything else raises `QueryError`.
- Only on a database with `features.supports_array_join` — ClickHouse: on another one a query with it
  raises `UnSupportedError` before any SQL is sent.

## <a id="json-table"></a>`JsonTable`

```python
class JsonTable:
    def __init__(
        self,
        document: str | Expression,
        path: str,
        columns: Mapping[str, Field[Any] | tuple[Field[Any], str]],
        *,
        ordinality: str | None = None,
    ) -> None: ...
```

The items of a JSON document as rows joined under the name `alias()`/`annotate()` gives it — the editions
of a book's details, the tags of a profile — read, filtered and ordered as columns (`JSON_TABLE`):

```python
from hare.query.expressions import JsonTable

editions = JsonTable(
    "details",                     # the JSONField - a name, F() or an expression
    "$.editions[*]",                # the JSON path of the items
    {
        "isbn": fields.CharField(max_length=13),            # read at $.isbn of each item
        "pages": fields.IntField(),
        "publisher": (fields.TextField(), "$.publisher.name"),  # another path
    },
    ordinality="position",          # numbers the items from 1
)
await Book.objects.alias(edition=editions).filter(edition__pages__gte=300).values("id", "edition__isbn", "edition__position")
```

```sql
SELECT "book"."id","edition"."isbn","edition"."position" FROM "book"
LEFT OUTER JOIN JSON_TABLE("book"."details", '$.editions[*]' COLUMNS ("position" FOR ORDINALITY,
  "isbn" VARCHAR(13) PATH '$.isbn', "pages" INT PATH '$.pages', "publisher" TEXT PATH '$.publisher.name')) "edition"
  ON TRUE
WHERE "edition"."pages">=$1
```

- `<name>__<column>` reads a column in filters (with the column's lookups), expressions, `values()` and
  `order_by()`; each column is read as its field reads a value — a value that doesn't convert to the
  column's type is NULL. The name alone selects nothing.
- A row comes once for each item. A row whose document has no item there — an empty array, a missing key,
  a NULL document — still comes once, its columns NULL. The rows are repeated whether or not anything
  reads a column: `count()`, `exists()`, the rows of `values()` and an aggregate read them.
- A column is a field, read at `$.<name>` of the item, or a `(field, path)` pair. A path not starting
  with `$`, a column name that isn't an identifier without `__`, an ordinality named like a column, a
  column that isn't a field raise `QueryError` when the `JsonTable` is made; reading a column it hasn't
  raises `FieldError`.
- PostgreSQL 17+ (`features.supports_json_table`): on another server a query reading the name raises
  `UnSupportedError` before any SQL is sent.

## <a id="jsonfield"></a>`JSONField`

A completely separate set (the generic set above does **not** apply): `exact`, `not`, `in`, `not_in`,
`isnull`, `not_isnull`, `gt`, `gte`, `lt`, `lte`, `range` (in the database's own order — see
[`F("data__key")`](expressions.md)), `contains`, `contained_by`, `has_key`, `has_keys`, `has_any_keys`,
`filter` (a nested dict-path lookup).

Any other name after the field is a key: `filter(data__owner__name="a")`, `filter(data__score__gt=10)`,
`filter(data__tags__0="x")`, `exclude(data__owner__isnull=True)` read the JSON value at the path and take
its lookups, the same as a filter on [`F("data__owner__name")`](expressions.md). Their
`gt`/`gte`/`lt`/`lte`/`range` compare in the database's own order of the JSON value at the path. PostgreSQL
uses `jsonb` order: a value of another JSON type compares by its type (`null` < string < number < boolean
< array < object), so `data__score__gt=10` matches `true`. SQLite compares the value `json_extract()` reads:
a string as itself, `true`/`false` as 1/0, a JSON `null` as `NULL`, an object or array as its minified
text; its order is numbers, then text. ClickHouse compares the `Dynamic` value it reads of the JSON text
(session setting `allow_suspicious_types_in_order_by=1`): it orders a `Dynamic` by the name of its type
first, then by value (a `Float64` `2.5` before an `Int64` `9`, `Bool` before numbers, strings after), and
comparing a path that holds values of no common type — strings and numbers, objects and numbers —
raises ClickHouse's own error (`NO_COMMON_TYPE`, `ILLEGAL_TYPE_OF_ARGUMENT`). A `date`/`datetime`
is its ISO-8601 text, compared as a string — `data__at__gt=date(...)` matches any later text. `__filter`
compares by the value's type instead (below).
A path can't name an empty key (`data__`, `data____isnull` raise `FieldError`, as any key with an empty
part does) — match one with `data__contains={'': value}` (PostgreSQL) or `data__has_key=''` (SQLite before
3.41 refuses the `$.""` path itself). A lookup name right after
the field (`data__contains`) is that lookup of the whole value; a key named like a lookup is reached
through `__filter` (`data__filter={"contains": 1}`). The same paths work into a JSON-valued annotation
(`annotate(summary=JSONObject(...)).filter(summary__total__gte=10)`) and after a relation
(`filter(owner__data__plan="pro")`). On SQLite they read the value through `json_extract()` and a few
built-in SQL functions hare registers on each connection. A text lookup of a path
(`data__code__contains="1"`) matches the database's own text of the value: on PostgreSQL what `->>` gives
(a string itself, `true`/`false`, a number's exact digits — `1e20` as `100000000000000000000` — an object
or an array as `jsonb` writes it), on SQLite what `json_extract()` gives; ClickHouse matches a number
written with an exponent as it is written. `__filter={...}` supports nested operator keys: `not`,
`isnull`, `not_isnull`, `has_key`, `has_keys`, `has_any_keys`, `in`, `not_in`, `gte`, `gt`, `lte`, `lt`,
`range`, `contains`, `startswith`, `endswith`, `iexact`, `icontains`, `istartswith`, `iendswith`, `year`,
`quarter`, `month`, `week`, `day`, `hour`, `minute`, `second`.

The comparison type follows the value: a number compares numerically, a string as text
(`{"name__gt": "b"}`), a bool as a boolean, a `date`/`datetime` as a timestamp. A timezone-aware
`datetime` compares as `timestamptz`, keeping the stored string's own UTC offset; a naive one compares
the wall-clock digits, ignoring any stored offset. A stored value of a different JSON type never matches
— including a string under a date comparison that isn't an ISO-8601 date/datetime (or names a day its
month doesn't have): such a row simply doesn't match. A `dict`/`list` value compares the jsonb value at
the path (`{"a": {"x": 1}}`, `{"a__in": [[1, 2]]}`); only `exact`/`not`/`in`/`not_in` accept one.
`contains`/`startswith`/`icontains`/... take a string (another value raises `QueryError`) and match only
a stored string. An operator can also be written as a nested single-key dict —
`{"enabled": {"not": False}}` is the same as `{"enabled__not": False}` (a one-key dict whose key is an
operator name is always read as an operator; match such a literal object with `__contains` instead). The
values of one `in`/`not_in` list must all be of one type, the bounds of a `range` single values and the key
a text, otherwise `QueryError` is raised — except
`None`, which matches a JSON `null` or a missing key (`{"a__in": [1, None]}` matches the number `1`, a
`null` and no `a` at all; `not_in` drops them). A digit path segment is an object key or an array index;
as an index it only matches inside an array, never a scalar. A negative index counts from the array's end
(`{"scores__-1": 30}`, also `F("data__scores__-1")`).

`exact`/`not`/`in`/`not_in` on the whole column compare as the database does. PostgreSQL compares `jsonb`
values, so key order, whitespace and `1` vs `1.0` don't matter. SQLite compares the stored text, so key
order and `1` vs `1.0` matter, and a container in `__filter` is compared as minified JSON text. On a
ClickHouse `JSON` column the value is cast to `JSON` and compared by ClickHouse (key order doesn't matter;
`__in` compares each value so, joined by `OR`; a value that is no object raises `ValidationError`, as the
column holds an object only); a `String` column of JSON text compares text. `None` in an `in`/`not_in`
list stands for a SQL `NULL` column. `contains`/`contained_by` are PostgreSQL's jsonb `@>`/`<@`: an object
contains another when every key of the other is there with a contained value, an array contains another
when each of its elements is contained in some element (order and repeats don't matter), and only a
top-level array also contains a bare scalar (`data__contains="bar"` matches `["foo", "bar"]`). SQLite's
and ClickHouse's JSON have no containment: there they raise `UnSupportedError` before any SQL is sent.

```python
await Book.objects.filter(details__contains={"formats": ["hardback"]})
await Book.objects.filter(details__filter={"illustrated": {"not": False}})
```

### <a id="key-existence"></a>Key existence: `has_key` / `has_keys` / `has_any_keys`

PostgreSQL jsonb `?` / `?&` / `?|`, `json_type()` on SQLite, `JSONHas()` on ClickHouse. They test the
**top-level** keys of the stored object (a nested key doesn't count):

| Lookup | Value | Matches rows where |
|---|---|---|
| `data__has_key="isbn13"` | a string | the key exists |
| `data__has_keys=["a", "b"]` | list of strings | **all** the keys exist (an empty list matches every row that has a document) |
| `data__has_any_keys=["a", "b"]` | list of strings | **at least one** key exists (an empty list matches nothing) |

Inside `__filter`, the same three work as operator keys on a nested path —
`data__filter={"publisher__has_key": "city"}` tests the object at `data->'publisher'`. With no path
before it an operator name is a key: `data__filter={"has_key": "publisher"}` compares the key `has_key`
with `"publisher"` — the column itself is tested by `data__has_key="publisher"`. A key that exists with a
JSON `null` value counts as existing. A SQL `NULL` column (no document at all) never matches any of the
three. Note that PostgreSQL's `?` also matches a string element of a top-level array or a top-level
scalar string equal to the key, where SQLite and ClickHouse find a key of an object only.

```python
await Book.objects.filter(details__has_key="isbn13")
await Book.objects.filter(details__has_keys=["isbn13", "pages"])
await Book.objects.filter(details__has_any_keys=["isbn13", "doi"])
await Book.objects.filter(details__filter={"publisher__has_key": "city"})
```

> [!WARNING]
> **`__isnull` of `__filter` means no value there: JSON `null` and an absent key both count**
>
> `__filter={"key__isnull": True}` (and `{"key": None}`) is built on the `->>` text extraction, which
> returns SQL `NULL` in **three** situations: the key exists with JSON `null`, the key is absent from the
> object, and the whole JSON column is SQL `NULL`. `isnull=True` matches all three; `isnull=False`
> matches only a present, non-null value. This is the established semantics of `->>`, not a bug. To tell
> the cases apart, combine `isnull` with `has_key`:
>
> ```python
> # key present, value is JSON null
> Q(data__filter={"key__isnull": True}) & Q(data__has_key="key")
> # key absent from the object (or no document at all)
> Q(data__filter={"key__isnull": True}) & ~Q(data__has_key="key")
> # key present with a real (non-null) value
> Q(data__filter={"key__isnull": False})
> ```
>
> A path's own lookups tell the two apart, on every database: `data__key__isnull=True` matches an
> absent key (and no document at all), `data__key=None` a JSON `null`.

## <a id="custom-lookups"></a>Custom lookups

Register your own with [`Field.register_lookup()`](../extending/custom-lookups.md) — the hardcoded
suffixes above always take priority, a custom lookup can't shadow one of them. Every lookup of a field,
with the value it takes and the dialects that run it, is listed by
[`get_lookups()`](describing-filters.md#get_lookups).

`Q`, `F`, `Case`/`When`, `Exists`/`OuterReference`/`Subquery`, `RawSQL`, window functions, and
`hare.query.functions` (`Count`/`Sum`/`Max`/`Min`/`Avg`/string functions/writing your own) are described
on their own page — see [Expressions](expressions.md).

## <a id="null-semantics"></a>NULL semantics

`exclude()`/`~Q(...)` keep a row in the result **unless the excluded condition is definitely `TRUE`** —
both `FALSE` and `NULL`/`UNKNOWN` (SQL's three-valued logic) count as "not matched", not just `FALSE`.
Concretely, for a nullable column:

```python
await Book.objects.exclude(subtitle="Book One")   # keeps a row where subtitle IS NULL - NULL isn't "Book One"
await Book.objects.exclude(rating__gt=1)            # keeps a row where rating IS NULL - NULL isn't > 1
await Book.objects.exclude(editor=some_reader)       # keeps a row where editor IS NULL (no editor at all)
await Book.objects.exclude(editor__name="Ann")       # same - crossing the relation, already NULL-safe
```

This applies uniformly to a direct column and to a relation crossed by the lookup. It also applies to a
compound condition (`exclude(Q(a) | Q(b))`) as a whole, not leaf-by-leaf: the row stays unless the WHOLE
combined condition evaluates to `TRUE`.

A non-nullable column is unaffected — there's no NULL row for it to ever keep, so the generated SQL is
unchanged.

An aggregate annotation filtered in `HAVING` follows the same rule:
`annotate(total=Sum("books__pages")) .exclude(total__gt=500)` keeps an author without books, whose `SUM`
is `NULL`.

The same "treat NULL like Python's `not in`/`!=` would" principle extends to two related lookups:

- `field__not_in=<queryset/Subquery>` / `exclude(field__in=<queryset/Subquery>)`: a NULL row inside the
  subquery's own result doesn't empty the whole result (as SQL's own `NOT IN (..., NULL)`, which is
  always `UNKNOWN`, would) — a value that genuinely isn't in the subquery's non-NULL results is kept,
  matching `x not in [1, None]` in plain Python. `field__in`/`.filter()` with a literal Python list
  handle an embedded `None` the same way. A NULL value matches `__in` exactly when the subquery
  contains a NULL; `__not_in`, `exclude(__in=...)` and `~Q(...__in=...)` then exclude it.
  A model queryset wrapped in `Subquery(...)` is narrowed to its primary key just like a bare queryset.
- `field__not=F(other_field)` (or any other expression on the right-hand side): compared via SQL's
  `IS DISTINCT FROM` (`IS NOT` on SQLite, same semantics) instead of a plain `<>` — NULL-safe in both
  directions, so a row is kept whenever the two sides genuinely differ, including when only one of them
  is NULL, and excluded when both are NULL (two NULLs aren't "distinct"). A literal value on the right
  (`field__not="foo"`) is unaffected — only an expression triggers this.

## <a id="field-names-from-user-input"></a>Field names from user input

Values passed to `filter()`/`exclude()` are always bound as parameters, but field **names** —
`order_by()`, `values()`/`values_list()`, `filter()`/`exclude()` keys, `select_related()`/
`prefetch_related()`, `only()` — are trusted: hare resolves every relation path you give it and puts no
limit on its length. Every hop adds a JOIN (a many-to-many hop two), and a path crossing several to-many
relations multiplies the rows the database has to produce, so a name taken straight from a request
(`?sort=company__docs__company__docs__...`) lets a client make the database do an arbitrary amount of
work — and read fields you never meant to expose (`?sort=owner__password_hash` leaks it through the
ordering). There is no built-in depth limit; map user input to field names through a whitelist instead:

```python
SORTABLE_FIELDS = {"title": "title", "-title": "-title", "published": "published_at", "-published": "-published_at"}

async def list_books(sort: str) -> list[Book]:
    ordering = SORTABLE_FIELDS.get(sort)
    if ordering is None:
        raise ValueError(f"unsupported sort key {sort!r}")
    return await Book.objects.all().order_by(ordering)
```

For each whitelisted key, [`get_lookup_info()`](describing-filters.md) tells what it crosses and the type
of value it takes, so a query parameter can be parsed before it reaches `.filter()`.

On SQLite a query joining more than 64 tables fails with an `OperationalError` that says so.

A value an integer field's type doesn't hold (`small__gt=100000` of a `SmallIntField`,
`tiny__in=[256, 1]` of a ClickHouse `UInt8Field`) raises `ValidationError` before the statement is sent,
on every database, as writing it does: the driver binds the parameter as the column's type and couldn't
send it. Compare a wider expression (`Cast("small", BigIntField())`) for such a value.

A name written into the SQL text — an output name of `values()`/`annotate()`, a JSON key of a path
(`data__key`, `F("data__key")`, `data__filter={...}`) — can't contain a null byte (`"\x00"`): it raises
`ValidationError` before the statement is sent, on every database. On PostgreSQL a raw SQL text holding
one (`RawSQL(...)`, `execute(...)`) raises `ValidationError` too, instead of a driver error that looks
like a lost connection; pass such a value as a parameter. On SQLite a pattern of a `LIKE`-family lookup
(`contains`, `startswith`, `iexact`, ...) holding a null byte raises `ValidationError` too, since SQLite
reads a pattern only up to it; every other value with a null byte is stored as the database stores it.
