from __future__ import annotations

import datetime

from hare.query.enums import Lookup
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

#: The connection feature (a ``Features`` attribute) a lookup needs beyond its dialect: SQLite runs
#: the regular expression lookups only on a connection that installed its ``REGEXP`` functions
#: (DB_URL ``?install_regexp_functions=true``).
LOOKUP_REQUIRED_FEATURES: dict[str, str] = {
    Lookup.POSIX_REGEX: "supports_posix_regex",
    Lookup.IPOSIX_REGEX: "supports_posix_regex",
}
