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

#: The ``pk`` of ``Model.clone()`` when the call gives none - the copy then gets its key's default.
CLONE_PK_NOT_GIVEN = object()

#: The refusal of an instance loaded without its primary key, by the model's name and the action refused.
PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE = (
    "{model_name} was loaded by .only()/.defer() without its primary key - name it in .only() to {action} the object"
)


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

#: The start of the alias an expression of ``Meta.ordering`` is ordered by - never selected, followed
#: by its place among the expressions.
META_ORDERING_ALIAS_PREFIX = "hare meta ordering "

#: Every attempted restoration is completed before setter errors are reported.
INSTANCE_RESTORE_FAILURES_MESSAGE = "Instance restoration failures"

#: Persistence bookkeeping is restored without invoking model field setters.
INSTANCE_STATE_ATTRIBUTE_NAMES = frozenset(
    (
        "_saved_in_db",
        "_custom_generated_pk",
        "_connection_alias",
        "_dirty_snapshot",
        "_partial",
        "_await_when_save",
        "_allow_soft_delete_write",
    )
)

#: The persistence state a write's snapshot of an instance holds, in the order undo restores it backwards.
PERSISTENCE_STATE_ATTRIBUTE_NAMES = ["_saved_in_db", "_custom_generated_pk", "_connection_alias", "_dirty_snapshot"]

WRITE_RESTORE_FAILURES_MESSAGE = "Write failed and instance restoration also failed"

REFRESH_RESTORE_FAILURES_MESSAGE = "Refresh failed and instance restoration also failed"

#: Bind stored-key codecs only for batches large enough to amortize their preparation,
#: including container keys whose write/read conversion costs more than registry lookup.
MIN_STORED_KEY_CODEC_BATCH_SIZE = 8
