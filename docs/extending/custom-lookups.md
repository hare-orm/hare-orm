# Custom lookups

`Field.register_lookup()` adds a filter suffix of a project's own — `field__within_km=...` — to a
field class and its subclasses, for every dialect or for some.

```python
@classmethod
def register_lookup(
    cls,
    lookup_name: str,
    lookup: Callable[[Field | None], FieldLookup] | None = None,
    *,
    value_shape: LookupValueShape = LookupValueShape.VALUE,
    value_type: Any = None,
    dialects: Iterable[str] | None = None,
    required_extension: str | None = None,
) -> Callable[[Callable], Callable] | None
```

- Can be called at any time. Called after `Hare.init()`, it drops the caches built from the
  registries, so the next query knows the lookup.
- `lookup_name` — the suffix without the leading `__`, e.g. `"within_km"` registers
  `field__within_km=...`.
- `lookup` — `(field) -> FieldLookup` (`hare.query.filters.FieldLookup`). Its `operator` builds the
  criterion as `(term, encoded_value)`; the optional `value_encoder` encodes the filter value as
  `(value, model, field, dialect)` — `dialect` is the dialect of the connection the query runs on —
  and without it the field converts the value as it converts its own values; every attribute is in the
  table below. The same lookup crosses relations by itself: `author__name__<lookup>`
  joins `author` and applies it to `name`.
- `binds_by_rebuild` on the `FieldLookup` — for an operator that derives what it compares from the
  value instead of comparing the encoded value as it is. A query plan then binds a later value by
  building the criterion again from it. The SQL text the operator builds must depend on nothing of
  the value but its type (and a list's length, a dict's keys): those are part of the plan key, the
  rest is bound. Without it, such an operator keeps no plan
  ([query plan cache](../querying/query-plan-cache.md#correctness-notes)).
- `value_shape` — whether the filter value is one value (`LookupValueShape.VALUE`), a list (`LIST`)
  or a two-item range (`RANGE`). A `LIST`/`RANGE` lookup giving no `value_encoder` of its own has
  each item encoded by the field, as the field's own `__in`/`__range` do.
- `value_type` — the type of the value (of each item of a list or range); `None` means the field's
  own type.
- `dialects` — the names of the dialects the lookup runs on; `None` means every dialect. A query on
  another dialect fails with `UnSupportedError` before its SQL is built.
- `required_extension` — the database extension the lookup needs; `None` means none.
- `value_shape`, `value_type`, `dialects` and `required_extension` are what
  [`get_lookup_info()`](../querying/describing-filters.md) reports for the lookup.
- Registered on `Field` itself, the lookup belongs to every value: every field, and a value with
  no field — an annotation (`annotate(total=...).filter(total__<lookup>=...)`), a JSON path — for
  which `lookup` is called with `None` as the field. PostgreSQL's trigram lookups are registered
  this way.
- Usable as a plain call, or as a decorator (omit `lookup` to decorate).

`FieldLookup` (`hare.query.filters.FieldLookup`), a dataclass:

| Attribute | What it is |
|---|---|
| `operator` | Builds the criterion — `(term, encoded_value) -> Criterion`. |
| `value_encoder` | Encodes the filter value — `(value, model, field, dialect)`; `None` converts it as the field converts its own values. |
| `array_element_field` | The field each item of a list value is bound as. |
| `array_element_fields` | Per key column, the field each item of a list of key rows is bound as. |
| `text_function` | Turns the value into the text a text lookup matches. |
| `compares_json_path_text` | Whether the lookup matches the text of the value at a JSON path. |
| `is_tsvector`, `search_config` | Whether a `__search` matches a stored text search vector as it is, and that vector's configuration. |
| `searched_field` | The field a `__search` matches — a dialect searching through a full-text index finds the field's index by it. |
| `required_feature` | The `Features` flag a connection needs to run the lookup; on a connection without it the query raises `UnSupportedError` before any SQL. |
| `binds_by_rebuild` | See the list above. |

`lookup.with_changes(**changes)` is a copy with some attributes replaced.

Two examples — the first is how `PostGISField` registers its `within_km` lookup:

```python
@staticmethod
def _within_km_lookup(field: Field | None) -> FieldLookup:
    def _encode_within_km_value(value, model, field_object, dialect):
        latitude, longitude, radius_km = value
        field_object.to_db_value((latitude, longitude), model)  # runs the field's validators
        return value

    def _operator(term, value):
        latitude, longitude, radius_km = value
        point = PostGISField.get_geography_term((latitude, longitude))
        return Function("ST_DWITHIN", term, point, radius_km * 1000)

    return FieldLookup(_operator, _encode_within_km_value)

PostGISField.register_lookup("within_km", PostGISField._within_km_lookup, value_type=tuple)
# .filter(location__within_km=(lat, lon, radius_km))
```

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

Hardcoded lookup suffixes (`exact`, `in`, `gte`, `icontains`, ...) always win — a custom lookup
can never accidentally shadow one of them.
