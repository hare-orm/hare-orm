from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.rules.enums import WriteType
from hare.time import Timezone

if TYPE_CHECKING:
    import datetime

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


class SoftDeletion:
    """The soft delete of an instance of a model with Meta.soft_delete_field: the field written instead
    of the row deleted, restore() writing it back, and the checks that a write may touch a soft-
    deleted row."""

    @staticmethod
    async def is_already_soft_deleted(obj: Model, connection: DatabaseClient | None) -> bool:
        """Check the database before skipping a delete as already done.

        A locally stored deletion time may belong to a row that has since been restored.

        Args:
            obj: The model obj.
            connection: The connection of the delete or its preview.
        """
        soft_delete_field = obj._binding.soft_delete_field
        if not soft_delete_field:
            return False
        if getattr(obj, soft_delete_field, ROLLBACK_RESTORE_UNSET) is None:
            return False
        return obj.pk in await RelatedRows.get_soft_deleted_pks(
            obj._binding, [obj.pk], connection, source_model=type(obj)
        )

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
        from hare.models.instances.instance_saving import InstanceSaving
        from hare.models.write.instance_writer import InstanceWriter

        soft_delete_field: str = obj._binding.soft_delete_field  # type: ignore[assignment]
        previous = SoftDeletion._replace_soft_delete_value(obj, soft_delete_field, value)
        writer = InstanceWriter(obj._binding, connection)
        optimistic_lock_field = obj._binding.optimistic_lock_field
        old_version = getattr(obj, optimistic_lock_field) if optimistic_lock_field else None
        try:
            outcome = await writer.execute_update(
                obj,
                update_fields=[soft_delete_field],
                apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                only_live_row=only_live_row,
                write_type=WriteType.SOFT_DELETE,
            )
        except BaseException:
            SoftDeletion.restore_soft_delete_field(obj, previous)
            raise
        if not outcome.may_have_written:
            SoftDeletion.restore_soft_delete_field(obj, previous)
            if only_live_row:
                return False
            await InstanceSaving.raise_for_unmatched_update(
                obj, connection, old_version, f"Can't {action} object that doesn't exist. PK: {obj.pk}"
            )
        RollbackRestores.register_rollback_restore(obj, connection, soft_delete_field, previous)
        DirtyFields.sync_dirty_snapshot_fields(
            obj, writer.get_update_rules([soft_delete_field], WriteType.SOFT_DELETE).get_updated_names(), connection
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
        # row live while the cascade's writes commit, its related rows cascaded as if the delete
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
        soft_delete_field: str = obj._binding.soft_delete_field  # type: ignore[assignment]
        previous = SoftDeletion._replace_soft_delete_value(obj, soft_delete_field, value)
        RollbackRestores.register_rollback_restore(obj, connection, soft_delete_field, previous)
        DirtyFields.sync_dirty_snapshot_fields(obj, [soft_delete_field], connection)

    @staticmethod
    def _replace_soft_delete_value(obj: Model, field_name: str, value: datetime.datetime | None) -> Any:
        """Set the guarded field and capture its previous value, including an unloaded field."""
        previous = getattr(obj, field_name, ROLLBACK_RESTORE_UNSET)
        SoftDeletion.set_soft_delete_field(obj, value)
        return previous

    @staticmethod
    def set_soft_delete_field(obj: Model, value: datetime.datetime | None) -> None:
        _setattr = object.__setattr__
        _setattr(obj, "_allow_soft_delete_write", True)
        try:
            setattr(obj, obj._binding.soft_delete_field, value)  # type: ignore[arg-type]
        finally:
            _setattr(obj, "_allow_soft_delete_write", False)

    @staticmethod
    def restore_soft_delete_field(obj: Model, previous: Any) -> None:
        """Restore the previous soft-delete value or unload the field again."""
        if previous is ROLLBACK_RESTORE_UNSET:
            object.__delattr__(obj, obj._binding.soft_delete_field)  # type: ignore[arg-type]
        else:
            SoftDeletion.set_soft_delete_field(obj, previous)

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
            obj._binding.soft_delete_field is not None
            and key == obj._binding.soft_delete_field
            and getattr(obj, "_saved_in_db", False)
            and not obj._allow_soft_delete_write
        ):
            raise QueryError(
                f"Cannot set {key!r} directly on a persisted {type(obj).__name__} - "
                "use .delete()/.restore() instead of mutating the soft-delete field"
            )
