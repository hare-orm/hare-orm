# Custom fields

A field of your own subclasses `hare.fields.Field`. What you typically override:

- `field_type` — the Python type of the field's values, a class attribute: `field_type = int`.
- `SQL_TYPE` — a string, or a `@property`.
- `to_db_value(self, value, instance)` / `from_db_value(self, value)` — custom (de)serialization;
  `to_lookup_value(self, value, instance)` converts a filter's value (`to_db_value()` by default).
- `to_python(self, value)` — normalizes a value freshly assigned via
  `Model(**kwargs)`; only needed when a fresh value should get the same treatment
  `from_db_value` gives a DB-read one (see below) — leave it alone for an asymmetric
  `to_db_value`/`from_db_value` pair (encryption, masking, ...).
- `deconstruct()` — returns `(path, args, kwargs)`: what a migration file writes and the
  autodetector compares. Extend it if your field takes constructor args beyond the base ones.
- `migration_import_path` — a stable dotted path, if the field's module might move.

```python
from hare.fields import Field


class MoneyField(Field[int]):
    """Stores an integer amount of cents."""

    field_type = int
    SQL_TYPE = "BIGINT"

    def to_db_value(self, value, instance):
        self.validate(value)
        return None if value is None else int(value)

    def from_db_value(self, value):
        return None if value is None else int(value)

```

## <a id="to-python-value-on-assign"></a>Freshly assigned values: `to_python`

`Model(**kwargs)`/`Model.objects.create(**kwargs)` construction runs each keyword value through
`to_python(self, value)`, not `from_db_value()` — a value read from the DB and a
value the caller just typed in are different things, even though they often need the same
handling. The default implementation returns the value unchanged, which is safe for any field
whose `to_db_value`/`from_db_value` pair isn't a faithful, idempotent encode/decode of an
already-Python value.

Override `to_python()` (typically to just call `self.from_db_value(value)`, the
same conversion a DB-read value gets) when a freshly assigned value genuinely needs that same
normalization — e.g. parsing a bare ISO string into a real `datetime`.

**Don't** override it (keep the default) when `to_db_value`/`from_db_value` are asymmetric — an
encoder/decoder pair that isn't its own inverse when applied to an already-decoded value, as with
encryption or masking. Running `from_db_value` (the decode half) on a value that never went
through `to_db_value` would silently corrupt it in memory, and a subsequent `save()` would then
encode the *already-corrupted* value — for an encryption field, that means writing plaintext to
the DB instead of ciphertext.

```python
class EncryptedField(Field[str]):
    field_type = str
    SQL_TYPE = "TEXT"

    def to_db_value(self, value, instance):
        self.validate(value)
        return None if value is None else encrypt(value)

    def from_db_value(self, value):
        return None if value is None else decrypt(value)

    # No to_python() override - the default (return the value as-is) is correct:
    # Model(secret="hello") must keep "hello" in memory, not decrypt() a value that was never
    # encrypted in the first place.
```

## <a id="per-dialect-storage"></a>Per-dialect storage

Each dialect has a type registry mapping a field class to how it stores it: a column type, the DDL
of a database-generated primary key and a cast wrapped around the column wherever it is compared or
ordered. A field class uses the mapping of its nearest registered base class, so `MoneyField` above
takes `BIGINT` everywhere unless a dialect registers something else for it:

```python
from hare.dialects.base.types import TypeMapping
from hare.dialects.dialect_registry import DialectRegistry

DialectRegistry.get_dialect("sqlite").types.register(MoneyField, TypeMapping(column_type="INTEGER"))
```

A field that only exists on some dialects lists them in `SUPPORTED_DIALECTS`; generating DDL for
another dialect raises `UnSupportedError`:

```python
class LtreeField(Field[str]):
    SQL_TYPE = "ltree"
    SUPPORTED_DIALECTS = frozenset({"postgresql"})
```

A field several dialects store, each in a type of its own, names none of them: it sets
`COLUMN_TYPE_FROM_DIALECT = True` and exists wherever a dialect's types give it a column type
(`TypeMapping.column_type`) — `GeometryField` and `VectorField` are such fields. A dialect built on
another one's types takes such a field away with an empty mapping:
`types.register(GeometryField, TypeMapping())`.

