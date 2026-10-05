from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from hare.fields.data.json.json_field import JSONField
from hare.models.instances.field_snapshot import FieldSnapshot
from hare.models.write.rollback_restores import RollbackRestores

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterable

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


class DirtyFields:
    """The field values an instance of a model with Meta.track_dirty_fields was loaded or saved with,
    and the fields that differ from them since."""

    @staticmethod
    def capture_field_values(obj: Model, field_names: Iterable[str]) -> dict[str, Any]:
        """Copies the loaded fields among ``field_names``, deep-copying only mutable values.

        Args:
            obj: The model obj.
            field_names: The fields whose values are captured.
        """
        values = {}
        copy_value = FieldSnapshot.copy_value
        for field_name in field_names:
            if hasattr(obj, field_name):
                values[field_name] = copy_value(getattr(obj, field_name))
        return values

    @staticmethod
    def diff_field_values(
        obj: Model,
        baseline_values: Mapping[str, Any],
        field_names: Iterable[str],
    ) -> dict[str, tuple[Any, Any]]:
        """Returns ``{field: (old, new)}`` for fields whose current value differs from the baseline
        (a field missing from the baseline counts as ``None``); a JSON value that only changed a
        nested type (``1`` to ``True``) counts as different too.

        Args:
            obj: The model obj.
            baseline_values: The values ``capture_field_values()`` captured.
            field_names: The fields compared.
        """
        changes: dict[str, tuple[Any, Any]] = {}
        fields_map = obj._meta.fields_map
        for field_name in field_names:
            old_value = baseline_values.get(field_name)
            current_value = getattr(obj, field_name, None)
            if FieldSnapshot.values_differ(
                old_value, current_value, compare_types=isinstance(fields_map.get(field_name), JSONField)
            ):
                changes[field_name] = (old_value, current_value)
        return changes

    @staticmethod
    def snapshot_dirty_fields(obj: Model) -> None:
        """Takes/refreshes the ``.get_dirty_fields()`` baseline - called after hydration from the
        DB and after a successful ``save()``, only when ``Meta.track_dirty_fields`` is set.

        Args:
            obj: The model obj.
        """
        object.__setattr__(obj, "_dirty_snapshot", DirtyFields.capture_field_values(obj, obj._meta.direct_fields))

    @staticmethod
    def sync_dirty_snapshot_fields(
        obj: Model,
        fields: Iterable[str],
        using: DatabaseClient | None = None,
    ) -> None:
        """Updates the ``get_dirty_fields()`` baseline of the written fields only - other unsaved
        changes stay dirty. A field left unloaded is skipped.

        Args:
            obj: The model obj.
            fields: The written field names.
            using: Connection of the write - the previous baseline comes back if its transaction
                rolls back.
        """
        if not obj._meta.track_dirty_fields:
            return
        dirty_snapshot = getattr(obj, "_dirty_snapshot", None)
        if using is not None:
            RollbackRestores.register_rollback_restore(
                obj, using, "_dirty_snapshot", dict(dirty_snapshot) if dirty_snapshot is not None else None
            )
        if dirty_snapshot is not None:
            copy_value = FieldSnapshot.copy_value
            for field in fields:
                if not hasattr(obj, field):
                    continue
                # Copied, not stored as-is - otherwise a later in-place mutation of a JSON dict/
                # list would mutate the baseline right along with the live value.
                dirty_snapshot[field] = copy_value(getattr(obj, field))
