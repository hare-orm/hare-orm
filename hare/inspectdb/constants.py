from __future__ import annotations

import re

#: Dotted path of the field class a JSON column maps to.
JSON_FIELD_PATH = "hare.fields.data.json.JSONField"


#: Private sentinel kwarg keys ColumnTypeMapper.map_column_type() returns alongside real field
#: kwargs - popped before the kwargs ever reach a field constructor.
BASE_FIELD_SENTINEL_KWARG = "__base_field"
AMBIGUOUS_REASON_SENTINEL_KWARG = "__ambiguous_reason"

#: ``__module__`` of a model class ModelFactory builds at runtime.
FACTORY_MODEL_MODULE_NAME = "hare.inspectdb.generation.model_factory.live_models"


#: The keywords of a trigger's event clause (``insert or update of col``) - uppercased on
#: introspection, while the column names after ``OF`` keep their own spelling.
TRIGGER_EVENT_KEYWORD_RE = re.compile(r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|OR|OF)\b", re.IGNORECASE)


#: An index key's order when it places NULLs as its direction does by default (Postgres: last
#: ascending, first descending) - declarable as a plain or ``"-"``-prefixed field name.
DEFAULT_KEY_ORDERS = frozenset({"ASC NULLS LAST", "DESC NULLS FIRST"})

INT_LITERAL_RE = re.compile(r"^-?\d+$")

FLOAT_LITERAL_RE = re.compile(r"^-?\d+\.\d+$")

#: A quoted string literal - a dialect's type cast after it already split off.
QUOTED_STRING_RE = re.compile(r"^'((?:[^']|'')*)'$")

CONDITION_AND_SPLIT_RE = re.compile(r"\)\s+AND\s+\(")

#: A ``column = value`` term of a predicate: the column side ends at the first ``=`` outside its
#: quoted name - whatever the database writes after the column (a type cast) is part of it.
CONDITION_TERM_RE = re.compile(
    r'^(?P<column_sql>\(?\s*(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_]*)[^=]*?)\s*=\s*(?P<value>.+)$'
)
#: The column of a predicate's term, bare or double-quoted (SQLite echoes hare's own quoting),
#: optionally in parentheses.
CONDITION_COLUMN_RE = re.compile(
    r'^\(?\s*(?:"(?P<quoted_column>(?:[^"]|"")+)"|(?P<bare_column>[A-Za-z_][A-Za-z0-9_]*))\s*\)?$'
)

QUOTED_IDENTIFIER_RE = re.compile(r'"(?:[^"]|"")*"')

#: A run of whitespace - collapsed to one space.
WHITESPACE_RUN_PATTERN = re.compile(r"\s+")
