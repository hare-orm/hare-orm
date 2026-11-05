# Custom fields

Field conversion and validation run in Python for individual writes, bulk writes,
filters and result materialization. Native drivers decode database wire formats;
they do not provide a second implementation of ORM field rules.

Container subclasses implement `encode_value(value, instance, types, path)`,
`decode_value(value, types)` and `normalize_value(value, path)`. These hooks apply
to root columns and nested values. The explicit `types` argument supplies child
conversions, and `path` identifies the nested value in an error. Dialect container
conversion does not select behavior by comparing method identities.

`JSONField.prepare_value()` validates a declared type and checks the document before
encoding. The default encoder receives its JSON representation; a custom encoder
receives the Python representation, with plain inputs preserved. Non-finite numbers,
circular references and excessive nesting are rejected before encoding. The default
codec preserves integers beyond 64 bits. Optional JSON acceleration runs only after
this preparation and does not change field policy or the raw driver API.

A field of your own subclasses `hare.fields.Field`.

## <a id="overrides"></a>What a field overrides

What you typically override:

- `field_type` — the Python type of the field's values, a class attribute: `field_type = int`.
- `SQL_TYPE` — a string, or a `@property`.
- `to_db_value(self, value, instance)` / `from_db_value(self, value)` — custom (de)serialization;
  `to_lookup_value(self, value, instance)` converts a filter's value (`to_db_value()` by default).
  `to_db_value()` converts the value and leaves `instance` as it is: what a write sets on the object
  itself (an `auto_now` stamp, a blind index) is set before the values are converted.
- `to_python(self, value)` — normalizes a value freshly assigned via `Model(**kwargs)`; only needed when
  a fresh value should get the same treatment `from_db_value` gives a value read from the database (see
  below) — leave it alone for an asymmetric `to_db_value`/`from_db_value` pair (encryption, masking,
  ...).
- `get_json_value(self, value)` — the value's JSON form, what an outbox event of a captured change holds
  of it. The default writes JSON's own values and the table of
  [OutboxJsonEncoding](../integrations/outbox.md#encoding), elements of lists and mappings too; a field
  whose values have no form there (an object of its own) returns one its consumers read — it raises
  `TypeError` otherwise, and the write fails instead of the event.
- `get_computed_field(self, computation)` — the field a result computed from the field's values is read
  as; `computation` is a `ValueComputation` (`hare.fields.enums`: `SUM`, `AVERAGE`, `STATISTIC`,
  `ARITHMETIC`). The field itself by default; a field the database computes with in a wider type returns
  a field of that type — ClickHouse's `Float32Field` gives a `FloatField` for a sum, an average and
  arithmetic.
- `get_column_type_options(self)` — what besides its class sets the field's column type, as a tuple: a
  cast to the field (`Cast(value, field)`) keeps its plan per these options. `()` by default;
  ClickHouse's `ClickhouseDatetimeField` gives its precision and zone.
- `deconstruct()` — returns `(path, args, kwargs)`: what a migration file writes and the autodetector
  compares. Extend it if your field takes constructor args beyond the base ones.
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
`to_python(self, value)`, not `from_db_value()` — a value read from the database and a value the caller
just typed in are different things, even though they often need the same handling. The default
implementation returns the value unchanged, which is safe for any field whose
`to_db_value`/`from_db_value` pair isn't a faithful, idempotent encode/decode of an already-Python value.

Override `to_python()` (typically to just call `self.from_db_value(value)`, the same conversion a value
read from the database gets) when a freshly assigned value genuinely needs that same normalization — e.g.
parsing a bare ISO string into a real `datetime`.

**Don't** override it (keep the default) when `to_db_value`/`from_db_value` are asymmetric — an
encoder/decoder pair that isn't its own inverse when applied to an already-decoded value, as with
encryption or masking. Running `from_db_value` (the decode half) on a value that never went through
`to_db_value` can reject or corrupt it before it is saved; a later `save()` cannot recover the original
value from an invalid decoding result.

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

Each dialect has a type registry mapping a field class to how it stores it: a column type, the DDL of a
database-generated primary key and a cast wrapped around the column wherever it is compared or ordered. A
field class uses the mapping of its nearest registered base class, so `MoneyField` above takes `BIGINT`
everywhere unless a dialect registers something else for it:

```python
from hare.dialects.base.types import TypeMapping
from hare.dialects.dialect_registry import DialectRegistry

DialectRegistry.get_dialect("sqlite").types.register(MoneyField, TypeMapping(column_type="INTEGER"))
```

A field that sets `COLUMN_TYPE_FROM_DIALECT = True` exists wherever a dialect's types give it a column
type (`TypeMapping.column_type`, registered for its class or a class between it and the one setting
that) and nowhere else; a model of it bound to a connection of another dialect raises `UnSupportedError`
(`Hare.init()`), as does a field whose declaration the dialect's type refuses (a ClickHouse
`PointField(null=True)`: a point holds no NULL there). A field of one database is such a field, its type
registered by that database's dialect — it may still name the type itself:

```python
class LtreeField(Field[str]):
    SQL_TYPE = "ltree"
    COLUMN_TYPE_FROM_DIALECT = True


DialectRegistry.get_dialect("postgresql").types.register(
    LtreeField, TypeMapping(column_type=lambda field: field.SQL_TYPE, extension="ltree")
)
```

A field several dialects store, each in a type of its own, names none of them — `GeometryField` and
`VectorField` are such fields. A dialect built on
another one's types takes such a field away with an empty mapping:
`types.register(GeometryField, TypeMapping())`.

