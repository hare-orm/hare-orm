# Field types

The fields of `hare.fields` — the arguments every field takes, the fields holding plain values, enum
fields, generated columns and composite primary keys — and where the fields of one database live.

## <a id="the-base-field-class"></a>The base `Field` class

Every field subclasses `hare.fields.Field[TValue]`. Its constructor kwargs are shared by every field type
below:

```python
class Field:
    def __init__(
        self,
        *,
        source_field: str | None = None,
        generated: bool = False,
        primary_key: bool | None = None,
        null: bool = False,
        default: Any = None,
        db_default: Any = NOT_PROVIDED,
        unique: bool = False,
        db_index: bool | None = None,
        description: str | None = None,
        validators: list[Validator | Callable[[Any], None]] | None = None,
        sensitive: bool = False,
        **kwargs: Any,
    ) -> None: ...
```

| Argument | Meaning |
|---|---|
| `source_field` | Override the database column name (default: the field's Python attribute name). Two fields of one model mapping to the same column raise `ConfigurationError`. |
| `generated` | A database-generated column (`SERIAL`, `AUTOINCREMENT`, ...) — see [`generated`](#generated) |
| `primary_key` | The column is the table's primary key (indexed, and unique by the `PRIMARY KEY` constraint). `null=True` + `primary_key=True` raises `ConfigurationError`. |
| `null` | Nullable column. |
| `default` | A Python-side default: a value, a plain callable, or an **async** callable — resolved on `save()` — see [`default`](#default) |
| `db_default` | A real database-level `DEFAULT`: a static value, or a `SqlDefault`/`Now`/`RandomHex`/`UuidV7` instance (`hare.fields.db_defaults`) — see [`db_default`](#db-default) |
| `unique` | Unique constraint. |
| `db_index` | Adds a database index for this column by itself. On by default for a `ForeignKeyField` — see [Relations](relations.md#index-on-the-key-column). |
| `description` | Written as the column's database comment. |
| `validators` | A list of `Validator` instances and/or plain callables — see [Validators](validators.md). |
| `sensitive` | Marks secret data (credentials, tokens, personal data) — see [Sensitive fields](encrypted-and-sensitive-fields.md#sensitive-fields). No effect on the database schema. |

A keyword argument the field doesn't take raises `TypeError` naming it. The arguments that go into the
schema are checked: `description` and `source_field` of another type than a text, and `max_length` of
another than an `int`, raise `ConfigurationError`. A flag (`null`, `unique`, `generated`,
`primary_key`, `db_index`, `sensitive`) is read for its truth, as Python reads it, and `validators` is a
list — neither is checked. A field name can't
contain `__`, which separates the parts of a lookup path (`author__name`), nor end in `_` —
`from___gt` would read as the field `from` and the lookup `_gt`: such a field, or a `related_name` like
it, raises `ConfigurationError` when the model is declared, as does an empty column or table name or one
with a null byte, and a `description`, text `default` or `db_default` with a null byte — no SQL text can
hold it, and a migration would fail only where it writes it. A column named after a keyword keeps its name
through `source_field` (`from_field = fields.IntField(source_field="from")`), as
[`inspectdb`](../migrations/inspectdb.md) writes it.

Every field also exposes: `to_db_value(value, instance)`, `from_db_value(value)`, `validate(value)`,
`required` (a property — `True` when non-nullable, no default, not generated, no `db_default`),
`deconstruct()` (the field's class path and constructor arguments — what a migration file writes and the
autodetector compares), `get_annotation()` (the Python type of the field's value for a schema: its enum
for an enum field, `| None` when nullable — what the pydantic creator, the Litestar DTO and request-query
parameters read) and `constraints` (a dict of JSON-Schema-style constraints, e.g.
`max_length`/`ge`/`le`).

What type of field it is, without checking its class:

| Attribute | Value |
|---|---|
| `relation_type` | `RelationType` (`hare.fields`) of a relation — `FOREIGN_KEY`, `ONE_TO_ONE`, `MANY_TO_MANY`, `BACKWARD_FOREIGN_KEY`, `BACKWARD_ONE_TO_ONE`; `None` for a field holding a plain value, including the key column of a forward relation (`author_id`). |
| `encrypted` | `True` for an [encrypted field](encrypted-and-sensitive-fields.md#encrypted-fields) — its column holds ciphertext, so it can't be compared by value, ordered, grouped or aggregated. |
| `enum_type` | The enum class of an `IntEnumField`/`CharEnumField`; `None` for any other field. |
| `model` / `model_field_name` | The model the field is bound to and its attribute name there. |

### <a id="generated"></a>`generated`

A database-generated column (`SERIAL`, `AUTOINCREMENT`, ...). A field class without generation support
raises `ConfigurationError`. A generated integer field outside the primary key only maps an existing
column (e.g. a PostgreSQL `IDENTITY` column) — schema generation and migrations raise
`ConfigurationError` for it, so keep such a model `Meta.managed = False`.

### <a id="default"></a>`default`

A Python-side default: a value, a plain callable, or an **async** callable — resolved on `save()`. Not
part of the database schema. The value (or a callable's result) is converted the same way an explicitly
passed one is, so memory holds the type a later read returns: `default="red"` on a `CharEnumField` is the
enum member, `default=1.5` on a `DecimalField` is `Decimal("1.50")`, a naive `datetime` becomes aware in
the configured zone.

A migration file writes the value as code: a literal, a list, tuple, dict, set or frozenset of them, a
`Decimal`, a `UUID`, a date or time, an IP address or network (`ipaddress`), an enum member, a module-level
function or class, or an object with `deconstruct()`. Any other value — a `Fraction`, a `deque`, an
instance of a class of your own — makes `makemigrations` raise `ConfigurationError` rather than write a
file that can't be loaded; give such a value a `deconstruct()`, or a function returning it as the default.

### <a id="db-default"></a>`db_default`

A real database-level `DEFAULT`: a static value, or a `SqlDefault`/`Now`/`RandomHex`/`UuidV7` instance
(`hare.fields.db_defaults`). Can't be a callable. Setting both `default=` and `db_default=` on the same
field emits `RedundantDbDefaultWarning` — `db_default` never actually applies once `default` is set,
since a value is always assigned before the `INSERT` is sent (except on a
`ForeignKeyField`/`OneToOneField` with `on_delete=SET_DEFAULT` and a real constraint, where the database
itself reads `db_default` on delete — no warning there).

### <a id="db-default-helpers"></a>`db_default` helpers (`hare.fields.db_defaults`)

```python
class Book(Model):
    copies = fields.IntField(db_default=SqlDefault("0"))    # raw SQL, verbatim in DDL
    added_at = fields.DatetimeField(db_default=Now())      # STATEMENT_TIMESTAMP() on PostgreSQL
    barcode = fields.CharField(max_length=36, db_default=RandomHex())  # dialect-specific random hex
```

`SqlDefault(sql: str)` emits its `sql` string verbatim into `generate_schemas()`/migration DDL — **never
build one from untrusted input**. On SQLite an expression other than a literal or a signed number
(`SqlDefault("40 + 2")`) is wrapped in parentheses, the form SQLite's `DEFAULT` requires. `Now()` and
`RandomHex()` are ready-made subclasses for the two most common cross-dialect cases; `RandomHex()` picks
the right expression per dialect on its own (`(lower(hex(randomblob(16))))` on SQLite,
`md5(random()::text)` on PostgreSQL). A subclass of your own — its SQL written by a renderer a dialect
registers for it, as `RandomHex`'s — is written into a migration by its class and called without
arguments; one taking arguments overrides `deconstruct()`. On SQLite `Now()` writes the same text a
Python-side `DatetimeField` value is stored as (UTC with a `+00:00` suffix under `use_timezone=True`,
naive local time under `use_timezone=False`), so exact/`__in`/range filters and `get_or_create()` match a
row it filled. On a `DateField` or `TimeField`, `Now()` fills in the current date or wall-clock time in
the configured zone (the system's local zone under `use_timezone=False`) on every database — the value a
Python-side write of `Timezone.localtime()` would store, a `TimeField`'s with the same offset a naive
time gets — so `filter(day=today)` finds the row. The zone is taken when the DDL is generated. On SQLite,
a zone with DST rules is applied by a function hare registers on its own connections (`hare_local_now`),
so rows inserted into such a table by another SQLite client need the column's value given explicitly.

`UuidV7()` fills a `UUIDField` with a version 7 UUID the database generates per row — time-ordered, so a
row inserted later gets a greater UUID and an index over a UUID primary key grows at its end instead of
at random places:

```python
class Review(Model):
    id = fields.UUIDField(primary_key=True, db_default=UuidV7())
```

PostgreSQL 18 writes it as `uuidv7()`; an older PostgreSQL server and SQLite have no such function, and
creating the column raises `UnSupportedError` before any DDL (`Features.supports_uuid_v7`). After
`create()` the instance holds the UUID the database generated.

A version 7 UUID can come from Python too, on every database: `default=uuid.uuid7` on Python 3.14, or the
function of a library on 3.12 and 3.13 — `uuid_utils.uuid7`, `uuid6.uuid7`. The UUID is made before the
`INSERT`, so the instance holds it before `create()` returns, and a database without generated keys —
ClickHouse — takes it as any other value. ClickHouse orders a UUID by its lower 64 bits first, so a version
7 key doesn't sort by time there; order by a date column. A migration writes the function by its import
path:

```python
import uuid

class Review(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid7)
```

A static `db_default` datetime/time literal is rendered the way the same value is written from Python: on
SQLite a `DatetimeField` default is stored as UTC text (`2020-01-02 03:00:00+00:00`); on PostgreSQL a
naive `DatetimeField` default under `use_timezone=False` carries the local system offset and a
`TimeField` default carries its offset (a naive one gets the configured zone's standard offset under
`use_timezone=True`, UTC under `use_timezone=False`).

A field with a `db_default` and no value of its own holds `DatabaseDefault` (`hare.fields`) on a new
instance: the `INSERT` leaves its column out, so the database fills it. The marker is false;
`isinstance(value, DatabaseDefault)` tells it from other false values. `bulk_create()` leaves out a
column every object leaves to the database, and raises `QueryError` for a column only some of them leave
to it.

## <a id="data-fields"></a>Data fields (`hare.fields`)

| Field | Python type | Extra arguments | SQL type (default / PostgreSQL) |
|---|---|---|---|
| `IntField` | `int` | `primary_key` sets `generated=True` by default | `INT` / `INTEGER`, `SERIAL` when generated — constraints `ge=-2^31, le=2^31-1` |
| `BigIntField` | `int` | — | `BIGINT` / `BIGSERIAL` when generated — int64 bounds |
| `SmallIntField` | `int` | — | `SMALLINT` / `SMALLSERIAL` when generated — int16 bounds |
| `PositiveSmallIntField` | `int` | — | constraints `ge=0, le=int16 max` |
| `PositiveIntField` | `int` | — | constraints `ge=0, le=int32 max` |
| `PositiveBigIntField` | `int` | — | constraints `ge=0, le=int64 max` |
| `CharField` | `str` | **`max_length: int`** (required, ≥1) | `VARCHAR(max_length)`; auto-appends a `MaxLengthValidator` |
| `TextField` | `str` | — | `TEXT`; accepts `unique=True`/`db_index=True` and can appear in a `UniqueConstraint`/`Meta.indexes` (both PostgreSQL and SQLite index `TEXT` natively) |
| `EmailField` | `str` | `max_length=254`, `lowercase: bool` | `VARCHAR(max_length)`; an email address, its domain written lowercase — see [below](#format-fields) |
| `URLField` | `str` | `max_length=2048`, `schemes=("http", "https")` | `VARCHAR(max_length)`; a URL of one of `schemes` — see [below](#format-fields) |
| `SlugField` | `str` | `max_length=50`, `allow_unicode: bool` | `VARCHAR(max_length)`, indexed by default; letters, digits, `-` and `_` — see [below](#format-fields) |
| `PhoneField` | `str` | `region: str` | `VARCHAR(16)`; a phone number written in E.164 form — see [below](#format-fields) |
| `BooleanField` | `bool` | — | `BOOL` (SQLite: `INT`); takes a `bool` alone — `0`/`1` or a text raises `ValidationError` |
| `DecimalField` | `Decimal` | **`max_digits: int`, `decimal_places: int`** (both required, `max_digits ≥ 1`, `0 ≤ decimal_places ≤ max_digits`) | `DECIMAL(max_digits, decimal_places)` on PostgreSQL and ClickHouse, no column on SQLite ([below](#decimal-field-sqlite)); a text is parsed, a `bool` raises `ValidationError` |
| `DatetimeField` | `datetime.datetime` | `auto_now: bool`, `auto_now_add: bool` (mutually exclusive) | `TIMESTAMP` (PostgreSQL: `TIMESTAMPTZ`) |
| `DateField` | `datetime.date` | — | `DATE` |
| `TimeField` | `datetime.time` | `auto_now`, `auto_now_add` (the local wall clock with the configured zone's standard offset — the same offset a naive time gets) | `TIME` (PostgreSQL: `TIMETZ`); a `timedelta` or the text `24:00` raises `ValidationError` |
| `TimeDeltaField` | `datetime.timedelta` | — | `BIGINT` (stored as microseconds); a number given instead of a `timedelta` raises `ValidationError` |
| `FloatField` | `float` | — | `DOUBLE PRECISION` (SQLite: `REAL`) |
| `JSONField[T]` | `dict` / `list` (or a Pydantic model class) | `encoder`, `decoder`, `field_type` (for OpenAPI docs) | `JSON` (PostgreSQL: `JSONB`); no btree index (`unique=True`/`db_index=True`/`Index(fields=...)`), but a non-btree `Meta.indexes` entry such as `GinIndex(fields=[...])` is accepted |
| `JSONTextField` | `dict` / `list` / any JSON value | as `JSONField` | `JSONField`'s column, but a `String` of the JSON text on ClickHouse, whose `JSON` type drops a key's null and a list at the top: every value reads back as written (the outbox's `payload` and `headers`) |
| `UUIDField` | `uuid.UUID` | `version` (a UUID of another version of RFC 9562 is refused wherever hare passes it to the database — in a write, a filter, the key of an existing row and a reference to it, `ValidationError` — a value the database computes itself, such as `F(...)` or `db_default`, isn't checked; `primary_key=True` without an explicit `default` auto-sets `default=uuid4`, `Uuid7.new` with `version=7`) | `CHAR(36)` (PostgreSQL: `UUID`); read back as a plain `uuid.UUID` on every driver |
| `BinaryField` | `bytes` (`bytearray`, `memoryview` too; anything else raises `ValidationError`) | — | `BLOB` (PostgreSQL: `BYTEA`); filters by equality, `__in`, `__isnull` and the comparisons (byte order) — a text lookup (`__contains`, ...) raises `FieldError` — set by `update()`, takes `unique=True`/`db_index=True` (a B-tree index — on PostgreSQL a value over about 2.7 KB can't be indexed) |

```python
class Book(Model):
    id = fields.UUIDField(primary_key=True)
    title = fields.CharField(max_length=300)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    tags = fields.JSONField(default=list, field_type=list[str])
    added_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
```

`auto_now` and `auto_now_add` are stamped by the write, one moment per call: every object of one
`bulk_create()`/`bulk_update()` and every row of one `update()`/`merge()`/`insert_from()` gets the same
moment. `auto_now` is stamped by every write, `auto_now_add` by an insert of an object holding no value
yet. An object gets the stamp before its values are written — a failed write puts the old value back, and
an object that left an `auto_now` field unloaded (`.only()`/`.defer()`) is stamped all the same. A write
given values (`update()`, `merge()`, `insert_from()`) keeps a value it is given for such a field.

### <a id="text-values"></a>Text

A text field (`CharField`, `TextField`, `SlugField`, an `EncryptedTextField`, PostgreSQL's
`CIText`/fixed-length text fields, ClickHouse's `FixedString`/`LowCardinality` text) takes a `str` alone:
a number, a `UUID`, `bytes` or any other object raises `ValidationError` on every write path and as a
filter value — its `str()` would be stored or compared. A text lookup (`__contains`, `__icontains`,
`__startswith`, `__endswith`, `__iexact`, `__posix_regex`, `__search`, ...) takes a `str` alone on a field
of any type: `number__contains=5` raises, `number__contains="5"` searches the number's text. A value read
from a column is taken as the driver returns it.

### <a id="numbers"></a>Numbers

An integer field takes an `int` alone: a `bool`, a `float` or `Decimal` (`5.0` too) and a text (`"5"`)
raise `ValidationError` on every write path and in an equality or membership filter (`=`, `__not`,
`__in`, `__not_in`) — `int()` would read `"1_000"` or `"٥"` as numbers, and no fraction is a value of the
column. A comparison (`__gt`, `__gte`, `__lt`, `__lte`, `__range`) takes a `float` or a `Decimal` too and
compares it exactly: `number__gt=2.5` matches `3`.

A `FloatField` takes an `int`, a `float` or a `Decimal`; a `bool`, a text, and an `int` or a finite
`Decimal` past the float range (`Decimal("1e500")`) raise `ValidationError`. It
refuses `NaN` on SQLite on every write path and as a filter's operand — SQLite would store it, and bind
it, as NULL; PostgreSQL and ClickHouse store it. A filter by `NaN` compares as the database does: on
PostgreSQL `NaN` equals `NaN` and is greater than every number, on ClickHouse it equals nothing (`__not=NaN`
takes every row). A negative zero
(`-0.0`) keeps its sign on PostgreSQL and ClickHouse and reads back as `0.0` on SQLite, which stores it
so; the two compare equal everywhere.

`DecimalField.max_digits`/`decimal_places` are enforced against every value on `save()`, not just checked
at field-declaration time — `Decimal("12345.67")` on a `max_digits=6` field raises `ValidationError`, not
a silent truncation or a database-level surprise. A value of more decimal places is rounded half away
from zero on every database: `Decimal("2.345")` on a `decimal_places=2` field is `Decimal("2.35")`. An
`update()` from an expression stores what the database makes of it: PostgreSQL rounds half away from
zero, ClickHouse cuts the extra places. A negative zero (`Decimal("-0.00")`) reads back as
`Decimal("0.00")` — a decimal column holds no sign of zero.

### <a id="decimal-field-sqlite"></a>`DecimalField` (SQLite)

SQLite has no exact decimal type, so a `DecimalField` has no column there: a model declaring one raises
`UnSupportedError` when it is bound to a SQLite connection, and so does a query run on SQLite with a
`Decimal` value or a `Cast()` to a decimal in it — before any SQL. Keep money in whole minor units (an
`IntField` or a `BigIntField` of cents) on SQLite, or use PostgreSQL or ClickHouse, which store exact
decimals. [inspectdb](../migrations/inspectdb.md) reads a SQLite `DECIMAL` or `NUMERIC` column as a
`FloatField`: SQLite gives such a column NUMERIC affinity, which keeps an integer or a 64-bit float.

### <a id="date-and-time-values"></a>Date and time values

A `DatetimeField` accepts a `datetime`, a `date` (its first moment in the configured zone under
`use_timezone=True` — midnight, or the end of a DST gap that starts at midnight, as on 2024-09-08 in
`America/Santiago`; the same when a date is compared with a `DatetimeField`), an ISO 8601 string starting with
a full date (`2024-05-01`, `2024-05-01T10:00:00+03:00`); a `DateField` accepts a `date`, a `datetime` (its
date) or such a string. Anything else — a number (no epoch seconds), a bare `"2024"`, unparsable text — raises
`ValidationError` on construction and on every write path (`create()`, `bulk_create()`, `save()`, `update()`,
`bulk_update()`, `update_or_create()`), identically on every database; so does a `datetime` that falls outside
the datetime range once converted to the configured timezone or to UTC. Lookups such as `__year=2024` compare
an extracted number with an `int` (a text, a float or a `Decimal` raises `UnSupportedError`, as any lookup
given an argument of another sort does). A `TimeField` works on SQLite and ClickHouse too: its value is stored
as ISO text (`12:30:45`, with an aware time's own offset `12:30:45+03:00`), and comparisons, ordering and
`Min`/`Max` compare that text, as the database does: `10:00+03:00` sorts after `08:00+00:00` there, though it
is the earlier moment, which PostgreSQL's `TIMETZ` orders by the UTC time. To order moments, keep them in a
`DatetimeField`. Under `use_timezone=False` a datetime before 1970
is converted to and from the system's local time even on Windows, where `datetime.astimezone()` can't do it
(the zone named by the optional `tzlocal` is used there, or the system's offset at 1970-01-02 without it). On
PostgreSQL a naive value under `use_timezone=False` is bound as the instant of that local wall-clock time, so
asyncpg and rust_pg store and read back the same value. A `DatetimeField` mapped onto an existing PostgreSQL
`timestamp` (without time zone) column reads that column as UTC wall-clock time and writes the UTC wall clock
of the value, on both drivers — the conversion PostgreSQL itself applies between `timestamp` and `timestamptz`
in hare's UTC session.

### <a id="extreme-dates"></a>Extreme dates (PostgreSQL)

`datetime.min`/`datetime.max` and `date(1, 1, 1)`/`date(9999, 12, 31)` are stored as the days and
moments they are on every database — on PostgreSQL too (asyncpg and rust_pg alike), not as
`-infinity`/`infinity` — so `__year`/`__month`/`__day`, `Extract`/`Trunc` and `F("dt") ± timedelta(...)`
work on them. A PostgreSQL `infinity`/`-infinity` written by other means is read as the same Python
extremes: `date.max`/`date.min`, `datetime.max`/`datetime.min`. Under a time zone east of UTC
`datetime.max` (UTC) is past the datetime range once shown in it and is refused with `ValidationError`.
Under `use_timezone=False` a naive `datetime.min`/`datetime.max` is that moment in UTC, as an aware one
is, and reads back as the naive extreme — not a local wall clock, which east or west of UTC would fall
past the range.

### <a id="json-values"></a>JSON values

`JSONField` raises `ValidationError` (not a bare `ValueError`/`TypeError`) for a value the encoder can't
serialize, malformed JSON text read back from the column, or a value that doesn't match the declared
`field_type`. A declared `field_type` (a Pydantic model, `list[Model]`, `list[str]`, ...) is applied
through a Pydantic `TypeAdapter` on assignment, on `save()` and on read: a dict (or a list of dicts)
becomes the model right away, so memory holds the same value a later read returns, and list items are
validated one by one. `pydantic_model_creator()` puts that type into the schema. Without `field_type`
(including the static-only `JSONField[T]` generic) values stay plain JSON.

A migration writes `field_type` as code importing it: a class by the name its module gives it, a
parameterized type by its origin and arguments (`dict[str, list[int]]`, `int | None`,
`typing.Literal["a"]`, `typing.List[int]`). A type no import statement reaches — a class Pydantic
makes for `GenericModel[int]`, a `type` alias statement — is refused by `makemigrations` with
`ConfigurationError`; declare a subclass (`class IntItems(GenericModel[int]): pass`) and give that.

A value JSON can't hold raises `ValidationError` on every write path (`create()`, `save()`, `update()`,
`bulk_create()` including `use_copy=True`, `bulk_update()`): a `NaN`/`Infinity` float anywhere inside it. A
null byte inside a JSON value is stored as the database stores it where it can hold one; PostgreSQL
refuses it, and hare raises `ValidationError`. An int of any size is stored and read
back exactly — one outside the 64-bit range orjson handles is encoded and decoded by the standard library
instead. A `datetime`, `date`, naive `time` or `UUID` inside the value is written as the same ISO/UUID text
on both paths, and with orjson not installed at all.

`exact`/`not` on a whole `JSONField` compare as the database does: PostgreSQL compares `jsonb` values (key
order and `1` vs `1.0` don't matter), SQLite compares the stored text (they matter), a ClickHouse `JSON`
column is compared by ClickHouse after the value is cast to `JSON` (key order doesn't matter) and a
`String` column of JSON text compares text.

On SQLite a `JSONField` column is declared `JSON_TEXT` (TEXT affinity), so a top-level number is stored
as its JSON text: `12345678901234567890` and `1.0` read back unchanged.

PostgreSQL's `JSONB` keeps a number as a `numeric` and writes it without an exponent, so a float written
in exponent form with no fraction reads back as an `int` of the same value: `1e22` is
`10000000000000000000000`, `1e300` an `int` of 301 digits. `1.5e-7` and `3.0` read back as floats.

### <a id="generatedfield"></a>`GeneratedField`

```python
class GeneratedField:
    def __init__(
        self,
        expression: Expression | RawSQLTerm | dict[str, RawSQLTerm],
        output_field: Field[Any],
        stored: bool = True,
        **kwargs: Any,
    ) -> None: ...
```

A column the database computes from an expression over the row — `GENERATED ALWAYS AS (...) STORED`
or `VIRTUAL` — so its value is right after every write the database sees, made outside hare too:

```python
class Book(Model):
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField()
    stock_value = fields.GeneratedField(
        F("price") * F("quantity"), output_field=fields.DecimalField(max_digits=12, decimal_places=2)
    )
```

- `expression` — an expression of hare over the model's fields (`F`, functions, arithmetic; a relation's
  own column as `F("owner_id")` — one reading a related model raises `ConfigurationError` when the model
  is bound): each database's DDL writes it in its own SQL, a migration renaming a field it reads renames
  it there too, and a migration changing the type of a column it reads drops the generated column before
  the change and adds it after. Or `RawSQLTerm` of the SQL written into the DDL as it is (**never build it
  from untrusted input**), or one per dialect: `{"sqlite": RawSQLTerm(...), "postgresql": RawSQLTerm(...)}`;
  a dialect the mapping doesn't name raises `ConfigurationError`, and so does plain text. Raw SQL is yours:
  hare neither renames a field in it nor reads which columns it uses — a migration changing such a column
  is refused by the database where it refuses the change under a generated column.
- `output_field` — a field giving the column's type and how its value is read, compared, filtered and
  described in a schema.
  Its JSON form and ordering rules apply too, including JSON value ordering and the refusal to order
  ClickHouse `DynamicField` and `VariantField` columns. Ordering an `F()` annotation of the column
  follows the same rules as ordering the column directly.
- `stored=True` computes the value on write; `stored=False` — a `VIRTUAL` column, computed on read —
  works on SQLite and PostgreSQL 18, and an older PostgreSQL server raises `UnSupportedError`
  (`Features.supports_virtual_generated_columns`).

hare never writes the column: an `INSERT` reads the computed value back, an `UPDATE` reads it again (on a
database with `RETURNING`). A value given to it raises `QueryError` — in the constructor, `create()`,
`get_or_create()`, `update_or_create(defaults=...)`, `save(update_fields=[...])` and `update()`; copy an
instance with `obj.clone()`. A new instance — and a clone — holds no value of it until its row is written:
reading it raises `NoValuesFetched`, as does reading it after a write that reads nothing back (a
`bulk_create()` without `returning=True`, a database without `RETURNING`) until `refresh_from_db()`. A value
assigned to a loaded instance is replaced by the computed one on `save()`; `bulk_update()` reads the new value
back only with `returning=True`. It can't be the primary key and
takes no `validators=` (`ConfigurationError` — put them on `output_field`); it is `sensitive` when its
`output_field` is, unless it passes its own.

### <a id="format-fields"></a>Email, URL, slug and phone fields

```python
class Author(Model):
    email = fields.EmailField(unique=True)
    homepage = fields.URLField(null=True)
    slug = fields.SlugField(unique=True)
    phone = fields.PhoneField(null=True, region="US")
```

```python
class EmailField:
    def __init__(self, max_length: int = 254, *, lowercase: bool = False, **kwargs: Any) -> None: ...

class URLField:
    def __init__(
        self,
        max_length: int = 2048,
        *,
        schemes: Sequence[str] = ("http", "https"),
        **kwargs: Any,
    ) -> None: ...

class SlugField:
    def __init__(self, max_length: int = 50, *, allow_unicode: bool = False, **kwargs: Any) -> None: ...

class PhoneField:
    def __init__(self, *, region: str | None = None, **kwargs: Any) -> None: ...
```

Each is a `CharField` (`VARCHAR(max_length)`, a `MaxLengthValidator`) that checks the format of its
value, and converts it to one written form, on every write path — `create()`, `save()`, `update()`,
`bulk_create()` (with `use_copy=True` too), `bulk_update()`, `update_or_create()` — and for every filter
value compared with it as a whole (`email=`, `email__in=`, `email__not=`). A value that fails raises
`ValidationError` naming the field: `email: Invalid email address`. Text lookups (`__contains`,
`__startswith`, `__icontains`, ...) take any text. The validators passed in `validators=` run too, before
the format check. An instance keeps the value as it was assigned until it is read from the database
again; the database holds the written form.

| Field | Accepts | Written as |
|---|---|---|
| `EmailField` | what `EmailValidator` accepts: a local part of atoms joined by dots (`ann.lee+news`) or a quoted string (`"ann@home"`), `@`, then a host name with a dot (`example.com`) or an address literal (`[192.0.2.1]`); 320 characters at most | the domain lowercase — `Ann@Example.COM` becomes `Ann@example.com`; with `lowercase=True` the whole address, `ann@example.com` |
| `URLField` | what `URLValidator` accepts, with one of `schemes` (compared lowercase, so `HTTPS://` is `https`): a host name with a dot, `localhost`, an IPv4 or IPv6 address, an optional port, user info, path, query and fragment | as it is given |
| `SlugField` | ASCII letters, digits, hyphens and underscores (`my-first_post`); with `allow_unicode=True` any Unicode letter or digit too (`привет-мир`) | as it is given |
| `PhoneField` | without the `phonenumbers` package — a number in E.164 form only: `+`, a country code not starting with 0, 15 digits at most in all; with it (`pip install hare-orm[phone]`) — a number in any form it reads and finds valid: `+1 (650) 253-0000`, and with `region` a national number, `(650) 253-0000` | E.164: `+16502530000` (`VARCHAR(16)`) |

So a filter value is found whatever form it is given in: `Author.objects.filter(email="Ann@EXAMPLE.com")`
finds `Ann@example.com`, `filter(phone="(650) 253-0000")` finds `+16502530000` on a `region="US"` field.
The local part of an address keeps its case — a mail server may tell `Ann` from `ann`; use
`lowercase=True` when your addresses are case-insensitive, or `email__iexact=` to compare regardless of
case.

- `schemes` — lowercase scheme names.
- `SlugField` is indexed unless `db_index=False` is passed. A migration file writes `db_index=True` for
  it, and a field a migration file declares without `db_index` has no index — the same rule as for a
  `ForeignKeyField`.
- `region` — a region code `phonenumbers` knows (`"US"`, `"GB"`, its `SUPPORTED_REGIONS`); `region`
  without `phonenumbers` installed raises `ConfigurationError`. Which numbers `phonenumbers` finds valid
  follows its own metadata, so it can change with its version.

A Pydantic schema (`pydantic_model_creator()`) and the OpenAPI built from it describe them with
`format: email`, `format: uri`, a `pattern` of the slug's characters, and for a `PhoneField` without
`phonenumbers` a `pattern` of an E.164 number (with it a number is accepted in any form).

Turning a `CharField` into one of them is an `AlterField` that keeps the column (a `SlugField` adds its
index): the rows already stored are neither checked nor converted — they are on their next write.

### <a id="enum-fields"></a>Enum fields

Not classes — factory functions returning an `IntEnumFieldInstance` (a `SmallIntField`) or a
`CharEnumFieldInstance` (a `CharField`), so `type(Model.status) is not IntEnumField`:

```python
def IntEnumField(
    enum_type: type[IntEnumType],
    description: str | None = None,
    **kwargs: Any,
) -> IntEnumType: ...

def CharEnumField(
    enum_type: type[CharEnumType],
    description: str | None = None,
    max_length: int = 0,
    **kwargs: Any,
) -> CharEnumType: ...
```

- `IntEnumField` is built on `SmallIntField` — enum values must fit `int16` (1..32767 if
  `generated=True`, else the full `int16` range).
- `CharEnumField` is built on `CharField` — `max_length=0` autodetects the longest enum value's string
  length. **If you add a longer enum value later, bump `max_length` yourself** — it isn't recomputed
  automatically. An enum with non-string values (e.g. `ONE = 1`) works too: the column stores
  `str(member.value)` (`"1"`) and a read maps that text back to the member.
- Both auto-generate a `"NAME: value"`-style `description` listing every member, unless you pass your
  own.
- Both refuse a declaration that can't hold its enum (`ConfigurationError`): a class that is no enum, and
  for `CharEnumField` a `max_length` shorter than the longest value or a `default` that is neither a
  member nor a value of the enum.
- A number of no member — `7`, or a fraction such as `1.5` — raises `ValidationError` in a write and in a
  filter alike; so does a whole float (`1.0`) for an `IntEnumField`, which takes an `int` as an integer
  field does.
- Where the column holds any integer or text (PostgreSQL, SQLite), a `CHECK` keeps it to the values the
  members are stored as: a value of no member written around the field — by raw SQL, or computed by
  `update(level=F("level") + 1)` — raises `IntegrityError`. It is a `CheckConstraint` of an
  `EnumMembersCondition`, named `<field>_enum_members`, added to the model's constraints when it is
  bound: `makemigrations` writes it with the model and replaces it when the members change. A column of
  an enum type needs none — ClickHouse's `Enum8`/`Enum16` (`Features.keeps_enum_columns`), PostgreSQL's
  `NativeEnumField`.

```python
from enum import Enum

class Status(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"

class Book(Model):
    status = fields.CharEnumField(Status, default=Status.DRAFT)
```

### <a id="compositeprimarykey"></a>`CompositePrimaryKey`

```python
class CompositePrimaryKey:
    def __init__(self, *field_names: str, without_overlaps: bool = False) -> None: ...
```

Not a real field — no database column of its own. It makes two or more already-declared fields the
table's primary key:

```python
class Shelf(Model):
    store = fields.CharField(max_length=20)
    number = fields.IntField()
    pk = CompositePrimaryKey("store", "number")
```

Each named field must exist and must **not** itself be `primary_key=True`, `generated=True` or
`null=True`, and no other field of the model is `primary_key=True` (`ConfigurationError`). A relation of
the model to itself can't be a component either: its key would hold the relation itself. Nor can a
container value — an `ArrayField`, `MapField`, `TupleField`, `NestedField` or `JSONField`, in a composite
key or as the key itself: the ORM keeps the rows it reads and writes by their key (`ConfigurationError`);
make such fields unique with a `UniqueConstraint` instead. `Model.pk` becomes a tuple; `clone()` on such
a model resolves each component's own `default=`/ `db_default=` independently — only components with
neither need an explicit value in `pk=(...)`.

A component can be a foreign key, named by itself or by its key column —
`CompositePrimaryKey("book_id", "number")` and `CompositePrimaryKey("book", "number")` are the same key.
The relation's key column takes its place (a relation to a model with a composite key adds each of its
key columns, `edition_book_id`, `edition_number`); `Model.pk` holds the related key's value(s), and `pk=`
and `pk__in=` take them. The relation can't be `null=True`, and a `ManyToManyField` can't be a component
(`ConfigurationError`):

```python
class Edition(Model):
    book = fields.ForeignKeyField("models.Book", related_name="editions")
    number = fields.IntField()
    pk = CompositePrimaryKey("book_id", "number")

await Edition.objects.get(pk=(book.id, 2))
```

`without_overlaps=True` makes the last field a range (or multirange) two rows may not overlap in while
the other fields are equal — one booking of a reading room at a time — enforced by the database
(`PRIMARY KEY (room, booked WITHOUT OVERLAPS)`, PostgreSQL 18):

```python
class RoomBooking(Model):
    room = fields.CharField(max_length=20)
    booked = DateRangeField()  # hare.dialects.postgresql.fields
    reader = fields.ForeignKeyField("models.Reader")
    pk = CompositePrimaryKey("room", "booked", without_overlaps=True)
```

An older PostgreSQL server and SQLite raise `UnSupportedError` before any DDL
(`Features.supports_without_overlaps`); a scalar field before the range needs the `btree_gist` extension,
which the schema editor and the migration autodetector add. A foreign key can't target such a key
(`ConfigurationError`), and changing `without_overlaps` of an existing table isn't autodetected — it
needs a hand-written migration. PostgreSQL infers no `ON CONFLICT` target from such a key and updates no
row conflicting with it, so `bulk_create(on_conflict=[...])` naming its fields and `update_fields` with
`on_conflict_constraint="pk"` raise `QueryError`; the conflicting rows are skipped with
`ignore_conflicts=True` — with no target, or `on_conflict_constraint="pk"`. A
`UniqueConstraint(without_overlaps=True)` is the same, named by its `name`.

> [!NOTE]
> A foreign key **can** target a model with a composite primary key — see
> [Relations](relations.md#targeting-a-composite-primary-key) — with real table-level `FOREIGN KEY` DDL,
> cascades, joins, and `prefetch_related()` support.

## <a id="postgresql-only-fields"></a>Fields of particular databases

`HStoreField`, `TSVectorField`, `PostGISField`, `CitextField` and the range field family live in
`hare.dialects.postgresql` — see [PostgreSQL fields](../dialects/postgresql/fields.md). Of `hare.fields`,
`ArrayField` runs on PostgreSQL and ClickHouse, `MapField`, `TupleField` and `NestedField` on ClickHouse,
and `IPAddressField` everywhere (`inet` on PostgreSQL, `IPv6` on ClickHouse, its text elsewhere) — see
[ClickHouse types](../dialects/clickhouse/types.md). Addresses compare, order and aggregate (`Min`/`Max`)
as each database orders its column: PostgreSQL's `inet` puts an IPv4 address before every IPv6 one, then
orders by the address's bytes; SQLite compares the text; ClickHouse orders its `IPv6` values its own way. The
text lookups (`__startswith`) match an IPv4 address by its IPv4 text, ClickHouse's `::ffff:` form included. An
IPv6 address naming a zone (`fe80::1%eth0`) raises `ValidationError` on PostgreSQL and ClickHouse, whose
address columns hold no zone; SQLite keeps it in the text. `VectorField` lives in `hare.vectors` and works on
PostgreSQL and SQLite — see [Vector search](../dialects/search-and-geodata/vector-search.md).

## <a id="writing-your-own-field"></a>Writing your own field

See [Custom fields](../extending/custom-fields.md).
