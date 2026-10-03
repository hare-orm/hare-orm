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
    APP = "app"
    CONSTRAINTS = "constraints"
    EXTENSIONS = "extensions"
    INDEXES = "indexes"
    MANAGED = "managed"
    MANAGER = "manager"
    ORDERING = "ordering"
    PK_ATTR = "pk_attr"
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
    TRACK_DIRTY_FIELDS = "track_dirty_fields"
    TRIGGERS = "triggers"
    OPTIMISTIC_LOCK_FIELD = "optimistic_lock_field"


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
