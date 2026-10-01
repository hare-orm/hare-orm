import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum
from uuid import UUID

from hare.fields.base.database_default import DatabaseDefault
from hare.fields.enums import OnDelete
from hare.models.enums import ModelOption
from hare.query.expressions.base.expression import Expression

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

#: ``on_delete`` actions whose rows block a delete via ``ProtectedError``.
PROTECTING_ON_DELETE_ACTIONS: frozenset[OnDelete] = frozenset({OnDelete.PROTECT})

#: ``on_delete`` actions whose rows block a delete via ``IntegrityError``.
RESTRICTING_ON_DELETE_ACTIONS: frozenset[OnDelete] = frozenset({OnDelete.RESTRICT, OnDelete.NO_ACTION})

#: Most related keys one check of a written row's tenant-scoped relation targets looks up in a
#: single query - room under every backend's bind-parameter and expression-depth limits.
TENANT_RELATION_CHECK_BATCH_SIZE = 500


#: A run of "#:" comment lines right before an attribute - the attribute's documentation.
FIELD_COMMENT_RE = re.compile(r"((?:^[ \t]*#:.*\n)+)[ \t]*(\w+)\s*[:=]", re.MULTILINE)

#: The fields of a parsed statement holding nested statements - where a class definition can be.
STATEMENT_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")


EMPTY = object()


#: Meta attributes merged across unrelated abstract bases by concatenation - each is a collection of
#: separately declared entries. Along one ancestor chain a redeclared key still replaces.
ADDITIVE_ABSTRACT_META_KEYS = (
    ModelOption.CONSTRAINTS,
    ModelOption.INDEXES,
    ModelOption.TRIGGERS,
    ModelOption.EXTENSIONS,
)


#: Bind parameters left unused by the deletion cascade's batched ``IN`` lookups, out of the
#: backend's own per-statement ceiling - room for whatever other parameters the same statement
#: carries (a tenant filter, a soft-delete filter, ...).
CASCADE_LOOKUP_BIND_PARAMS_HEADROOM = 100

#: Most composite-key rows the deletion cascade ORs together in a single lookup - each row
#: becomes one AND-group of a flat OR chain, and a chain that long can hit a backend's own
#: expression-depth limit (SQLite's is 1000) well before the bind-parameter ceiling.
CASCADE_LOOKUP_MAX_COMPOSITE_ROWS = 500

#: Meta options a model can no longer declare -> what to declare instead.
UNSUPPORTED_META_OPTIONS = {
    "unique_together": "declare the uniqueness as UniqueConstraint(fields=(...)) in Meta.constraints",
    "version_field": "name the optimistic lock field in Meta.optimistic_lock_field",
}

#: The name of the recursive ``WITH`` listing the rows below deleted ones through a relation of a
#: model onto itself.
DELETION_TREE_CTE_NAME = "hare_deletion_tree"
