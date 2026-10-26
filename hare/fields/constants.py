from __future__ import annotations

import datetime
import uuid

from hare.fields.db_default_not_set import DbDefaultNotSet
from hare.fields.enums import OnDelete

#: The naive datetime.max/datetime.min - what a moment beyond the datetime range (a database's
#: ``infinity``/``-infinity``) is read as. They stand for instants, not for wall clocks of a zone.
NAIVE_INFINITY_DATETIMES = frozenset({datetime.datetime.max, datetime.datetime.min})


#: The least precision of the ``decimal`` context a ``DecimalField`` quantizes in - the default context's
#: precision; a ``max_digits`` above it raises the context's precision to ``max_digits``.
DECIMAL_QUANTIZE_MIN_CONTEXT_PRECISION = 28

#: Shown instead of a sensitive field's value in an error message.
SENSITIVE_VALUE_PLACEHOLDER = "<hidden>"

#: The suffix of a blind index field's name and column - ``email`` -> ``email_blind_index``.
BLIND_INDEX_NAME_SUFFIX = "_blind_index"

#: orjson decodes an integer literal outside its range as a float - a JSON text with a run of at
#: least this many digits is decoded by the standard library instead, which reads any int exactly.
JSON_LONG_INTEGER_MIN_DIGITS = 19
#: A run of JSON_LONG_INTEGER_MIN_DIGITS digits after JSON_DIGIT_MARKER_TABLE is applied.
JSON_LONG_INTEGER_MARKER = b"1" * JSON_LONG_INTEGER_MIN_DIGITS

#: The ``db_default`` of a field that declares none.
DB_DEFAULT_NOT_SET = DbDefaultNotSet()

#: The ``on_delete`` actions, importable by name - ``on_delete=CASCADE``.
CASCADE = OnDelete.CASCADE
RESTRICT = OnDelete.RESTRICT
SET_NULL = OnDelete.SET_NULL
SET_DEFAULT = OnDelete.SET_DEFAULT
NO_ACTION = OnDelete.NO_ACTION
PROTECT = OnDelete.PROTECT

#: The range of non-ASCII letters a domain name may hold, for the character classes below.
UNICODE_LETTER_RANGE = "\u00a1-\uffff"

#: A host name label: letters, digits and inner dashes, at most 63 characters.
HOSTNAME_REGEX = (
    r"[a-z"
    + UNICODE_LETTER_RANGE
    + r"0-9](?:[a-z"
    + UNICODE_LETTER_RANGE
    + r"0-9-]{0,61}[a-z"
    + UNICODE_LETTER_RANGE
    + r"0-9])?"
)

#: The labels after the host name, each of at most 63 characters (RFC 1034 sec. 3.1).
DOMAIN_REGEX = r"(?:\.(?!-)[a-z" + UNICODE_LETTER_RANGE + r"0-9-]{1,63}(?<!-))*"

#: The top-level domain, without the trailing dot of a fully qualified name.
TLD_NO_FQDN_REGEX = (
    r"\."  # dot
    r"(?!-)"  # can't start with a dash
    r"(?:[a-z" + UNICODE_LETTER_RANGE + "-]{2,63}"  # domain label
    r"|xn--[a-z0-9]{1,59})"  # or punycode label
    r"(?<!-)"  # can't end with a dash
)

#: The top-level domain, with or without the trailing dot.
TLD_REGEX = TLD_NO_FQDN_REGEX + r"\.?"


#: The constructor parameters of a field a migration never writes: the call's own, the model the field
#: is bound to, the validators, the ``db_default`` (written with the arguments every field takes) and
#: ``sensitive``, which never affects the schema.
UNWRITTEN_FIELD_CONSTRUCTOR_PARAMETERS = frozenset(
    {"self", "args", "kwargs", "model", "validators", "db_default", "sensitive"}
)

#: Bind parameters left unused by a many-to-many relation's batched through-table INSERTs, out of the
#: backend's own per-statement ceiling.
MANY_TO_MANY_WRITE_BIND_PARAMETERS_HEADROOM = 100

#: Column-name suffix appended to a relational field's own name to form its DB column.
FOREIGN_KEY_COLUMN_SUFFIX = "_id"

#: A field not loaded on the instance before the write - unlike None.
ROLLBACK_RESTORE_UNSET = object()

#: Why a LIKE pattern holding a null byte is refused where the database reads the pattern up to it
#: (``Features.cuts_like_pattern_at_null_byte``).
NULL_BYTE_MESSAGE = "value contains a null byte ('\\x00'), which the database reads a pattern up to"

#: The characters of a slug - ASCII letters, digits, hyphens and underscores.
SLUG_PATTERN = r"[-a-zA-Z0-9_]+"

#: The characters of a slug that allows Unicode - any letter or digit, hyphens and underscores.
UNICODE_SLUG_PATTERN = r"[-\w]+"

#: A phone number in E.164 form - "+", a country code not starting with 0, at most 15 digits in all.
E164_PHONE_PATTERN = r"\+[1-9][0-9]{1,14}"


#: How the type of a GenericForeignKeyField - the name of its branch set - is read and filtered:
#: ``target__type``.
GENERIC_FOREIGN_KEY_TYPE_SUFFIX = "__type"


#: The key of the branch name in the dict form of a GenericForeignKeyField value - its schema's
#: discriminator: ``{"type": "post", "id": 1}``.
GENERIC_FOREIGN_KEY_TYPE_FIELD = "type"

#: The module of each name ``hare.fields`` exports from a module importing the fields back.
EXPORTED_MODULES = {
    "ManyToManyRelation": "hare.query.queryset.relations.many_to_many_relation",
    "ReverseRelation": "hare.query.queryset.relations.reverse_relation",
}

#: The refusal of a text the schema is declared with that holds a null byte - no SQL text can hold one, so
#: a migration would fail only where it writes it, part of it applied.
SCHEMA_TEXT_NULL_BYTE_MESSAGE = (
    "{owner}: {parameter} is written into the schema, which can't hold a null byte ('\\x00')"
)

#: The refusal of a JSONField declaring field_type without pydantic - the type is validated through it.
JSON_FIELD_TYPE_PACKAGE_MESSAGE = "JSONField(field_type=...) validates through pydantic - install hare-orm[pydantic]"
#: The JSON form of a float JSON has no number for - a value's JSON form (``Field.get_json_value()``) holds
#: its text.
NAN_FLOAT_TEXT = "NaN"
NON_FINITE_FLOAT_TEXTS = {float("inf"): "Infinity", float("-inf"): "-Infinity"}

#: Python's own version 7 UUIDs (3.14 and newer), None where Python has none - Uuid7 makes them then.
UUID7_FACTORY = getattr(uuid, "uuid7", None)
#: The 74 random bits of a version 7 UUID, and the 62 of them after its variant.
UUID7_RANDOM_MASK = (1 << 74) - 1
UUID7_RANDOM_B_MASK = (1 << 62) - 1
