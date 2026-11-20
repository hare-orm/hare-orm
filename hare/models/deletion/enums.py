from __future__ import annotations

from enum import StrEnum


class DeletionRoute(StrEnum):
    """How the rows of a deletion are removed - chosen after a model's own ``delete()`` has run."""

    #: A soft-deleting model no other model references: its rows are marked deleted by one UPDATE.
    SOFT_UPDATE = "soft_update"
    #: A soft-deleting model other models reference: the mark cascades to its referencing rows.
    SOFT_CASCADE = "soft_cascade"
    #: hare walks the cascade itself (``CascadeDeletion``) - for a relation the database doesn't
    #: enforce, ``Meta.change_capture`` or a model overriding ``delete()``.
    PYTHON_CASCADE = "python_cascade"
    #: A cycle of database-enforced CASCADE relations on a database limiting its cascade depth.
    DEPTH_LIMITED = "depth_limited"
    #: A PROTECT relation inside the cascade on a database checking foreign keys per cascade step:
    #: the check is deferred to the end of the statement.
    PROTECT_DEFERRED = "protect_deferred"
    #: One DELETE statement; the database cascades it.
    STATEMENT = "statement"