A `TypeMapping` leaves to the field whatever it doesn't set:

| Attribute | What it sets |
|---|---|
| `column_type` | The column type, or a `(field) -> str \| None` building it; returning `None` means that declaration has no supported column type. |
| `generated_sql` | The DDL of a database-generated primary key column. |
| `function_cast` | A `(field, term) -> term` wrapping the column wherever it is compared, ordered or copied. |
| `to_db` | A `(field, value, instance) -> value` in place of the field's `to_db_value()`. |
| `to_lookup` | A `(field, value, instance) -> value` in place of the field's `to_lookup_value()`. |
| `to_python` | A `(field, value) -> value` replacing `from_db_value()`. Built-in converters normalize the driver's format and invoke the field's reader; a custom converter must invoke it itself if the field's contract requires it. |
| `json_term` | A `(field, term) -> term` giving the text the value is written into a JSON object as — for a value stored in a form JSON can't hold (a UUID in 16 bytes). |
| `ordering_key` | A `(field, term) -> term \| None` defining the SQL ordering expression; `None` uses the column itself. |
| `holds_null` | Whether the column type supports NULL; `None` inherits the mapping of a base field class. |
| `extension` | The database extension the column type needs — created wherever a field of the class is used (`postgis` for a `GeometryField`, `vector` for a `VectorField` on PostgreSQL). |
| `inserted_by_select` | An INSERT writing the column writes its rows as a `SELECT` of them, not as `VALUES` — for a value written as an expression the dialect's `VALUES` doesn't read; or a `(field) -> bool` deciding it per field. |

`field.get_column_type(dialect)`, `field.get_generated_sql(dialect)` and
`field.get_function_cast(dialect)` read the result; a field computing them from its own configuration
overrides these methods.


## <a id="declaration-snapshots"></a>Declaration snapshots

`field.snapshot()` creates an independent, unbound declaration for model publication and migration state. It
does not
call the field constructor, defaults or validators. Mutable literal defaults and nested fields
are copied; callbacks retain their identity. The snapshot drops model/name/reference binding and
derived conversion caches. Relation snapshots retain physical column overrides and declaration
references, while removing the bound target model and key fields.

`get_declaration_values()` returns the state to retain, including inherited and private slots.
Runtime copies, deep copies and snapshots preserve slot values automatically. A custom field can
override this method to exclude runtime attributes. Override `snapshot(memo=...)`
when external handles need a specific copying policy; pass the memo when snapshotting nested fields.
Historical model rendering binds fresh declarations, so neither rendering nor changing a later
migration state changes an earlier field declaration.

Model bodies, abstract bases and mixins use this protocol before binding. Reusing a field declaration
under different names in different models does not bind or mutate the original object. Generated
relation sides and shadow key columns are rebuilt for the new model rather than inherited as declarations.

Publication retains an independent declaration snapshot. Calling `snapshot()` on a published field
reads that declaration, including an implicit relation column name, rather than reconstructing it
from runtime mutations. Model/name/column/reference identity, normalization state and cache buckets
belong to its `BoundField` record. A runtime field copy receives fresh caches and captures a new
declaration when published; changing that copy does not update the original publication.
For relations, the record also holds a `BoundRelation` with target fields, source/target key names
and through-table identity. A declaration snapshot clears this data; the model graph prepares it
again when binding the relation.

## <a id="lookups-value-paths-and-generated-columns"></a>Lookups, value paths and generated columns

A field whose value isn't a plain scalar tells hare how it is filtered by overriding these methods of
`Field` — hare's own array, range and hstore fields are built this way, so a third-party field gets the
same treatment without any change to hare:

| Method / attribute | What it gives |
|---|---|
| `get_lookups()` | The field's lookups by suffix (`""` is plain equality), each a `FieldLookup` like a `register_lookup()` one — the generic set by default; a field with a set of its own returns it instead, and a filter with a suffix outside it raises `FieldError`. `register_lookup()` lookups are added to it. |
| `get_path_transform(segment)` | How a path segment after the field's name reads inside its value — `(function building the term from the field's term, field of the value read)`, or `None`. Filters (`tags__0__startswith`), `F()`, `values()` and `order_by()` follow it. |
| `get_lookup_value_description(lookup)` | `(LookupValueShape, type)` of a lookup's value where it isn't a value of the field's type (an array's `contains` takes a list of elements), for [`get_lookup_info()`](../querying/describing-filters.md); `None` otherwise. |
| `get_like_text_function()` | How the value becomes text for `contains`/`icontains`/`startswith`/... and the pattern lookups; `None` casts it to `VARCHAR`. |
| `get_generated_from_field_names()` / `with_renamed_generated_from_field(old, new)` | The model's other fields a generated column is computed from, by name — a migration refuses to remove such a field while this one reads it, and a rename renames it here. |
| `holds_container_value` | `True` for an array, range or JSON-like value — an annotation of such a field takes its own lookups. |
| `holds_intervals` | `True` for a column of the database's interval type — a duration added to a moment as the database adds it, not as a count of microseconds. |

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

A dialect adds path segments to other field classes by returning `TransformRegistration` declarations
in `ExtensionContribution.transforms` from `Dialect.get_contribution()`. Each declaration contains the
field class, segment and `RegisteredTransform(get_transform, required_extension)`.
`get_transform(field)` returns the same pair as `get_path_transform()`. Outside factories,
`FieldClass.register_transform(segment, get_transform, required_extension=...)` publishes one declaration.
