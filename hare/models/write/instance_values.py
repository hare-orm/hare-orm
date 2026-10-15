from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.models.write.rollback_restores import RollbackRestores
from hare.native.native_modules import NativeModules

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class InstanceValues:
    """The values a write changes on its instances before it knows its outcome. A failed write puts
    them back (``restore()``); a successful one keeps them until its transaction rolls back
    (``keep()``).
    """

    __slots__ = ("old_values",)

    #: The compiled ``rust.native.rows`` - remembers the values of many instances in one call; None
    #: where it isn't built.
    native_rows: ClassVar[Any] = NativeModules.rows

    def __init__(self) -> None:
        #: id() of an instance -> the instance and each changed field's value before the write -
        #: ``ROLLBACK_RESTORE_UNSET`` for a field that wasn't loaded.
        self.old_values: dict[int, tuple[Model, dict[str, Any]]] = {}

    def capture(self, objs: Iterable[Model], field_names: Sequence[str]) -> None:
        """Remembers the current values of fields the write is about to change - a field already
        remembered keeps its first value.

        Args:
            objs: The objs.
            field_names: The fields.
        """
        if not field_names:
            return
        if self.native_rows is not None:
            self.native_rows.capture_attribute_values(self.old_values, objs, list(field_names), ROLLBACK_RESTORE_UNSET)
            return
        old_values = self.old_values
        for instance in objs:
            entry = old_values.get(id(instance))
            if entry is None:
                entry = old_values[id(instance)] = (instance, {})
            values = entry[1]
            for name in field_names:
                if name not in values:
                    values[name] = getattr(instance, name, ROLLBACK_RESTORE_UNSET)

    def bump_optimistic_lock(self, obj: Model, field_name: str) -> Any:
        """Increments the optimistic lock field of an obj, remembering its value.

        Args:
            obj: The obj.
            field_name: The ``Meta.optimistic_lock_field``.

        Returns:
            The value before the bump - the one the row must still have.
        """
        old_version = getattr(obj, field_name)
        self.capture((obj,), (field_name,))
        setattr(obj, field_name, old_version + 1)
        return old_version

    def restore(self, instances: Iterable[Model] | None = None) -> None:
        """Puts the remembered values back - the write failed or matched no row.

        Args:
            instances: The instances whose values go back; every remembered one when None.
        """
        entries = (
            list(self.old_values.values())
            if instances is None
            else [entry for instance in instances if (entry := self.old_values.get(id(instance))) is not None]
        )
        for instance, values in entries:
            for name, old_value in values.items():
                RollbackRestores.restore_field_value(instance, name, old_value)

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
        # Local import: the models package imports this module.

        RollbackRestores.register_rollback_restores(connection, self.get_rollback_restores(instances))

    def get_rollback_restores(self, instances: Iterable[Model] | None = None) -> list[tuple[Model, str, Any]]:
        """The new values ``keep()`` registers putting back - each changed one with its old value.

        Args:
            instances: The instances whose write succeeded; every remembered one when None.

        Returns:
            ``(instance, field name, old value)`` of each.
        """
        entries = (
            list(self.old_values.values())
            if instances is None
            else [entry for instance in instances if (entry := self.old_values.get(id(instance))) is not None]
        )
        return [
            (instance, name, old_value)
            for instance, values in entries
            for name, old_value in values.items()
            if getattr(instance, name, ROLLBACK_RESTORE_UNSET) is not old_value
        ]
