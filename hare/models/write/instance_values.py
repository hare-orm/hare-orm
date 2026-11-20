from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import QueryError
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.fields.database_default import DatabaseDefault
from hare.models.write.instance_snapshot import InstanceSnapshot
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.rules.constants import UNREAD_VERSION_MESSAGE
from hare.native.native_modules import NativeModules

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class InstanceValues:
    """The values a write changes on its instances before it knows its outcome. A failed write puts
    them back (``restore()``); a successful one keeps them until its transaction rolls back
    (``keep()``).
    """

    __slots__ = ("old_values",)

    #: The compiled ``rust.native.rows`` - remembers the values of many instances in one call.
    native_rows: ClassVar[Any] = NativeModules.rows

    def __init__(self) -> None:
        #: id() of an instance -> the instance and each changed field's value before the write -
        #: ``ROLLBACK_RESTORE_UNSET`` for a field that wasn't loaded.
        self.old_values: dict[int, tuple[Model, dict[str, Any]]] = {}

    def capture(self, objs: Iterable[Model], field_names: list[str] | tuple[str, ...]) -> None:
        """Remembers the current values of fields the write is about to change - a field already
        remembered keeps its first value.

        Args:
            objs: The objs.
            field_names: The fields.
        """
        if not field_names:
            return
        self.native_rows.capture_attribute_values(self.old_values, objs, field_names, ROLLBACK_RESTORE_UNSET)

    def capture_preparation(self, objs: Sequence[Model]) -> None:
        """Remember pending defaults and tenant assignments before preparing objects of one model.

        The common attribute capture handles the whole batch. Relation caches precede their
        columns so reverse restoration returns the columns first, then the cached objects.
        Pending mappings are copied without copying their factories; a missing instance mapping
        remains missing on restoration.
        """
        if not objs:
            return
        meta = objs[0]._binding
        names = {name for obj in objs for name in obj._await_when_save}
        if meta.tenant_field is not None:
            names.add(meta.tenant_field)
        if not names:
            return
        cache_names = {
            f"_{reference.model_field_name}"
            for name in names
            if (reference := meta.fields_map[name].reference) is not None
        }
        self.capture(objs, ("_await_when_save", *sorted(cache_names), *sorted(names)))
        for obj in objs:
            pending = obj.__dict__.get("_await_when_save", ROLLBACK_RESTORE_UNSET)
            self.old_values[id(obj)][1]["_await_when_save"] = (
                ROLLBACK_RESTORE_UNSET if pending is ROLLBACK_RESTORE_UNSET else dict(pending)
            )

    def bump_optimistic_lock(self, obj: Model, field_name: str, connection: DatabaseClient | None = None) -> Any:
        """Increments the optimistic lock field of an obj, remembering its value.

        Args:
            obj: The obj.
            field_name: The ``Meta.optimistic_lock_field``.
            connection: Register undo before publication when the SQL already completed.

        Returns:
            The value before the bump - the one the row must still have.
        """
        old_version = getattr(obj, field_name)
        try:
            new_version = old_version + 1
        except TypeError:
            if isinstance(old_version, DatabaseDefault):
                raise QueryError(UNREAD_VERSION_MESSAGE.format(model=type(obj).__name__, field=field_name)) from None
            raise
        self.capture((obj,), (field_name,))
        if connection is not None:
            RollbackRestores.register_rollback_restore(obj, connection, field_name, old_version)
        setattr(obj, field_name, new_version)
        return old_version

    def restore(self, instances: Iterable[Model] | None = None) -> None:
        """Restore fields after a failed write or one that matched no row."""
        InstanceSnapshot.restore_all(self.get_snapshots(instances))

    def keep(self, connection: DatabaseClient, instances: Iterable[Model] | None = None) -> None:
        """Keeps the new values - registering putting each changed one back should the
        transaction the write ran in roll back.

        Args:
            connection: The connection the write ran on.
            instances: The instances whose write succeeded; every remembered one when None.
        """
        if not connection.is_transaction_client:
            # Outside a transaction a write can't be rolled back.
            return
        RollbackRestores.register_snapshots(connection, self.get_snapshots(instances, changed_only=True))

    def get_snapshots(
        self, instances: Iterable[Model] | None = None, *, changed_only: bool = False
    ) -> list[InstanceSnapshot]:
        """The old values already captured for a write.

        Args:
            instances: The instances to restore, or every remembered instance when None.
            changed_only: Keep only attributes the write changed, for a later transaction rollback.

        Returns:
            One snapshot per instance in capture order.
        """
        entries = (
            list(self.old_values.values())
            if instances is None
            else [entry for instance in instances if (entry := self.old_values.get(id(instance))) is not None]
        )
        if not changed_only:
            return [InstanceSnapshot(instance, values) for instance, values in entries]
        return [
            InstanceSnapshot(
                instance,
                {
                    name: old_value
                    for name, old_value in values.items()
                    if getattr(instance, name, ROLLBACK_RESTORE_UNSET) is not old_value
                },
            )
            for instance, values in entries
        ]
