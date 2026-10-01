# Custom fields

Subclass `hare.fields.base.Field` (or `hare.fields.Field`). What you typically override:

- `field_type` — the field's base Python type (usually set via a second base class, Django-style:
  `class MoneyField(Field[int]):`).
- `SQL_TYPE` — a string, or a `@property`.
- `to_db_value(self, value, instance)` / `from_db_value(self, value)` — custom (de)serialization.
- `to_python(self, value)` — normalizes a value freshly assigned via
  `Model(**kwargs)`; only needed when a fresh value should get the same treatment
  `from_db_value` gives a DB-read one (see below) - leave it alone for an asymmetric
  `to_db_value`/`from_db_value` pair (encryption, masking, ...).
- `deconstruct()` — returns `(path, args, kwargs)`: what a migration file writes and the
  autodetector compares. Extend it if your field takes constructor args beyond the base ones.
- `migration_import_path` — a stable dotted path, if the field's module might move.

```python
from hare.fields.base import Field


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

## Freshly assigned values: `to_python` {: #to-python-value-on-assign }

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

## Per-dialect storage {: #per-dialect-storage }

Each dialect has a type registry mapping a field class to how it stores it: a column type, the DDL
of a database-generated primary key and a cast wrapped around the column wherever it is compared or
ordered. A field class uses the mapping of its nearest registered base class, so `MoneyField` above
takes `BIGINT` everywhere unless a dialect registers something else for it:

```python
from hare.dialects.base.types import TypeMapping
from hare.dialects.registry import DialectRegistry

DialectRegistry.get_dialect("sqlite").types.register(MoneyField, TypeMapping(column_type="INTEGER"))
```

A field that only exists on some dialects lists them in `SUPPORTED_DIALECTS`; generating DDL for
another dialect raises `UnSupportedError`:

```python
class LtreeField(Field[str]):
    SQL_TYPE = "ltree"
    SUPPORTED_DIALECTS = frozenset({"postgresql"})
```

`field.get_column_type(dialect)`, `field.get_generated_sql(dialect)` and
`field.get_function_cast(dialect)` read the result; a field computing them from its own
configuration overrides these methods.

## Lookups, value paths and generated columns {: #lookups-value-paths-and-generated-columns }

A field whose value isn't a plain scalar tells hare how it is filtered by overriding these methods
of `Field` - hare's own array, range and hstore fields are built this way, so a third-party field
gets the same treatment without any change to hare:

| Method / attribute | What it gives |
|---|---|
| `get_lookups()` | The field's lookups by suffix (`""` is plain equality), each a `FieldLookup` like a `register_lookup()` one - the generic set by default; a field with a set of its own returns it instead, and a filter with a suffix outside it raises `FieldError`. `register_lookup()` lookups are added to it. |
| `get_path_transform(segment)` | How a path segment after the field's name reads inside its value - `(function building the term from the field's term, field of the value read)`, or `None`. Filters (`tags__0__startswith`), `F()`, `values()` and `order_by()` follow it. |
| `get_lookup_value_description(lookup)` | `(LookupValueShape, type)` of a lookup's value where it isn't a value of the field's type (an array's `contains` takes a list of elements), for [`get_lookup_info()`](../querying/describing-filters.md); `None` otherwise. |
| `get_like_text_function()` | How the value becomes text for `contains`/`icontains`/`startswith`/... and the pattern lookups; `None` casts it to `VARCHAR`. |
| `get_generated_from_field_names()` / `with_renamed_generated_from_field(old, new)` | The model's other fields a generated column is computed from, by name - a migration refuses to remove such a field while this one reads it, and a rename renames it here. |
| `holds_container_value` | `True` for an array, range or JSON-like value - an annotation of such a field takes its own lookups. |

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

A dialect adds a path segment to field classes it doesn't define itself -
PostgreSQL's `name__unaccent` on text fields - with
`FieldClass.register_transform(segment, get_transform, required_extension=...)` - `get_transform(field)`
returns the same pair as `get_path_transform()` - from its `Dialect.install()`, which runs once when
the dialect is registered.
