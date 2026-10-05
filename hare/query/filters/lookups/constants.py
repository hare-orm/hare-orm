from __future__ import annotations

from hare.query.enums import Lookup, LookupValueShape

#: Lookups chained after a date part or ``__date``/``__time`` (``created__year__gte``), and the
#: type of value each takes: one value, a list, or a two-item range.
DATE_PART_COMPARISON_LOOKUPS: dict[Lookup, LookupValueShape] = {
    Lookup.EXACT: LookupValueShape.VALUE,
    Lookup.NOT: LookupValueShape.VALUE,
    Lookup.GT: LookupValueShape.VALUE,
    Lookup.GTE: LookupValueShape.VALUE,
    Lookup.LT: LookupValueShape.VALUE,
    Lookup.LTE: LookupValueShape.VALUE,
    Lookup.IN: LookupValueShape.LIST,
    Lookup.NOT_IN: LookupValueShape.LIST,
    Lookup.RANGE: LookupValueShape.RANGE,
}

#: Lookups only an array/range/JSON value has - an annotation name accepts them in `.filter()`,
#: and they are rejected once the annotation's value turns out to be none of those.
ANNOTATION_CONTAINER_LOOKUPS = frozenset(
    {
        Lookup.CONTAINED_BY,
        Lookup.OVERLAP,
        Lookup.LENGTH,
        Lookup.ITEM,
        Lookup.HAS_KEY,
        Lookup.HAS_KEYS,
        Lookup.HAS_ANY_KEYS,
        Lookup.FILTER,
    }
)

#: Lookups of a JSON path value (`F("data__key")`) matched against the value's text.
JSON_PATH_TEXT_LOOKUPS = frozenset(
    {
        Lookup.CONTAINS,
        Lookup.STARTSWITH,
        Lookup.ENDSWITH,
        Lookup.IEXACT,
        Lookup.ICONTAINS,
        Lookup.ISTARTSWITH,
        Lookup.IENDSWITH,
        Lookup.POSIX_REGEX,
        Lookup.IPOSIX_REGEX,
        Lookup.SEARCH,
    }
)

#: Lookups of a JSON path value whose filter value is one JSON value (`""` is plain equality).
JSON_PATH_VALUE_LOOKUPS = frozenset({Lookup.EXACT, Lookup.NOT, Lookup.GT, Lookup.GTE, Lookup.LT, Lookup.LTE})

#: Lookups of a JSON path value testing the JSON object/array there, as on a whole JSONField.
JSON_PATH_CONTAINER_LOOKUPS = frozenset(
    {Lookup.CONTAINED_BY, Lookup.HAS_KEY, Lookup.HAS_KEYS, Lookup.HAS_ANY_KEYS, Lookup.FILTER}
)

#: What a lookup operator only dialects implement raises when reached - a dialect replaces it in its
#: ``FilterOperators``.
DIALECT_IMPLEMENTED_OPERATOR_MESSAGE = "the operator is implemented by a dialect"
