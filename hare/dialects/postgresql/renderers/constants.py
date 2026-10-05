from __future__ import annotations

#: The digest functions ENCODE(...(CONVERT_TO(text)), 'hex') gives the hex text of.
POSTGRESQL_DIGEST_FUNCTIONS = frozenset({"SHA224", "SHA256", "SHA384", "SHA512"})

#: The math functions with a NUMERIC variant only - every argument is cast to it.
POSTGRESQL_NUMERIC_ONLY_MATH_FUNCTIONS = frozenset({"MOD", "LOG"})

#: PostgreSQL's Now() db_default of a DateField/TimeField: the current wall clock in {zone} (a
#: quoted zone name or an INTERVAL offset), a TimeField's with {offset} attached.
POSTGRESQL_NOW_DATE_SQL_TEMPLATE = "((STATEMENT_TIMESTAMP() AT TIME ZONE {zone})::date)"

POSTGRESQL_NOW_TIME_SQL_TEMPLATE = (
    "(((((STATEMENT_TIMESTAMP() AT TIME ZONE {zone})::time)::text || '{offset}'))::timetz)"
)

#: PostgreSQL's Now(): the moment of the statement, as SQLite's and Django's - CURRENT_TIMESTAMP is
#: the start of the transaction.
POSTGRESQL_NOW_SQL = "STATEMENT_TIMESTAMP()"

POSTGRESQL_NOW_OFFSET_ZONE_TEMPLATE = "INTERVAL '{offset}'"

#: The UTC offset a naive time is bound with (use_timezone=False).
POSTGRESQL_NAIVE_TIME_OFFSET = "+00:00"

#: PostgreSQL's UuidV7() db_default.
POSTGRESQL_UUID_V7_SQL = "uuidv7()"

#: PostgreSQL's RandomHex() db_default: the MD5 of a random number, 32 lower-case hex digits.
POSTGRESQL_RANDOM_HEX_SQL = "md5(random()::text)"

#: A random float from 0 (included) to 1 (excluded).
POSTGRESQL_RANDOM_FLOAT_SQL = "random()"
