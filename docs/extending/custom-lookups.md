# Custom lookups: `register_lookup()`

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
  `(value, model, field, dialect)` - `dialect` is the dialect of the connection the query runs on -
  and without it the field converts the value as it converts its own values. The other attributes
  are `array_element_field`, `array_element_fields`, `text_function`, `compares_json_path_text`,
  `is_tsvector` and `search_config`. The same lookup crosses relations by itself: `author__name__<lookup>`
  joins `author` and applies it to `name`.
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
  no field - an annotation (`annotate(total=...).filter(total__<lookup>=...)`), a JSON path - for
  which `lookup` is called with `None` as the field. PostgreSQL's trigram lookups are registered
  this way.
- Usable as a plain call, or as a decorator (omit `lookup` to decorate).

Two examples - the first is how `PostGISField` registers its `within_km` lookup:

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