A `TypeMapping` leaves to the field whatever it doesn't set:

| Attribute | What it sets |
|---|---|
| `column_type` | The column type, or a `(field) -> str` building it. |
| `generated_sql` | The DDL of a database-generated primary key column. |
| `function_cast` | A `(field, term) -> term` wrapping the column wherever it is compared, ordered or copied. |
| `to_db` | A `(field, value, instance) -> value` in place of the field's `to_db_value()`. |
| `to_lookup` | A `(field, value, instance) -> value` in place of the field's `to_lookup_value()`. |
| `to_python` | A `(field, value) -> value` in place of the field's `from_db_value()`. |
| `json_term` | A `(field, term) -> term` giving the text the value is written into a JSON object as — for a value stored in a form JSON can't hold (a UUID in 16 bytes). |
| `naive_datetime_is_utc` | The field's own `from_db_value()`, with a naive datetime from the driver read as a UTC instant. |
| `extension` | The database extension the column type needs — created wherever a field of the class is used (`postgis` for a `GeometryField`, `vector` for a `VectorField` on PostgreSQL). |
| `inserted_by_select` | An INSERT writing the column writes its rows as a `SELECT` of them, not as `VALUES` — for a value written as an expression the dialect's `VALUES` doesn't read; or a `(field) -> bool` deciding it per field. |

`field.get_column_type(dialect)`, `field.get_generated_sql(dialect)` and
`field.get_function_cast(dialect)` read the result; a field computing them from its own
configuration overrides these methods.

## <a id="lookups-value-paths-and-generated-columns"></a>Lookups, value paths and generated columns

A field whose value isn't a plain scalar tells hare how it is filtered by overriding these methods
of `Field` — hare's own array, range and hstore fields are built this way, so a third-party field
gets the same treatment without any change to hare:

| Method / attribute | What it gives |
|---|---|
| `get_lookups()` | The field's lookups by suffix (`""` is plain equality), each a `FieldLookup` like a `register_lookup()` one — the generic set by default; a field with a set of its own returns it instead, and a filter with a suffix outside it raises `FieldError`. `register_lookup()` lookups are added to it. |
| `get_path_transform(segment)` | How a path segment after the field's name reads inside its value — `(function building the term from the field's term, field of the value read)`, or `None`. Filters (`tags__0__startswith`), `F()`, `values()` and `order_by()` follow it. |
| `get_lookup_value_description(lookup)` | `(LookupValueShape, type)` of a lookup's value where it isn't a value of the field's type (an array's `contains` takes a list of elements), for [`get_lookup_info()`](../querying/describing-filters.md); `None` otherwise. |
| `get_like_text_function()` | How the value becomes text for `contains`/`icontains`/`startswith`/... and the pattern lookups; `None` casts it to `VARCHAR`. |
| `get_generated_from_field_names()` / `with_renamed_generated_from_field(old, new)` | The model's other fields a generated column is computed from, by name — a migration refuses to remove such a field while this one reads it, and a rename renames it here. |
| `holds_container_value` | `True` for an array, range or JSON-like value — an annotation of such a field takes its own lookups. |

```python
import operator
from functools import partial

from hare.fields import CharField
from hare.query.filters import FieldLookup
from hare.sql.terms import Function

class CodeField(CharField):
    def get_lookups(self):
        def starts_with(term, value):
            return term.like(f"{value}%")

        return {"": FieldLookup(operator.eq), "prefix": FieldLookup(starts_with)}

    def get_path_transform(self, segment):
        if segment == "upper":
            return partial(Function, "UPPER"), self
        return None
```

A dialect adds a path segment to field classes it doesn't define itself —
PostgreSQL's `name__unaccent` on text fields — with
`FieldClass.register_transform(segment, get_transform, required_extension=...)` — `get_transform(field)`
returns the same pair as `get_path_transform()` — from its `Dialect.install()`, which runs once when
the dialect is registered.
