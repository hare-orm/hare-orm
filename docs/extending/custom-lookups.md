# Custom lookups

`Field.register_lookup()` adds a filter suffix of a project's own — `field__reversed=...` — to a field
class and its subclasses, for every dialect or for one database.

```python
class Field:
    @classmethod
    def register_lookup(
        cls,
        lookup_name: str,
        lookup: LookupFunction | None = None,
        *,
        value_shape: LookupValueShape = LookupValueShape.VALUE,
        value_type: Any = None,
        required_extension: str | None = None,
    ) -> Callable[[LookupFunction], LookupFunction] | None: ...
```

## <a id="arguments"></a>Arguments

- Can be called at any time. Called after `Hare.init()`, it drops the caches built from the registries,
  so the next query knows the lookup.
- `lookup_name` — the suffix without the leading `__`, e.g. `"reversed"` registers
  `field__reversed=...`.
- `lookup` — `(field) -> FieldLookup` (`hare.query.filters.FieldLookup`). Its `operator` builds the
  criterion as `(term, encoded_value)`; the optional `value_encoder` encodes the filter value as
  `(value, model, field, dialect)` — `dialect` is the dialect of the connection the query runs on — and
  without it the field converts the value as it converts its own values; every attribute is in the table
  below. The same lookup crosses relations by itself: `author__name__<lookup>` joins `author` and applies
  it to `name`.
- To bind fresh values on cache hits, convert the input in `value_encoder` and have the
  operator use that converted value directly. An operator deriving further values uses
  normal preparation; binding never rebuilds the criterion.
- `value_shape` — whether the filter value is one value (`LookupValueShape.VALUE`), a list (`LIST`) or a
  two-item range (`RANGE`). A `LIST`/`RANGE` lookup giving no `value_encoder` of its own has each item
  encoded by the field, as the field's own `__in`/`__range` do.
- `value_type` — the type of the value (of each item of a list or range); `None` means the field's own
  type.
- `required_extension` — the database extension the lookup needs; `None` means none.
- `value_shape`, `value_type` and `required_extension` are what
  [`get_lookup_info()`](../querying/describing-filters.md) reports for the lookup.
- Registered on `Field` itself, the lookup is available to fields and expressions with no known output
  field. Typed annotations pass their output field, including a typed JSON path; `None` is passed only
  when no output field is known. Supported path suffixes still depend on the path's lookup declaration.
  PostgreSQL's trigram lookups are registered on `Field`.
- Usable as a plain call, or as a decorator (omit `lookup` to decorate).

## <a id="one-database"></a>A lookup of one database

A lookup only one database runs has an operator marked by `DialectImplementedOperators.mark`
(`hare.query.filters.lookups.dialect_implemented_operators`), which that database's dialect implements —
`dialect.filter_operators.register_operator(operator, implementation)`, `implementation` the operator
itself when it builds that database's SQL already. A query on another dialect fails with
`UnSupportedError` before its SQL is built, and `get_lookup_info()` reports the lookup unsupported there:

```python
from hare import fields
from hare.dialects.dialect_registry import DialectRegistry
from hare.query.enums import LookupValueShape
from hare.query.filters import FieldLookup
from hare.query.filters.lookups.dialect_implemented_operators import DialectImplementedOperators
from hare.query.filters.lookups.lookups import Lookups


@DialectImplementedOperators.mark
def between_on_postgresql(term, value):
    return Lookups.between(term, value)


fields.IntField.register_lookup(
    "within_on_postgresql",
    lambda field: FieldLookup(between_on_postgresql),
    value_shape=LookupValueShape.RANGE,
)
DialectRegistry.get_dialect("postgresql").filter_operators.register_operator(
    between_on_postgresql, between_on_postgresql
)
```

The operator is matched by identity: a function of a module, not one made anew by each `lookup` call.
PostgreSQL's trigram lookups and ClickHouse's `__global_in` are lookups of one database.

## <a id="fieldlookup"></a>`FieldLookup`

`FieldLookup` (`hare.query.filters.FieldLookup`), a dataclass:

| Attribute | What it is |
|---|---|
| `operator` | Builds the criterion — `(term, encoded_value) -> Criterion`. |
| `value_encoder` | Encodes the filter value — `(value, model, field, dialect)`; `None` converts it as the field converts its own values. |
| `value_encoder_builder` | Optional `(field, dialect) -> encoder`. Prepares fixed field conversions before the value is encoded. The resulting encoder still receives `(value, model, field, dialect)` and performs every per-value check. Plain `value_encoder` callbacks remain supported. |
| `array_element_field` | The field each item of a list value is bound as. |
| `array_element_fields` | Per key column, the field each item of a list of key rows is bound as. |
| `text_function` | Turns the value into the text a text lookup matches. |
| `compares_json_path_text` | Whether the lookup matches the text of the value at a JSON path. |
| `is_tsvector`, `search_config` | Whether a `__search` matches a stored text search vector as it is, and that vector's configuration. |
| `requires_search_query_config` | Requires an explicit query configuration when a computed vector has no common configuration. |
| `searched_field` | The field a `__search` matches — a dialect searching through a full-text index finds the field's index by it. |
| `required_feature` | The `Features` flag a connection needs to run the lookup; on a connection without it the query raises `UnSupportedError` before any SQL. |

`lookup.with_changes(**changes)` is a copy with some attributes replaced.

`lookup.get_value_encoder(field, dialect)` selects the encoder for ordinary fields, annotations
and relation lookups. A configured builder takes precedence over `value_encoder`. The same
selected encoder converts the value and is retained when the filter records a parameter
reference; recording does not call the builder again. SQL terms bypass value encoding.

The built-in LIKE operators declare the pattern parameter when constructing their SQL.
`LikePatternParameter` carries the source wrapper and the prefix/suffix wildcard flags;
case conversion may wrap that source without changing its binding. A manually constructed
`Like` without this declaration does not receive the built-in pattern rebinder.
Typed container comparisons retain the container's parameter source and field conversion.
On a plan hit, only the new Python value is converted; the container's SQL cast is not rebuilt.

## <a id="examples"></a>Examples

```python
from hare.fields import CharField
from hare.query.filters import FieldLookup

def _reversed_lookup(field: CharField | None) -> FieldLookup:
    def _operator(term, value: str):
        return term == value[::-1]
    return FieldLookup(_operator)

CharField.register_lookup("reversed", _reversed_lookup)
# .filter(name__reversed="oom")
```

Hardcoded lookup suffixes (`in`, `gte`, `icontains`, ...) always win — a custom lookup can never
accidentally shadow one of them.
