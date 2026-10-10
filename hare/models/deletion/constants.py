from __future__ import annotations

from hare.fields.enums import OnDelete
from hare.models.enums import DeletionAction

#: The actions of rows their own model's delete takes over - the cascade doesn't go below them.
HANDED_OVER_ACTIONS = frozenset({DeletionAction.OWN_DELETE, DeletionAction.OWN_DISPATCH})

#: ``on_delete`` actions whose rows block a delete via ``ProtectedError``.
PROTECTING_ON_DELETE_ACTIONS: frozenset[OnDelete] = frozenset({OnDelete.PROTECT})

#: ``on_delete`` actions whose rows block a delete via ``IntegrityError``.
RESTRICTING_ON_DELETE_ACTIONS: frozenset[OnDelete] = frozenset({OnDelete.RESTRICT, OnDelete.NO_ACTION})

#: Bind parameters left unused by the deletion cascade's batched ``IN`` lookups, out of the
#: backend's own per-statement ceiling - room for whatever other parameters the same statement
#: carries (a tenant filter, a soft-delete filter, ...).
CASCADE_LOOKUP_BIND_PARAMETERS_HEADROOM = 100

#: Most composite-key rows the deletion cascade ORs together in a single lookup - each row
#: becomes one AND-group of a flat OR chain, and a chain that long can hit a backend's own
#: expression-depth limit (SQLite's is 1000) well before the bind-parameter ceiling.
CASCADE_LOOKUP_MAX_COMPOSITE_ROWS = 500

#: The name of the recursive ``WITH`` listing the rows below deleted ones through a relation of a
#: model onto itself.
DELETION_TREE_CTE_NAME = "hare_deletion_tree"
