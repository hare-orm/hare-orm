from __future__ import annotations

import datetime
from collections.abc import Callable

#: A sanity ceiling, not a protection against catastrophic backtracking - a short pattern can hang
#: re.search() too.
MAX_REGEX_PATTERN_LENGTH = 1000

#: POSIX named character classes with a fixed member list on Postgres (UTF8), as the Python `re`
#: bracket-expression fragment used in place of the "[:name:]" token - "[[:digit:]]+" becomes
#: "[0-9]+".
POSIX_CHARACTER_CLASS_TRANSLATIONS = {
    "digit": "0-9",
    "space": r"\t\n\v\f\r \x85\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000",
    "cntrl": r"\x00-\x1f\x7f-\x9f",
    "xdigit": "0-9A-Fa-f",
    "blank": r" \t",
}

#: POSIX named character classes spanning the whole Unicode range, as a Python `re` pattern matching
#: one member character: letters, letters or ASCII digits, anything but a control character or a
#: line/paragraph separator, and the same without the spaces.
POSIX_CHARACTER_CLASS_PATTERNS = {
    "alpha": r"[^\W\d_]",
    "alnum": r"[^\W\d_]|[0-9]",
    "print": r"[^\x00-\x1f\x7f-\x9f\u2028\u2029]",
    "graph": r"[^\x00-\x20\x7f-\x9f\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000]",
}

#: POSIX named character classes whose member lists are collected from the code points below
#: ``POSIX_CLASS_MEMBER_CODE_POINT_LIMIT`` on first use.
POSIX_COLLECTED_CHARACTER_CLASS_NAMES = ("upper", "lower", "punct")

#: POSIX classes defined by case mapping.
POSIX_CASED_CHARACTER_CLASS_NAMES = ("upper", "lower")

#: End of the last Unicode block holding a character with a case mapping or a punctuation/symbol
#: character (Symbols for Legacy Computing).
POSIX_CLASS_MEMBER_CODE_POINT_LIMIT = 0x1FC00

#: Unicode categories of the POSIX ``punct`` class members: punctuation, symbols, other numbers
#: (superscripts, fractions), enclosing marks, format characters and the no-break spaces.
POSIX_PUNCT_CATEGORIES = frozenset(
    ("Pc", "Pd", "Pe", "Pf", "Pi", "Po", "Ps", "Sc", "Sk", "Sm", "So", "No", "Me", "Cf", "Zs")
)

#: Characters Postgres counts as lowercase although they have no one-character uppercase.
POSIX_LOWER_CLASS_EXTRA_CHARACTERS = "\N{LATIN SMALL LETTER SHARP S}"

#: POSIX classes Postgres widens to ``alpha`` in a case-insensitive match.
POSIX_CASE_INSENSITIVE_ALPHA_CLASS_NAMES = ("upper", "lower")

#: How many characters follow the escape letter of a fixed-length character escape (``\x41``,
#: ``\u0041``, ``\U00000041``) - copied verbatim, never read as pattern characters.
REGEX_ESCAPE_ARGUMENT_LENGTHS = {"x": 2, "u": 4, "U": 8}

#: Single-letter escapes standing for one control character.
REGEX_CHARACTER_ESCAPES = {"a": "\a", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}

#: Characters that may close the prefix of a ``(?...)`` group - after any inline flag letters.
REGEX_GROUP_PREFIX_TERMINATORS = ":)=!>"

#: Size of the compiled-pattern cache of the SQLite ``REGEXP``/``MATCH`` functions.
REGEX_PATTERN_CACHE_SIZE = 256

#: Length of an ISO date text (``YYYY-MM-DD``) - a stored date, not a datetime or a time.
DATE_TEXT_LENGTH = 10

#: Each EXTRACT() date part to the function the hare_extract_date_part UDF applies to a
#: zone-adjusted datetime - SQLite has no EXTRACT().
DATE_PART_EXTRACTORS: dict[str, Callable[[datetime.datetime], int]] = {
    "YEAR": lambda value: value.year,
    "ISOYEAR": lambda value: value.isocalendar()[0],
    "QUARTER": lambda value: (value.month - 1) // 3 + 1,
    "MONTH": lambda value: value.month,
    "WEEK": lambda value: value.isocalendar()[1],
    # 1 (Sunday) to 7 (Saturday), as Django's week_day.
    "DOW": lambda value: value.isoweekday() % 7 + 1,
    "ISODOW": lambda value: value.isoweekday(),
    "DAY": lambda value: value.day,
    "HOUR": lambda value: value.hour,
    "MINUTE": lambda value: value.minute,
    "SECOND": lambda value: value.second,
    "MICROSECOND": lambda value: value.microsecond,
}

#: Name of the Python UDF registered on every SQLite connection that rewrites a JSON text into a
#: canonical form (sorted keys, no whitespace, integral floats as ints) - backs JSONField's exact
#: and __not lookups, which compare by JSON value like Postgres jsonb does, not by stored text.
SQLITE_JSON_CANONICAL_FUNCTION_NAME = "hare_json_canonical"

#: UDF testing Postgres jsonb containment (``@>``) - backs ``contains``/``contained_by``.
SQLITE_JSON_CONTAINS_FUNCTION_NAME = "hare_json_contains"

#: The start of Unix time, the zero of the instant a JSON ``__filter`` aware datetime comparison uses.
SQLITE_JSON_DATETIME_EPOCH = datetime.datetime(1970, 1, 1)

#: Distinct stored/bound time texts whose TimeField collation sort key is kept parsed.
SQLITE_TIME_COLLATION_SORT_KEY_CACHE_SIZE = 4096

#: The only integer size Postgres casts a boolean to - its 32-bit ``integer``.
BOOLEAN_CAST_INTEGER_BITS = 32

#: Name of the Python UDF registered on every SQLite connection that gives any stored value's
#: exact decimal text (a double at its shortest round-tripping digits), compared under
#: `SQLITE_DECIMAL_COLLATION_NAME`.
SQLITE_DECIMAL_TEXT_FUNCTION_NAME = "hare_decimal_text"

#: Name of the Python UDF registered on every SQLite connection that gives the text a
#: `DECIMAL(max_digits, decimal_places)` column stores for a value an UPDATE sets it to.
SQLITE_DECIMAL_STORED_TEXT_FUNCTION_NAME = "hare_decimal_stored_text"

#: Name of the SQLite UDF comparing two JSON values in Postgres's ``jsonb`` order.
SQLITE_JSON_COMPARE_FUNCTION_NAME = "hare_json_compare"

#: The first byte of the sort key of an INTEGER or REAL, a TEXT and a BLOB value - SQLite orders
#: values of different storage classes so.
SQLITE_SORT_KEY_NUMBER_CLASS = b"\x10"

SQLITE_SORT_KEY_TEXT_CLASS = b"\x20"

SQLITE_SORT_KEY_BLOB_CLASS = b"\x30"
