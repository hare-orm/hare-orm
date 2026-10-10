# Field types

The fields of `hare.fields` — the arguments every field takes, the fields holding plain values, enum
fields, generated columns and composite primary keys — and where the fields of one database live.

## <a id="the-base-field-class"></a>The base `Field` class

Every field subclasses `hare.fields.Field[TValue]`. Its constructor kwargs are shared by every
field type below:

```python
def __init__(
    self,
    source_field: str | None = None,
    generated: bool = False,
    primary_key: bool | None = None,
    null: bool = False,
    default: Any = None,
    db_default: Any = DB_DEFAULT_NOT_SET,
    unique: bool = False,
    db_index: bool | None = None,
    description: str | None = None,
    validators: list[Validator | Callable[[Any], None]] | None = None,
    sensitive: bool = False,
    **kwargs: Any,
) -> None
```

| kwarg | Meaning |
|---|---|
| `source_field` | Override the DB column name (default: the field's Python attribute name). Two fields of one model mapping to the same column raise `ConfigurationError`. |
| `generated` | A DB-generated column (`SERIAL`, `AUTOINCREMENT`, ...). A field class without generation support raises `ConfigurationError`. A generated integer field outside the primary key only maps an existing column (e.g. a PostgreSQL `IDENTITY` column) — schema generation and migrations raise `ConfigurationError` for it, so keep such a model `Meta.managed = False`. |
| `primary_key` | The column is the table's primary key (indexed, and unique by the `PRIMARY KEY` constraint). `null=True` + `primary_key=True` raises `ConfigurationError`. |
| `null` | Nullable column. |
| `default` | A Python-side default: a value, a plain callable, or an **async** callable — resolved on `save()`. Not part of the DB schema. The value (or a callable's result) is converted the same way an explicitly passed one is, so memory holds the type a later read returns: `default="red"` on a `CharEnumField` is the enum member, `default=1.5` on a `DecimalField` is `Decimal("1.50")`, a naive `datetime` becomes aware in the configured zone, `default=5_000_000` on a `TimeDeltaField` is `timedelta(seconds=5)`. |
| `db_default` | A real database-level `DEFAULT`: a static value, or a `SqlDefault`/`Now`/`RandomHex`/`UuidV7` instance (`hare.fields.db_defaults`). Can't be a callable. Setting both `default=` and `db_default=` on the same field emits `RedundantDbDefaultWarning` — `db_default` never actually applies once `default` is set, since a value is always assigned before the `INSERT` is sent (except on a `ForeignKeyField`/`OneToOneField` with `on_delete=SET_DEFAULT` and a real constraint, where the database itself reads `db_default` on delete — no warning there). |
| `unique` | Unique constraint. |
| `db_index` | Adds a DB index for this column by itself. On by default for a `ForeignKeyField` — see [Relations](relations.md#index-on-the-key-column). |
| `description` | Written as the column's DB comment. |
| `validators` | A list of `Validator` instances and/or plain callables — see [Validators](validators.md). |
| `sensitive` | Marks secret data (credentials, tokens, personal data) — see [Sensitive fields](encrypted-and-sensitive-fields.md#sensitive-fields). No effect on the DB schema. |

A keyword argument the field doesn't take raises `TypeError` naming it.

Every field also exposes: `to_db_value(value, instance)`, `from_db_value(value)`,
`validate(value)`, `required` (a property — `True` when non-nullable, no default, not generated, no
`db_default`), `deconstruct()` (the field's class path and constructor arguments — what a migration
file writes and the autodetector compares), `get_annotation()` (the Python type of the field's value
for a schema: its enum for an enum field, `| None` when nullable — what the pydantic creator, the
Litestar DTO and request-query parameters read) and `constraints` (a dict of JSON-Schema-style constraints, e.g. `max_length`/`ge`/`le`).

What type of field it is, without checking its class:

| Attribute | Value |
|---|---|
| `relation_type` | `RelationType` (`hare.fields`) of a relation — `FOREIGN_KEY`, `ONE_TO_ONE`, `MANY_TO_MANY`, `BACKWARD_FOREIGN_KEY`, `BACKWARD_ONE_TO_ONE`; `None` for a field holding a plain value, including the key column of a forward relation (`author_id`). |
| `encrypted` | `True` for an [encrypted field](encrypted-and-sensitive-fields.md#encrypted-fields) — its column holds ciphertext, so it can't be compared by value, ordered, grouped or aggregated. |
| `enum_type` | The enum class of an `IntEnumField`/`CharEnumField`; `None` for any other field. |
| `model` / `model_field_name` | The model the field is bound to and its attribute name there. |

### <a id="db-default-helpers"></a>`db_default` helpers (`hare.fields.db_defaults`)

```python
class MyModel(Model):
    counter = fields.IntField(db_default=SqlDefault("0"))    # raw SQL, verbatim in DDL
    created_at = fields.DatetimeField(db_default=Now())      # STATEMENT_TIMESTAMP() on Postgres
    tracking_id = fields.CharField(max_length=36, db_default=RandomHex())  # dialect-specific random hex
```

`SqlDefault(sql: str)` emits its `sql` string verbatim into `generate_schemas()`/migration DDL —
**never build one from untrusted input**. On SQLite an expression other than a literal or a signed
number (`SqlDefault("40 + 2")`) is wrapped in parentheses, the form SQLite's `DEFAULT` requires. `Now()` and `RandomHex()` are ready-made subclasses for
the two most common cross-dialect cases; `RandomHex()` picks the right expression per dialect on
its own (`(lower(hex(randomblob(16))))` on SQLite, `md5(random()::text)` on PostgreSQL). On SQLite
`Now()` writes the same text a Python-side `DatetimeField` value is stored as (UTC with a `+00:00`
suffix under `use_timezone=True`, naive local time under `use_timezone=False`), so exact/`__in`/range filters
and `get_or_create()` match a row it filled. On a `DateField` or `TimeField`, `Now()` fills in the
current date or wall-clock time in the configured zone (the system's local zone under
`use_timezone=False`) on every backend — the value a Python-side write of `Timezone.localtime()` would
store, a `TimeField`'s with the same offset a naive time gets — so `filter(day=today)` finds the row.
The zone is taken when the DDL is generated. On SQLite, a zone with DST rules is applied by a
function hare registers on its own connections (`hare_local_now`), so rows inserted into such a
table by another SQLite client need the column's value given explicitly.

`UuidV7()` fills a `UUIDField` with a version 7 UUID the database generates per row — time-ordered,
so a row inserted later gets a greater UUID and an index over a UUID primary key grows at its end
instead of at random places:

```python
class Event(Model):
    id = fields.UUIDField(primary_key=True, db_default=UuidV7())
```

PostgreSQL 18 writes it as `uuidv7()`; an older PostgreSQL server and SQLite have no such function,
and creating the column raises `UnSupportedError` before any DDL (`Features.supports_uuid_v7`). After
`create()` the instance holds the UUID the database generated.

A version 7 UUID can come from Python too, on every database: `default=uuid.uuid7` on Python 3.14,
or the function of a library on 3.12 and 3.13 — `uuid_utils.uuid7`, `uuid6.uuid7`. The UUID is made
before the `INSERT`, so the instance holds it before `create()` returns, and a database without
generated keys — ClickHouse — takes it as any other value. A migration writes the function by its
import path:

```python
import uuid

class Event(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid7)
```

A static `db_default` datetime/time literal is rendered the way the same value is written from
Python: on SQLite a `DatetimeField` default is stored as UTC text (`2020-01-02 03:00:00+00:00`); on
PostgreSQL a naive `DatetimeField` default under `use_timezone=False` carries the local system offset and a
`TimeField` default carries its offset (a naive one gets the configured zone's standard offset under
`use_timezone=True`, UTC under `use_timezone=False`).

A field with a `db_default` and no value of its own holds `DatabaseDefault` (`hare.fields`) on a new
instance: the `INSERT` leaves its column out, so the database fills it. The marker is false;
`isinstance(value, DatabaseDefault)` tells it from other false values. `bulk_create()` leaves out a
column every object leaves to the database, and raises `QueryError` for a column only some of them
leave to it.

## <a id="data-fields"></a>Data fields (`hare.fields`)

| Field | Python type | Extra kwargs | SQL type (default / PostgreSQL) |
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
| `BooleanField` | `bool` | — | `BOOL` (SQLite: `INT`) |
| `DecimalField` | `Decimal` | **`max_digits: int`, `decimal_places: int`** (both required, `max_digits ≥ 1`, `0 ≤ decimal_places ≤ max_digits`) | `DECIMAL(max_digits, decimal_places)` |
| `DatetimeField` | `datetime.datetime` | `auto_now: bool`, `auto_now_add: bool` (mutually exclusive) | `TIMESTAMP` (PostgreSQL: `TIMESTAMPTZ`) |
| `DateField` | `datetime.date` | — | `DATE` |
| `TimeField` | `datetime.time` | `auto_now`, `auto_now_add` (the local wall clock with the configured zone's standard offset — the same offset a naive time gets) | `TIME` (PostgreSQL: `TIMETZ`) |
| `TimeDeltaField` | `datetime.timedelta` | — | `BIGINT` (stored as microseconds); a plain int is a number of microseconds on every write path |
| `FloatField` | `float` | — | `DOUBLE PRECISION` (SQLite: `REAL`) |
| `JSONField[T]` | `dict` / `list` (or a Pydantic model class) | `encoder`, `decoder`, `field_type` (for OpenAPI docs) | `JSON` (PostgreSQL: `JSONB`); no btree index (`unique=True`/`db_index=True`/`Index(fields=...)`), but a non-btree `Meta.indexes` entry such as `GinIndex(fields=[...])` is accepted |
| `UUIDField` | `uuid.UUID` | — (`primary_key=True` without an explicit `default` auto-sets `default=uuid4`) | `CHAR(36)` (PostgreSQL: `UUID`); read back as a plain `uuid.UUID` on every driver |
| `BinaryField` | `bytes` | — | `BLOB` (PostgreSQL: `BYTEA`); filters by equality, `__in` and the comparisons (byte order), set by `update()`, takes `unique=True`/`db_index=True` (a B-tree index — on PostgreSQL a value over about 2.7 KB can't be indexed) |

```python
class Widget(Model):
    id = fields.UUIDField(primary_key=True)
    name = fields.CharField(max_length=200)
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    tags = fields.JSONField(default=list, field_type=list[str])
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
```

An integer field accepts a whole-number `float`/`Decimal` (`5.0`, `Decimal("5")`) as the int, but
raises `ValidationError` for one with a fractional part (`5.7`) on every write path instead of
truncating it. As a filter value such a number is compared exactly: `number=1.5` and
`number__in=[1.5, 3]` never match `1`.

`DecimalField.max_digits`/`decimal_places` are enforced against every value on `save()`, not just
checked at field-declaration time — `Decimal("12345.67")` on a `max_digits=6` field raises
`ValidationError`, not a silent truncation or a database-level surprise.

**`DecimalField` on SQLite.** SQLite has no exact decimal type: the column is stored as text
(`VARCHAR(40)`, in fixed-point notation with the field's full scale — `2.0000`, `0.0000000001` —
the text PostgreSQL prints). Comparisons (`exact`/`gt`/`lt`/`in`/`range`/`not`, against a value or
another `DecimalField` via `F()`), ordering, `Min`/`Max` and a plain `F()` copy
(`update(price=F("price"))`, `annotate(copy=F("price"))`) compare and move that text as an exact
decimal, whatever `max_digits` is. A filter on an annotation compared with `Decimal` values
(`annotate(top=Max("price")).filter(top__gt=Decimal("10"))`, `__in`, `__range`) is exact too.
`contains`/`startswith`/`endswith`/`iexact` (and the `i...` variants) match the same text PostgreSQL
does: `2.0000` on a `decimal_places=4` field, and a `FloatField`'s shortest text (`2`, not `2.0`).
Arithmetic (`F("price") * 2`, `Sum`, `Avg`), `Coalesce`/`Case` over a `DecimalField`, and an
annotation compared with a mix of `Decimal` and other numbers go through a 64-bit double: about 15
significant digits survive. PostgreSQL uses a real arbitrary-precision `NUMERIC` and has no such
limit; use it for money math close to `max_digits`.

**Date and time values.** A `DatetimeField` accepts a `datetime`, a `date` (its first moment in the
configured zone under `use_timezone=True` — midnight, or the end of a DST gap that starts at midnight, as
on 2024-09-08 in `America/Santiago`; the same when a date is compared with a `DatetimeField`), an ISO
8601 string starting with a full date (`2024-05-01`, `2024-05-01T10:00:00+03:00`) or an epoch
integer; a `DateField` accepts a `date`, a `datetime` (its date) or such a string. Anything else — a
float, a bare `"2024"`, unparsable text, an epoch past the datetime range — raises
`ValidationError` on construction and on every write path (`create()`, `bulk_create()`, `save()`,
`update()`, `bulk_update()`, `update_or_create()`), identically on every backend; so does a
`datetime` that falls outside the datetime range once converted to the configured timezone or to
UTC. Lookups such as `__year="2024"` compare an extracted number and are unaffected. A `TimeField`
works on SQLite too: its value is stored as ISO text (`12:30:45`, with an aware time's own offset
`12:30:45+03:00`), and comparisons, ordering and `Min`/`Max` order it the way PostgreSQL orders
`TIMETZ` — by the UTC time (`10:00+03:00` is before `08:00+00:00`), two values being equal only with
the same wall clock and offset; a naive time counts as UTC. This applies to rows already stored as
well, since the stored text is unchanged. Under `use_timezone=False` a datetime before 1970 is converted to and from the
system's local time even on Windows, where `datetime.astimezone()` can't do it (the zone named by the
optional `tzlocal` is used there, or the system's offset at 1970-01-02 without it). On PostgreSQL a
naive value under `use_timezone=False` is bound as the instant of that local wall-clock time, so asyncpg
and rust_pg store and read back the same value. A `DatetimeField` mapped onto an existing PostgreSQL
`timestamp` (without time zone) column reads that column as UTC wall-clock time and writes the UTC
wall clock of the value, on both drivers — the conversion PostgreSQL itself applies between
`timestamp` and `timestamptz` in hare's UTC session.

**Extreme dates on PostgreSQL.** On PostgreSQL (asyncpg and rust_pg alike) `datetime.min`/`datetime.max`
— including the same instant written in another time zone — and `date(1, 1, 1)`/`date(9999, 12, 31)`
are stored as `-infinity`/`infinity`, and read back as those same Python values. Ordering and
comparisons (`__lt`, `__gte`, `order_by()`) treat them as the smallest/largest value, but
`__year`/`__month`/`__day` and the other date-part lookups never match them, and `F("dt") ±
timedelta(...)` leaves them infinite. SQLite stores them as ordinary dates, so the same lookups and
arithmetic work there. Don't use these values as real dates; keep them only as open-ended markers,
or use `NULL`.

`JSONField` raises `ValidationError` (not a bare `ValueError`/`TypeError`) for a value the encoder
can't serialize, malformed JSON text read back from the column, or a value that doesn't match the
declared `field_type`. A declared `field_type` (a Pydantic model, `list[Model]`, `list[str]`, ...) is
applied through a Pydantic `TypeAdapter` on assignment, on `save()` and on read: a dict (or a list
of dicts) becomes the model right away, so memory holds the same value a later read returns, and
list items are validated one by one. `pydantic_model_creator()` puts that type into the schema.
Without `field_type` (including the static-only `JSONField[T]` generic) values stay plain JSON.

A value JSON can't hold raises `ValidationError` on every write path (`create()`, `save()`,
`update()`, `bulk_create()` including `use_copy=True`, `bulk_update()`): a `NaN`/`Infinity` float
anywhere inside it, or a null byte in a string or key. An int of any size is stored and read back
exactly — one outside the 64-bit range orjson handles is encoded and decoded by the standard
library instead. A `datetime`, `date`, naive `time` or `UUID` inside the value is written as the
same ISO/UUID text on both paths, and with orjson not installed at all.

On both SQLite and PostgreSQL, `exact`/`not` on a `JSONField` compare JSON values — key order and
`1` vs `1.0` don't matter.

On SQLite a `JSONField` column is declared `JSON_TEXT` (TEXT affinity), so a top-level number is
stored as its JSON text: `12345678901234567890` and `1.0` read back unchanged.

### <a id="generatedfield"></a>`GeneratedField`

```python
class GeneratedField(
    expression: RawSQLTerm | dict[str, RawSQLTerm], output_field: Field, stored: bool = True, **kwargs
)
```

A column the database computes from a raw SQL expression over the row — `GENERATED ALWAYS AS (...)
STORED` or `VIRTUAL` — so its value is right after every write the database sees, made outside hare
too:

```python
class OrderLine(Model):
    price = fields.DecimalField(max_digits=10, decimal_places=2)
    quantity = fields.IntField()
    total = fields.GeneratedField(
        RawSQLTerm("price * quantity"), output_field=fields.DecimalField(max_digits=12, decimal_places=2)
    )
```

- `expression` — `RawSQLTerm` of the SQL written into the DDL as it is (**never build it from
  untrusted input**), or one per dialect: `{"sqlite": RawSQLTerm(...), "postgresql": RawSQLTerm(...)}`;
  a dialect the mapping doesn't name raises `ConfigurationError`, and so does plain text.
- `output_field` — a field giving the column's type and how its value is read, compared, filtered
  and described in a schema.
- `stored=True` computes the value on write; `stored=False` — a `VIRTUAL` column, computed on read —
  works on SQLite and PostgreSQL 18, and an older PostgreSQL server raises `UnSupportedError`
  (`Features.supports_virtual_generated_columns`).

hare never writes the column: an `INSERT` reads the computed value back, an `UPDATE` reads it again
(on a database with `RETURNING`), and `save(update_fields=[...])` naming it raises `QueryError`. It
can't be the primary key and takes no `validators=` (`ConfigurationError` — put them on
`output_field`); it is `sensitive` when its `output_field` is, unless it passes its own.

### <a id="format-fields"></a>Email, URL, slug and phone fields

```python
class Author(Model):
    email = fields.EmailField(unique=True)
    homepage = fields.URLField(null=True)
    slug = fields.SlugField(unique=True)
    phone = fields.PhoneField(null=True, region="US")
```

```python
class EmailField(max_length: int = 254, *, lowercase: bool = False, **kwargs)
class URLField(max_length: int = 2048, *, schemes: Sequence[str] = ("http", "https"), **kwargs)
class SlugField(max_length: int = 50, *, allow_unicode: bool = False, **kwargs)
class PhoneField(*, region: str | None = None, **kwargs)
```

Each is a `CharField` (`VARCHAR(max_length)`, a `MaxLengthValidator`) that checks the format of
its value, and converts it to one written form, on every write path — `create()`, `save()`,
`update()`, `bulk_create()` (with `use_copy=True` too), `bulk_update()`, `update_or_create()` — and
for every filter value compared with it as a whole (`email=`, `email__in=`, `email__not=`). A
value that fails raises `ValidationError` naming the field: `email: Invalid email address`. Text
lookups (`__contains`, `__startswith`, `__icontains`, ...) take any text. The validators passed in
`validators=` run too, before the format check. An instance keeps the value as it was assigned
until it is read from the database again; the database holds the written form.

| Field | Accepts | Written as |
|---|---|---|
| `EmailField` | what `EmailValidator` accepts: a local part of atoms joined by dots (`ann.lee+news`) or a quoted string (`"ann@home"`), `@`, then a host name with a dot (`example.com`) or an address literal (`[192.0.2.1]`); 320 characters at most | the domain lowercase — `Ann@Example.COM` becomes `Ann@example.com`; with `lowercase=True` the whole address, `ann@example.com` |
| `URLField` | what `URLValidator` accepts, with one of `schemes` (compared lowercase, so `HTTPS://` is `https`): a host name with a dot, `localhost`, an IPv4 or IPv6 address, an optional port, user info, path, query and fragment | as it is given |
| `SlugField` | ASCII letters, digits, hyphens and underscores (`my-first_post`); with `allow_unicode=True` any Unicode letter or digit too (`привет-мир`) | as it is given |
| `PhoneField` | without the `phonenumbers` package — a number in E.164 form only: `+`, a country code not starting with 0, 15 digits at most in all; with it (`pip install hare-orm[phone]`) — a number in any form it reads and finds valid: `+1 (650) 253-0000`, and with `region` a national number, `(650) 253-0000` | E.164: `+16502530000` (`VARCHAR(16)`) |

So a filter value is found whatever form it is given in: `Author.objects.filter(email="Ann@EXAMPLE.com")`
finds `Ann@example.com`, `filter(phone="(650) 253-0000")` finds `+16502530000` on a `region="US"`
field. The local part of an address keeps its case — a mail server may tell `Ann` from `ann`; use
`lowercase=True` when your addresses are case-insensitive, or `email__iexact=` to compare
regardless of case.

- `schemes` — lowercase scheme names, at least one; anything else raises `ConfigurationError`.
- `SlugField` is indexed unless `db_index=False` is passed. A migration file writes `db_index=True`
  for it, and a field a migration file declares without `db_index` has no index — the same rule as
  for a `ForeignKeyField`.
- `region` — a region code `phonenumbers` knows (`"US"`, `"GB"`, its `SUPPORTED_REGIONS`);
  another value, or `region` without `phonenumbers` installed, raises `ConfigurationError`. Which
  numbers `phonenumbers` finds valid follows its own metadata, so it can change with its version.

A Pydantic schema (`pydantic_model_creator()`) and the OpenAPI built from it describe them with
`format: email`, `format: uri`, a `pattern` of the slug's characters, and for a `PhoneField`
without `phonenumbers` a `pattern` of an E.164 number (with it a number is accepted in any form).

Turning a `CharField` into one of them is an `AlterField` that keeps the column (a `SlugField` adds
its index): the rows already stored are neither checked nor converted — they are on their next write.

### <a id="enum-fields"></a>Enum fields

Not classes — factory functions returning an `IntEnumFieldInstance` (a `SmallIntField`) or a
`CharEnumFieldInstance` (a `CharField`), so `type(Model.status) is not IntEnumField`:

```python
def IntEnumField(enum_type: type[IntEnum], description: str | None = None, **kwargs) -> IntEnumType
def CharEnumField(enum_type: type[Enum], description: str | None = None, max_length: int = 0, **kwargs) -> CharEnumType
```

- `IntEnumField` is built on `SmallIntField` — enum values must fit `int16` (1..32767 if
  `generated=True`, else the full `int16` range).
- `CharEnumField` is built on `CharField` — `max_length=0` autodetects the longest enum value's
  string length. **If you add a longer enum value later, bump `max_length` yourself** — it isn't
  recomputed automatically. An enum with non-string values (e.g. `ONE = 1`) works too: the column
  stores `str(member.value)` (`"1"`) and a read maps that text back to the member.
- Both auto-generate a `"NAME: value"`-style `description` listing every member, unless you pass
  your own.

```python
from enum import Enum

class Status(Enum):
    DRAFT = "draft"
    PUBLISHED = "published"

class Post(Model):
    status = fields.CharEnumField(Status, default=Status.DRAFT)
```

### <a id="compositeprimarykey"></a>`CompositePrimaryKey`

```python
class CompositePrimaryKey:
    def __init__(self, *field_names: str, without_overlaps: bool = False) -> None
```

Not a real field — no DB column of its own. It makes two or more already-declared fields the
table's primary key:

```python
class ArticleVersion(Model):
    id = fields.UUIDField()
    version = fields.IntField()
    pk = CompositePrimaryKey("id", "version")
```

Each named field must exist and must **not** itself be `primary_key=True` or `generated=True`.
`Model.pk` becomes a tuple; `clone()` on such a model resolves each component's own `default=`/
`db_default=` independently — only components with neither need an explicit value in `pk=(...)`.

`without_overlaps=True` makes the last field a range (or multirange) two rows may not overlap in
while the other fields are equal — one price per product for any moment, one booking per room at a
time — enforced by the database (`PRIMARY KEY (product, valid WITHOUT OVERLAPS)`, PostgreSQL 18):

```python
class PricePeriod(Model):
    product = fields.CharField(max_length=20)
    valid = DateRangeField()  # hare.dialects.postgresql.fields.ranges
    price = fields.IntField()
    pk = CompositePrimaryKey("product", "valid", without_overlaps=True)
```

An older PostgreSQL server and SQLite raise `UnSupportedError` before any DDL
(`Features.supports_without_overlaps`); a scalar field before the range needs the `btree_gist`
extension, which the schema editor and the migration autodetector add. A foreign key can't target
such a key (`ConfigurationError`), and changing `without_overlaps` of an existing table isn't
autodetected — it needs a hand-written migration.

> [!NOTE]
> A foreign key **can** target a model with a composite primary key — see
> [Relations](relations.md#targeting-a-composite-primary-key) — with real table-level
> `FOREIGN KEY` DDL, cascades, joins, and `prefetch_related()` support.

## <a id="postgresql-only-fields"></a>PostgreSQL-only fields

`HStoreField`, `TSVectorField`, `PostGISField`, `CitextField` and the range field family live in
`hare.dialects.postgresql` — see [PostgreSQL fields](../dialects/postgresql/fields.md).
Of `hare.fields`, `ArrayField` runs on PostgreSQL and ClickHouse, `MapField`, `TupleField` and
`NestedField` on ClickHouse, and `IPAddressField` everywhere (`inet` on PostgreSQL, `IPv6` on
ClickHouse, its text elsewhere) — see [ClickHouse types](../dialects/clickhouse/types.md).
`VectorField` lives in `hare.vectors` and works on PostgreSQL and SQLite — see
[Vector search](../dialects/search-and-geodata/vector-search.md).

## <a id="writing-your-own-field"></a>Writing your own field

See [Custom fields](../extending/custom-fields.md).
