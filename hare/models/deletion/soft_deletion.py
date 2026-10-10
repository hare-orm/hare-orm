from __future__ import annotations

from typing import TYPE_CHECKING, cast

from hare.exceptions import QueryError
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.instances.instance_saving import InstanceSaving
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.write_fields import WriteFields
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    import datetime

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


class SoftDeletion:
    """The soft delete of an instance of a model with Meta.soft_delete_field: the field written instead
    of the row deleted, restore() writing it back, and the checks that a write may touch a soft-
    deleted row."""

    @staticmethod
    async def is_already_soft_deleted(obj: Model, connection: DatabaseClient | None) -> bool:
        """Whether this obj's row is already soft-deleted - read from the database when a
        ``.only()``/``.defer()`` query left ``Meta.soft_delete_field`` unloaded.

        Args:
            obj: The model obj.
            connection: Connection to read through when the field isn't loaded.
        """
        soft_delete_field = obj._meta.soft_delete_field
        if not soft_delete_field:
            return False
        if hasattr(obj, soft_delete_field):
            return getattr(obj, soft_delete_field) is not None
        return obj.pk in await RelatedRows.get_soft_deleted_pks(type(obj), [obj.pk], connection)

    @staticmethod
    async def write_soft_delete_field(
        obj: Model,
        connection: DatabaseClient,
        value: datetime.datetime | None,
        action: str,
        apply_active_tenant_scope_guard: bool = True,
        only_live_row: bool = False,
    ) -> bool:
        """Writes ``Meta.soft_delete_field`` of this obj's row - the deletion time for
        ``delete()``, None for ``restore()``. The in-memory value is put back when the write fails,
        matches no row or its transaction rolls back.

        Args:
            obj: The model obj.
            connection: The connection.
            value: The value written.
            action: ``delete`` or ``restore``, for the error message.
            apply_active_tenant_scope_guard: See ``SoftDeletion.persist_soft_delete()``.
            only_live_row: Write the row only while it isn't soft-deleted, and report a row not
                matched instead of raising.

        Returns:
            Whether the row was written.

        Raises:
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row was modified
                concurrently.
            IntegrityError: The row doesn't exist.
        """
        soft_delete_field = cast("str", obj._meta.soft_delete_field)
        had_soft_delete_value = hasattr(obj, soft_delete_field)
        old_soft_delete_value = getattr(obj, soft_delete_field) if had_soft_delete_value else None
        SoftDeletion.set_soft_delete_field(obj, value)
        writer = InstanceWriter(obj.__class__, connection)
        optimistic_lock_field = obj._meta.optimistic_lock_field
        old_version = getattr(obj, optimistic_lock_field) if optimistic_lock_field else None
        try:
            rows = await writer.execute_update(
                obj,
                update_fields=[soft_delete_field],
                apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                only_live_row=only_live_row,
            )
        except BaseException:
            SoftDeletion.restore_soft_delete_field(obj, had_soft_delete_value, old_soft_delete_value)
            raise
        if rows == 0:
            SoftDeletion.restore_soft_delete_field(obj, had_soft_delete_value, old_soft_delete_value)
            if only_live_row:
                return False
            InstanceSaving.raise_for_unmatched_update(
                obj, old_version, f"Can't {action} object that doesn't exist. PK: {obj.pk}"
            )
        RollbackRestores.register_rollback_restore(
            obj,
            connection,
            soft_delete_field,
            old_soft_delete_value if had_soft_delete_value else ROLLBACK_RESTORE_UNSET,
        )
        DirtyFields.sync_dirty_snapshot_fields(
            obj, WriteFields.of(obj.__class__).get_written_with({soft_delete_field}), connection
        )
        return True

    @staticmethod
    async def persist_soft_delete(
        obj: Model,
        connection: DatabaseClient,
        apply_active_tenant_scope_guard: bool = True,
        deleted_at: datetime.datetime | None = None,
    ) -> None:
        """Marks this already-cascaded obj's own row deleted with an ``UPDATE``.

        Args:
            obj: The model obj.
            connection: The connection the delete runs on.
            apply_active_tenant_scope_guard: False for a row a cascade found through a real FK match
                - its tenant may differ from the active one.
            deleted_at: The deletion time to write - now by default.

        Raises:
            IntegrityError: The row no longer exists.
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row was modified
                concurrently.
        """
        # The UPDATE matching no row raises - a concurrent version bump would otherwise leave the
        # row live while the cascade's mutations commit, its related rows cascaded as if the delete
        # had gone through.
        await SoftDeletion.write_soft_delete_field(
            obj,
            connection,
            deleted_at or Timezone.now(),
            "delete",
            apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
        )

    @staticmethod
    def adopt_soft_delete_value(obj: Model, connection: DatabaseClient, value: datetime.datetime) -> None:
        """Takes the soft-delete value another write already stored in this obj's row.

        Args:
            obj: The model obj.
            connection: Connection of the running transaction.
            value: The row's soft-delete value.
        """
        soft_delete_field = cast("str", obj._meta.soft_delete_field)
        had_soft_delete_value = hasattr(obj, soft_delete_field)
        old_soft_delete_value = getattr(obj, soft_delete_field) if had_soft_delete_value else None
        SoftDeletion.set_soft_delete_field(obj, value)
        RollbackRestores.register_rollback_restore(
            obj,
            connection,
            soft_delete_field,
            old_soft_delete_value if had_soft_delete_value else ROLLBACK_RESTORE_UNSET,
        )
        DirtyFields.sync_dirty_snapshot_fields(obj, [soft_delete_field], connection)

    @staticmethod
    def set_soft_delete_field(obj: Model, value: datetime.datetime | None) -> None:
        _setattr = object.__setattr__
        _setattr(obj, "_allow_soft_delete_write", True)
        try:
            setattr(obj, cast("str", obj._meta.soft_delete_field), value)
        finally:
            _setattr(obj, "_allow_soft_delete_write", False)

    @staticmethod
    def restore_soft_delete_field(obj: Model, had_value: bool, old_value: datetime.datetime | None) -> None:
        """Puts ``Meta.soft_delete_field`` back after a failed delete()/restore() - to its previous
        value, or to not loaded at all.

        Args:
            obj: The model obj.
            had_value: Whether the field was loaded before.
            old_value: Its value before.
        """
        if had_value:
            SoftDeletion.set_soft_delete_field(obj, old_value)
        else:
            object.__delattr__(obj, cast("str", obj._meta.soft_delete_field))

    @staticmethod
    def check_soft_delete_write_allowed(obj: Model, key: str) -> None:
        """
        Rejects a direct write to ``Meta.soft_delete_field`` on a persisted obj.

        Args:
            obj: The model obj.
            key: The field name about to be assigned.

        Raises:
            QueryError: If ``key`` is the soft-delete field of an already persisted obj.
        """
        if (
            obj._meta.soft_delete_field is not None
            and key == obj._meta.soft_delete_field
            and getattr(obj, "_saved_in_db", False)
            and not obj._allow_soft_delete_write
        ):
            raise QueryError(
                f"Cannot set '{key}' directly on a persisted {type(obj).__name__} - "
                "use .delete()/.restore() instead of mutating the soft-delete field"
            )
