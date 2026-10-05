from __future__ import annotations

import datetime
from collections.abc import Callable

from hare.lazy_loading.lazy_pattern import LazyPattern

#: A sanity ceiling, not a protection against catastrophic backtracking - a short pattern can hang
#: re.search() too.
MAX_REGEX_PATTERN_LENGTH = 1000

#: POSIX named character classes with a fixed member list on Postgres (UTF8), as the Python `re`
#: bracket-expression fragment used in place of the "[:name:]" token - "[[:digit:]]+" becomes
#: "[0-9]+".
POSIX_CHARACTER_CLASS_TRANSLATIONS = {
    "digit": "0-9",
    "space": r"\t\n\v\f\r \u1680\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000",
    "cntrl": r"\x00-\x1f\x7f-\x9f",
    "xdigit": "0-9A-Fa-f",
    "blank": r" \t",
}

#: POSIX named character classes spanning the whole Unicode range, as a Python `re` pattern matching
#: one member character: anything but a control character or a line/paragraph separator, and the
#: same without the spaces. ``alpha`` and ``alnum`` are made of ``POSIX_ALPHA_PATTERN_TEMPLATE``.
POSIX_CHARACTER_CLASS_PATTERNS = {
    "print": r"[^\x00-\x1f\x7f-\x9f\u2028\u2029]",
    "graph": r"[^\x00-\x20\x7f-\x9f\u2000-\u2006\u2008-\u200a\u2028\u2029\u205f\u3000]",
}

#: The POSIX ``alpha`` class as Postgres (UTF8, glibc) has it - a word character of Python's but an
#: underscore, an ASCII digit or another number (``½``, ``²``): letters, letter numbers and the
#: digits of other scripts. {0}: the bracket-expression fragment of the other numbers.
POSIX_ALPHA_PATTERN_TEMPLATE = r"(?![{0}])[^\W_0-9]"

#: The collected class of the other numbers (``No``) - Python's word characters, not Postgres's.
POSIX_OTHER_NUMBER_CLASS_NAME = "other_number"

#: POSIX named character classes whose member lists are collected from the code points below
#: ``POSIX_CLASS_MEMBER_CODE_POINT_LIMIT`` on first use, and the other numbers.
POSIX_COLLECTED_CHARACTER_CLASS_NAMES = ("upper", "lower", "punct", POSIX_OTHER_NUMBER_CLASS_NAME)

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

#: How many hex digits follow the escape letter of a fixed-length character escape (``\u0041``,
#: ``\U00000041``) - ``\x`` takes any number.
REGEX_ESCAPE_ARGUMENT_LENGTHS = {"u": 4, "U": 8}

#: Single-letter escapes of an advanced regular expression standing for one character - ``\b`` a
#: backspace, ``\B`` a backslash, as on Postgres.
REGEX_CHARACTER_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "B": "\\",
    "e": "\x1b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
}

#: The letters of a regular expression's embedded options - ``(?in)`` at its start.
REGEX_OPTION_LETTERS = frozenset("bceimnpqstwx")

#: The most times a bound repeats an atom (``a{255}``) - Postgres's ``DUPMAX``.
REGEX_MAX_REPETITION = 255

#: The most bytes Postgres reads of a ``\x`` escape's hex digits - more are refused.
REGEX_MAX_CODE_POINT = 0x10FFFF

#: A bound of a regular expression after its ``{`` - ``m``, ``m,`` or ``m,n``.
REGEX_BOUND_PATTERN = LazyPattern(r"(\d+)(,(\d*))?")
#: The hex digits of a ``\x`` escape.
REGEX_HEX_DIGITS_PATTERN = LazyPattern("[0-9A-Fa-f]+")
#: The digits after a backslash - a back reference or an octal escape.
REGEX_DIGITS_PATTERN = LazyPattern(r"\d+")
#: The digits of an octal escape.
REGEX_OCTAL_DIGITS_PATTERN = LazyPattern("[0-7]{1,3}")

#: The director of a regular expression read as literal text, and of an advanced one.
REGEX_LITERAL_DIRECTOR = "***="
REGEX_ADVANCED_DIRECTOR = "***:"

#: The whitespace an expanded regular expression (``(?x)``) skips outside a bracket expression.
REGEX_EXPANDED_WHITESPACE = frozenset(" \t\n\r\f\v")

