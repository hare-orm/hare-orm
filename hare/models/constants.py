from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any
from uuid import UUID

from hare.fields.database_default import DatabaseDefault
from hare.models.enums import ModelOption
from hare.query.expressions.expression import Expression

#: Field-value types a snapshot/clone shares as-is instead of deep-copying - an in-place mutation
#: can't change any of them, so the copy would only cost time. Every other value (dict, list, set,
#: bytearray, custom field objects) is deep-copied.
IMMUTABLE_FIELD_VALUE_TYPES: tuple[type, ...] = (
    int,
    float,
    str,
    bool,
    bytes,
    type(None),
    datetime,
    date,
    time,
    timedelta,
    Decimal,
    UUID,
    Enum,
)

#: Field-value types a snapshot/clone shares as-is and compares by identity - a ``db_default``
#: placeholder or an unresolved ``F()``-expression awaiting its write, not a real value.
PLACEHOLDER_FIELD_VALUE_TYPES: tuple[type, ...] = (DatabaseDefault, Expression)
#: The field values a snapshot keeps as they are - immutable ones and placeholders.
UNCOPIED_FIELD_VALUE_TYPES: tuple[type, ...] = (*IMMUTABLE_FIELD_VALUE_TYPES, *PLACEHOLDER_FIELD_VALUE_TYPES)


EMPTY = object()


#: Meta attributes merged across unrelated abstract bases by concatenation - each is a collection of
#: separately declared entries. Along one ancestor chain a redeclared key still replaces.
ADDITIVE_ABSTRACT_META_KEYS = (
    ModelOption.CONSTRAINTS,
    ModelOption.INDEXES,
    ModelOption.TRIGGERS,
    ModelOption.EXTENSIONS,
    ModelOption.VIEWS,
    ModelOption.MATERIALIZED_VIEWS,
    ModelOption.DICTIONARIES,
    ModelOption.FUNCTIONS,
    ModelOption.SEQUENCES,
    ModelOption.POLICIES,
    ModelOption.GRANTS,
)

#: The pending async defaults of an instance with none - shared, read-only; an instance gets a dict of
#: its own with its first pending default (``InstanceInitialization.add_pending_default()``).
EMPTY_PENDING_DEFAULTS: MappingProxyType[str, Any] = MappingProxyType({})
