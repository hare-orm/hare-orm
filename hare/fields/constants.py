from __future__ import annotations

import datetime

from hare.fields.db_default_not_set import DbDefaultNotSet
from hare.fields.enums import OnDelete

#: The naive datetime.max/datetime.min - what a moment beyond the datetime range (a database's
#: ``infinity``/``-infinity``) is read as. They stand for instants, not for wall clocks of a zone.
NAIVE_INFINITY_DATETIMES = frozenset({datetime.datetime.max, datetime.datetime.min})


#: Floor for DecimalField's own quantize() Context precision - matches decimal's own default
#: context precision, so a max_digits at or below it keeps the exact previous behavior.
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


DB_DEFAULT_NOT_SET = DbDefaultNotSet()


CASCADE = OnDelete.CASCADE


RESTRICT = OnDelete.RESTRICT


SET_NULL = OnDelete.SET_NULL


SET_DEFAULT = OnDelete.SET_DEFAULT


NO_ACTION = OnDelete.NO_ACTION


PROTECT = OnDelete.PROTECT


UL = "\u00a1-\uffff"


HOSTNAME_REGEX = r"[a-z" + UL + r"0-9](?:[a-z" + UL + r"0-9-]{0,61}[a-z" + UL + r"0-9])?"


# Max length for domain name labels is 63 characters per RFC 1034 sec. 3.1.
DOMAIN_REGEX = r"(?:\.(?!-)[a-z" + UL + r"0-9-]{1,63}(?<!-))*"


# Top-level domain.
TLD_NO_FQDN_REGEX = (
    r"\."  # dot
    r"(?!-)"  # can't start with a dash
    r"(?:[a-z" + UL + "-]{2,63}"  # domain label
    r"|xn--[a-z0-9]{1,59})"  # or punycode label
    r"(?<!-)"  # can't end with a dash
)


TLD_REGEX = TLD_NO_FQDN_REGEX + r"\.?"


#: Bind parameters left unused by a many-to-many relation's batched through-table INSERTs, out of the
#: backend's own per-statement ceiling.
MANY_TO_MANY_WRITE_BIND_PARAMETERS_HEADROOM = 100

#: Column-name suffix appended to a relational field's own name to form its DB column.
FOREIGN_KEY_COLUMN_SUFFIX = "_id"

#: A field not loaded on the instance before the write - unlike None.
ROLLBACK_RESTORE_UNSET = object()

#: Why a text or JSON value holding a null byte is refused - PostgreSQL's text protocol can't carry
#: one in any text type, and SQLite cuts a LIKE pattern at it.
NULL_BYTE_MESSAGE = "value contains a null byte ('\\x00'), which JSON/text columns can't store"

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
