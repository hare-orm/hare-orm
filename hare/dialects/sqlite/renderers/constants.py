from __future__ import annotations

import re

from hare.lazy_loading.lazy_pattern import LazyPattern

#: What SQLite's column DEFAULT takes without parentheses: a signed number or a literal.
SQLITE_BARE_DEFAULT_PATTERN = LazyPattern(
    r"\s*(?:[+-]?\s*(?:(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|0x[0-9a-f]+)|'(?:[^']|'')*'|x'[0-9a-f]*'"
    r"|null|true|false|current_time|current_date|current_timestamp)\s*",
    re.IGNORECASE,
)

#: A call to one of hare's own SQLite functions or collations in rendered SQL - a condition written
#: into a SQLite constraint or index can't use one: it doesn't exist outside hare's connections.
SQLITE_OWN_FUNCTION_PATTERN = LazyPattern(r'(?<!")\bhare_\w+')

#: A random float from 0 to 1 - SQLite's random() is a 64-bit integer, its sign bit masked off here.
SQLITE_RANDOM_FLOAT_SQL = "((random() & 9223372036854775807) / 9223372036854775808.0)"

#: SQLite's own function of the same semantics, by the Postgres name ``TextFunction`` takes.
SQLITE_NATIVE_TEXT_FUNCTIONS = {"LTRIM": "ltrim", "RTRIM": "rtrim", "REPLACE": "replace", "STRPOS": "instr"}

SQLITE_NOW_TIME_TEXT_FORMAT = "%H:%M:%S"

#: SQLite's Now() db_default of a DateField - {moment} as for SQLITE_NOW_SQL_TEMPLATE.
SQLITE_NOW_DATE_SQL_TEMPLATE = "(date({moment}))"

#: SQLite's 'now' shifted by a whole number of minutes (a zone with one constant UTC offset).
SQLITE_NOW_SHIFTED_MOMENT_TEMPLATE = "'now', '{minutes:+d} minutes'"

SQLITE_NOW_LOCAL_MOMENT = "'now', 'localtime'"

#: SQLite's RandomHex() db_default: 16 random bytes as 32 lower-case hex digits.
SQLITE_RANDOM_HEX_SQL = "(lower(hex(randomblob(16))))"

#: Largest Decimal scale SQLite computes an exact remainder for - both operands are scaled to
#: 64-bit integers by ``10 ** scale`` first.
SQLITE_DECIMAL_MOD_MAX_SCALE = 15
