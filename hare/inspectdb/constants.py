import re

#: Dotted path of the field class a JSON column maps to.
JSON_FIELD_PATH = "hare.fields.data.json.JSONField"


#: Private sentinel kwarg keys ColumnTypeMapper.map_column_type() returns alongside real field
#: kwargs - popped before the kwargs ever reach a field constructor.
BASE_FIELD_SENTINEL_KWARG = "__base_field"
AMBIGUOUS_REASON_SENTINEL_KWARG = "__ambiguous_reason"

#: ``__module__`` of a model class ModelFactory builds at runtime.
FACTORY_MODEL_MODULE_NAME = "hare.inspectdb.model_factory.live_models"


#: The keywords of a trigger's event clause (``insert or update of col``) - uppercased on
#: introspection, while the column names after ``OF`` keep their own spelling.
TRIGGER_EVENT_KEYWORD_RE = re.compile(r"\b(?:INSERT|UPDATE|DELETE|TRUNCATE|OR|OF)\b", re.IGNORECASE)


#: An index key's order when it places NULLs as its direction does by default (Postgres: last
#: ascending, first descending) - declarable as a plain or ``"-"``-prefixed field name.
DEFAULT_KEY_ORDERS = frozenset({"ASC NULLS LAST", "DESC NULLS FIRST"})

INT_LITERAL_RE = re.compile(r"^-?\d+$")

FLOAT_LITERAL_RE = re.compile(r"^-?\d+\.\d+$")

#: A quoted string literal, optionally followed by a Postgres type cast (e.g.
#: "'active'::character varying") - the cast is just how Postgres echoes column_default back,
#: not part of the value itself.
QUOTED_STRING_RE = re.compile(r"^'((?:[^']|'')*)'(?:::(?P<cast_type>[\w \[\]]+))?$")

CONDITION_AND_SPLIT_RE = re.compile(r"\)\s+AND\s+\(")

# A column is bare or double-quoted (SQLite echoes hare's own quoting), optionally wrapped
# in parentheses and type-cast the way Postgres prints a varchar column: "(isbn)::text".
CONDITION_TERM_RE = re.compile(
    r'^\(?\s*(?:"(?P<quoted_column>(?:[^"]|"")+)"|(?P<bare_column>[A-Za-z_][A-Za-z0-9_]*))\s*\)?'
    r"(?:::[A-Za-z_][\w ]*)?\s*=\s*(?P<value>.+)$"
)

QUOTED_IDENTIFIER_RE = re.compile(r'"(?:[^"]|"")*"')