#: The bracket expressions standing for the start and the end of a word.
REGEX_WORD_START_BRACKET = "[[:<:]]"
REGEX_WORD_END_BRACKET = "[[:>:]]"

#: The beginning of every error of a regular expression - Postgres's.
REGEX_ERROR_PREFIX = "invalid regular expression: "

#: Postgres's errors of a regular expression.
REGEX_UNBALANCED_PARENTHESES_MESSAGE = "parentheses () not balanced"
REGEX_UNBALANCED_BRACKETS_MESSAGE = "brackets [] not balanced"
REGEX_UNBALANCED_BRACES_MESSAGE = "braces {} not balanced"
REGEX_QUANTIFIER_OPERAND_MESSAGE = "quantifier operand invalid"
REGEX_INVALID_ESCAPE_MESSAGE = "invalid escape \\ sequence"
REGEX_INVALID_OPTION_MESSAGE = "invalid embedded option"
REGEX_INVALID_BACKREFERENCE_MESSAGE = "invalid backreference number"
REGEX_INVALID_REPETITION_MESSAGE = "invalid repetition count(s)"
REGEX_INVALID_RANGE_MESSAGE = "invalid character range"
REGEX_INVALID_CLASS_MESSAGE = "invalid character class"
REGEX_INVALID_COLLATING_ELEMENT_MESSAGE = "invalid collating element"

#: Python's errors of a translated pattern -> Postgres's.
REGEX_PYTHON_ERROR_MESSAGES = {
    "bad character range": REGEX_INVALID_RANGE_MESSAGE,
    "missing ), unterminated subpattern": REGEX_UNBALANCED_PARENTHESES_MESSAGE,
    "unbalanced parenthesis": REGEX_UNBALANCED_PARENTHESES_MESSAGE,
    "unterminated character set": REGEX_UNBALANCED_BRACKETS_MESSAGE,
    "nothing to repeat": REGEX_QUANTIFIER_OPERAND_MESSAGE,
    "multiple repeat": REGEX_QUANTIFIER_OPERAND_MESSAGE,
    "min repeat greater than max repeat": REGEX_INVALID_REPETITION_MESSAGE,
    "invalid group reference": REGEX_INVALID_BACKREFERENCE_MESSAGE,
}

#: The names of the collating elements Postgres knows (``[[.hyphen.]]``) - the characters of ASCII.
REGEX_COLLATING_ELEMENT_NAMES = {
    "NUL": "\x00",
    "SOH": "\x01",
    "STX": "\x02",
    "ETX": "\x03",
    "EOT": "\x04",
    "ENQ": "\x05",
    "ACK": "\x06",
    "BEL": "\x07",
    "alert": "\x07",
    "BS": "\x08",
    "backspace": "\x08",
    "HT": "\t",
    "tab": "\t",
    "LF": "\n",
    "newline": "\n",
    "VT": "\v",
    "vertical-tab": "\v",
    "FF": "\f",
    "form-feed": "\f",
    "CR": "\r",
    "carriage-return": "\r",
    "SO": "\x0e",
    "SI": "\x0f",
    "DLE": "\x10",
    "DC1": "\x11",
    "DC2": "\x12",
    "DC3": "\x13",
    "DC4": "\x14",
    "NAK": "\x15",
    "SYN": "\x16",
    "ETB": "\x17",
    "CAN": "\x18",
    "EM": "\x19",
    "SUB": "\x1a",
    "ESC": "\x1b",
    "IS4": "\x1c",
    "FS": "\x1c",
    "IS3": "\x1d",
    "GS": "\x1d",
    "IS2": "\x1e",
    "RS": "\x1e",
    "IS1": "\x1f",
    "US": "\x1f",
    "space": " ",
    "exclamation-mark": "!",
    "quotation-mark": '"',
    "number-sign": "#",
    "dollar-sign": "$",
    "percent-sign": "%",
    "ampersand": "&",
    "apostrophe": "'",
    "left-parenthesis": "(",
    "right-parenthesis": ")",
    "asterisk": "*",
    "plus-sign": "+",
    "comma": ",",
    "hyphen": "-",
    "hyphen-minus": "-",
    "period": ".",
    "full-stop": ".",
    "slash": "/",
    "solidus": "/",
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
    "colon": ":",
    "semicolon": ";",
    "less-than-sign": "<",
    "equals-sign": "=",
    "greater-than-sign": ">",
    "question-mark": "?",
    "commercial-at": "@",
    "left-square-bracket": "[",
    "backslash": "\\",
    "reverse-solidus": "\\",
    "right-square-bracket": "]",
    "circumflex": "^",
    "circumflex-accent": "^",
    "underscore": "_",
    "low-line": "_",
    "grave-accent": "`",
    "left-brace": "{",
    "left-curly-bracket": "{",
    "vertical-line": "|",
    "right-brace": "}",
    "right-curly-bracket": "}",
    "tilde": "~",
    "DEL": "\x7f",
}

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
#: How many address texts the address collation keeps the sort key of.
SQLITE_ADDRESS_COLLATION_SORT_KEY_CACHE_SIZE = 4096

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

