from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.models.constants import INSTANCE_RESTORE_FAILURES_MESSAGE, INSTANCE_STATE_ATTRIBUTE_NAMES

if TYPE_CHECKING:
    from hare.models.model import Model


class InstanceSnapshot:
    """Values replaced by a write, in the order they were first captured.

    This is an undo record, not a deep copy of a model. Values retain their identity,
    and an absent attribute differs from a loaded NULL. Repeated writes in the same
    scope retain the first value. Restoration visits fields and instances in reverse
    order and attempts every entry before reporting setter failures.
    """

    __slots__ = ("instance", "values")

    def __init__(self, instance: Model, values: dict[str, Any] | None = None) -> None:
        self.instance = instance
        self.values = {} if values is None else values

    def remember(self, field_name: str, value: Any) -> None:
        """Keep the value preceding the first assignment in this scope."""
        self.values.setdefault(field_name, value)

    @staticmethod
    def capture_persistence(instance: Model, *, include_primary_key: bool = True) -> InstanceSnapshot:
        """Capture state changed when a write publishes its result on an instance.

        The dirty baseline is copied because a partial save edits its mapping in
        place. The primary key is last so undo restores it before persistence flags.
        Read publication can omit this synthetic key entry and capture its selected
        key columns directly, avoiding a second key setter after restoring relations.
        """
        values = {
            name: instance.__dict__.get(name, ROLLBACK_RESTORE_UNSET)
            for name in ("_saved_in_db", "_custom_generated_pk", "_connection_alias", "_dirty_snapshot")
        }
        if isinstance(values["_dirty_snapshot"], dict):
            values["_dirty_snapshot"] = dict(values["_dirty_snapshot"])
        if include_primary_key and instance._meta.has_primary_key:
            values["pk"] = instance.pk
        return InstanceSnapshot(instance, values)

    @staticmethod
    def restore_all(snapshots: Iterable[InstanceSnapshot]) -> None:
        """Undo the supplied capture sequence, preserving every restoration failure.

        A failing custom setter does not prevent other fields or objects from being
        restored. One failure is raised unchanged; multiple failures are grouped.
        """
        errors: list[BaseException] = []
        for snapshot in reversed(tuple(snapshots)):
            for name, value in reversed(tuple(snapshot.values.items())):
                try:
                    InstanceSnapshot.restore_value(snapshot.instance, name, value)
                except BaseException as error:
                    errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup(INSTANCE_RESTORE_FAILURES_MESSAGE, errors)

    @staticmethod
    def restore_value(instance: Model, field_name: str, value: Any) -> None:
        """Restore a field through its setter, including protected and unloaded fields.

        Database defaults remain markers rather than being passed to field setters.
        Soft-delete restoration uses the same guard as an explicit restore operation.
        """
        # These packages also register write snapshots during model construction.
        from hare.fields.database_default import DatabaseDefault
        from hare.models.deletion.soft_deletion import SoftDeletion

        if field_name in INSTANCE_STATE_ATTRIBUTE_NAMES:
            if value is ROLLBACK_RESTORE_UNSET:
                if field_name in instance.__dict__:
                    object.__delattr__(instance, field_name)
            else:
                object.__setattr__(instance, field_name, value)
        elif field_name == instance._meta.soft_delete_field:
            if value is ROLLBACK_RESTORE_UNSET and not hasattr(instance, field_name):
                return
            SoftDeletion.restore_soft_delete_field(
                instance,
                value is not ROLLBACK_RESTORE_UNSET,
                None if value is ROLLBACK_RESTORE_UNSET else value,
            )
        elif value is ROLLBACK_RESTORE_UNSET:
            if hasattr(instance, field_name):
                object.__delattr__(instance, field_name)
        elif isinstance(value, DatabaseDefault):
            instance.__dict__[field_name] = value
        else:
            setattr(instance, field_name, value)
