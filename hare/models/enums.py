from __future__ import annotations

from enum import StrEnum


class FieldBucket(StrEnum):
    """Which row-hydration path a field's raw DB value needs on a type of connection - see
    `HydrationLayout`."""

    NATIVE = "native"
    DEFAULT = "default"
    COMPLEX = "complex"


class ModelOption(StrEnum):
    """Name of a model option (a ``Meta`` attribute kept in migration state)."""

    ABSTRACT = "abstract"
    CHANGE_CAPTURE = "change_capture"
    APP = "app"
    CONSTRAINTS = "constraints"
    EXTENSIONS = "extensions"
    GET_LATEST_BY = "get_latest_by"
    INDEXES = "indexes"
    MANAGED = "managed"
    MANAGER = "manager"
    ORDERING = "ordering"
    PRIMARY_KEY_ATTRIBUTE = "primary_key_attribute"
    PK_WITHOUT_OVERLAPS = "pk_without_overlaps"
    PRIMARY_KEY = "primary_key"
    RETURNING = "returning"
    SCHEMA = "schema"
    SOFT_DELETE_FIELD = "soft_delete_field"
    SOFT_DELETE_HARD_CASCADE = "soft_delete_hard_cascade"
    SWAPPABLE = "swappable"
    TABLE = "table"
    TABLE_DESCRIPTION = "table_description"
    TABLE_IS_EXPLICIT = "table_is_explicit"
    TABLE_OPTIONS = "table_options"
    TENANT_FIELD = "tenant_field"
    TENANT_SCHEMA = "tenant_schema"
    TRACK_DIRTY_FIELDS = "track_dirty_fields"
    TRIGGERS = "triggers"
    OPTIMISTIC_LOCK_FIELD = "optimistic_lock_field"
    VIEWS = "views"
    MATERIALIZED_VIEWS = "materialized_views"
    DICTIONARIES = "dictionaries"
    FUNCTIONS = "functions"
    SEQUENCES = "sequences"
    POLICIES = "policies"
    GRANTS = "grants"
    ROW_LEVEL_SECURITY = "row_level_security"


class DeletionAction(StrEnum):
    """What a delete does to one row it reaches - see ``DeletionPlan``."""

    #: Removed by a ``DELETE`` hare issues.
    DELETE = "delete"
    #: Removed by the database's own ``ON DELETE CASCADE`` of a row hare deletes.
    DELETE_BY_DATABASE = "delete_by_database"
    #: Marked deleted through ``Meta.soft_delete_field``.
    SOFT_DELETE = "soft_delete"
    #: Already soft-deleted - the soft-delete cascade goes on below it without writing it.
    UNCHANGED = "unchanged"
    #: Deleted one by one through its model's own overridden ``delete()``, which runs its own
    #: cascade.
    OWN_DELETE = "own_delete"
    #: Deleted through ``QuerySet.delete()`` of its model, which deletes the other way (soft
    #: instead of hard, or hard instead of soft) with its own cascade.
    OWN_DISPATCH = "own_dispatch"


class DefaultAssignmentType(StrEnum):
    """How the native constructor of a model's instances gives a field its default."""

    #: The same immutable value on every instance.
    CONSTANT = "constant"
    #: The result of the field's callable default, normalized as an assigned value is.
    CALL = "call"
    #: Worked out by ``InstanceInitialization.assign_default()``.
    FIELD = "field"
