import datetime

from hare.query.enums import Lookup, LookupValueShape
from hare.sql.enums import DatePart

#: Suffix -> DatePart for the 5 `__year`/`__quarter`/`__month`/`__week`/`__day` lookups a
#: `DateField` has a real calendar value for - also the calendar half of what a `DatetimeField`
#: registers.
CALENDAR_DATE_PART_LOOKUPS: dict[str, DatePart] = {
    "year": DatePart.YEAR,
    "iso_year": DatePart.ISO_YEAR,
    "quarter": DatePart.QUARTER,
    "month": DatePart.MONTH,
    "week": DatePart.WEEK,
    "week_day": DatePart.WEEK_DAY,
    "iso_week_day": DatePart.ISO_WEEK_DAY,
    "day": DatePart.DAY,
}

#: Suffix -> DatePart for the 4 `__hour`/`__minute`/`__second`/`__microsecond` lookups a
#: `TimeField` has a real time-of-day value for - also the time-of-day half of what a
#: `DatetimeField` registers.
TIME_OF_DAY_DATE_PART_LOOKUPS: dict[str, DatePart] = {
    "hour": DatePart.HOUR,
    "minute": DatePart.MINUTE,
    "second": DatePart.SECOND,
    "microsecond": DatePart.MICROSECOND,
}

#: Suffix -> DatePart for every date part of a `DatetimeField`, which has both a calendar and a
#: time-of-day component.
DATETIME_DATE_PART_LOOKUPS: dict[str, DatePart] = {**CALENDAR_DATE_PART_LOOKUPS, **TIME_OF_DAY_DATE_PART_LOOKUPS}

#: The segments reading a datetime's date or time of day (``created__date``, ``created__time``).
DATETIME_CAST_SEGMENTS = frozenset({"date", "time"})

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
        Lookup.LEN,
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

#: Lookups of a JSON path value whose filter value is a list of JSON values.
JSON_PATH_LIST_LOOKUPS = frozenset({Lookup.IN, Lookup.NOT_IN, Lookup.RANGE})

#: Lookups whose filter value is a list of values.
LIST_VALUE_LOOKUPS = frozenset({Lookup.IN, Lookup.NOT_IN})

#: Lookups whose filter value is a bool.
BOOLEAN_VALUE_LOOKUPS = frozenset({Lookup.ISNULL, Lookup.NOT_ISNULL})

#: Lookups matching a value's text, whose filter value is a string.
TEXT_VALUE_LOOKUPS = frozenset(
    {
        Lookup.IEXACT,
        Lookup.CONTAINS,
        Lookup.ICONTAINS,
        Lookup.STARTSWITH,
        Lookup.ISTARTSWITH,
        Lookup.ENDSWITH,
        Lookup.IENDSWITH,
        Lookup.POSIX_REGEX,
        Lookup.IPOSIX_REGEX,
        Lookup.SEARCH,
    }
)

#: Lookups of a JSON or hstore value naming keys - ``has_keys``/``has_any_keys`` take a list of them.
KEY_LIST_LOOKUPS = frozenset({Lookup.HAS_KEYS, Lookup.HAS_ANY_KEYS})

#: The type of the value a date transform reads - a date part is an integer, ``date``/``time``
#: the datetime's date / time of day.
DATE_TRANSFORM_VALUE_TYPES: dict[str, type] = {
    **dict.fromkeys(DATETIME_DATE_PART_LOOKUPS, int),
    "date": datetime.date,
    "time": datetime.time,
}

#: ISO-8601 date / datetime text a JSON `__filter` date comparison accepts - a stored string not
#: matching it (or naming a day its month doesn't have) never matches instead of failing the
#: whole query on a CAST error. Year 0000 is excluded: Postgres has no year zero.
JSON_ISO_DATETIME_PATTERN = (
    r"^(?!0000)[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"
    r"([T ]([01][0-9]|2[0-3]):[0-5][0-9](:[0-5][0-9](\.[0-9]+)?)?(Z|[+-]([01][0-9]|2[0-3])(:?[0-5][0-9])?)?)?$"
)


DEFAULT_LIKE_ESCAPE_CLAUSE = " ESCAPE '\\'"


#: Order matters - the backslash escape must be applied first, or it would double-escape
#: the backslashes just inserted for "%"/"_".
LIKE_ESCAPE_MAP = (
    ("\\", "\\\\"),
    ("%", "\\%"),
    ("_", "\\_"),
)