#: The most bytes Postgres allocates for one value - a longer text is refused.
POSTGRES_MAX_ALLOCATION_BYTES = 0x3FFFFFFF

#: The bytes of the length header Postgres stores before a text.
POSTGRES_TEXT_HEADER_BYTES = 4

#: The bytes Postgres counts for each character of a padded text - UTF-8's longest character.
POSTGRES_PADDED_CHARACTER_BYTES = 4

#: Postgres's error of a text longer than it allocates.
REQUESTED_LENGTH_TOO_LARGE_MESSAGE = "requested length too large"

#: Postgres's error of an integer beyond its type.
INTEGER_OUT_OF_RANGE_MESSAGE = "integer out of range"

#: Postgres's error of a float result too large for a double.
FLOAT_OVERFLOW_MESSAGE = "value out of range: overflow"

#: Postgres's error of a float result too small for a double.
FLOAT_UNDERFLOW_MESSAGE = "value out of range: underflow"

#: Postgres's error of a math function's argument outside its domain, by the function's name; a
#: function not named here says ``MATH_INPUT_OUT_OF_RANGE_MESSAGE``.
MATH_DOMAIN_MESSAGES = {
    "sqrt": "cannot take square root of a negative number",
}

#: Postgres's error of a trigonometric function's argument outside its domain.
MATH_INPUT_OUT_OF_RANGE_MESSAGE = "input is out of range"

#: Postgres's error of the logarithm of zero.
LOGARITHM_OF_ZERO_MESSAGE = "cannot take logarithm of zero"

#: Postgres's error of the logarithm of a negative number.
LOGARITHM_OF_NEGATIVE_MESSAGE = "cannot take logarithm of a negative number"

#: Postgres's error of a negative number raised to a fractional power.
NEGATIVE_FRACTIONAL_POWER_MESSAGE = "a negative number raised to a non-integer power yields a complex result"

#: Postgres's error of zero raised to a negative power.
ZERO_NEGATIVE_POWER_MESSAGE = "zero raised to a negative power is undefined"

#: The functions of ``TemporalArithmeticFunctions`` a connection calls.
TEMPORAL_ARITHMETIC_FUNCTION_NAMES = ("shift_datetime", "shift_date", "difference_datetime", "difference_date")

#: Postgres's error of a moment shifted beyond its range.
TIMESTAMP_OUT_OF_RANGE_MESSAGE = "timestamp out of range"

#: Postgres's error of a date shifted beyond its range.
DATE_OUT_OF_RANGE_MESSAGE = "date out of range"

#: Name of the SQLite UDF writing a JSON value as the text Postgres's ``jsonb`` writes of it.
SQLITE_JSON_TEXT_FUNCTION_NAME = "hare_json_text"

#: Name of the SQLite UDF giving the value at a JSON path as the text Postgres's ``->>`` gives.
SQLITE_JSON_PATH_TEXT_FUNCTION_NAME = "hare_json_path_text"

#: The first byte of the sort key of an INTEGER or REAL, a TEXT and a BLOB value - SQLite orders
#: values of different storage classes so.
SQLITE_SORT_KEY_NUMBER_CLASS = b"\x10"

SQLITE_SORT_KEY_TEXT_CLASS = b"\x20"

SQLITE_SORT_KEY_BLOB_CLASS = b"\x30"
