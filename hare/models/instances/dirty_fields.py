from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

from hare.exceptions import IncompleteInstanceError
from hare.fields.data.json.json_field import JSONField
from hare.models.constants import PLACEHOLDER_FIELD_VALUE_TYPES
from hare.models.instances.field_snapshot import FieldSnapshot
from hare.models.write.rollback_restores import RollbackRestores

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


class DirtyFields:
    """The field values an instance of a model with Meta.track_dirty_fields was loaded or saved with,
    and the fields that differ from them since."""

    @staticmethod
    def capture_field_values(
        obj: Model, field_names: Iterable[str], *, require_loaded: bool = False
    ) -> dict[str, Any]:
        """Copies the loaded fields among ``field_names``, deep-copying only mutable values.

        Args:
            obj: The model obj.
            field_names: The fields whose values are captured.
            require_loaded: Refuse a missing field rather than omit it from the snapshot.
        """
        values = {}
        copy_value = FieldSnapshot.copy_value
        for field_name in field_names:
            try:
                value = getattr(obj, field_name)
            except AttributeError:
                if require_loaded:
                    raise IncompleteInstanceError(
                        f"{type(obj).__name__} is a partial model, field {field_name!r} is not loaded"
                    ) from None
            else:
                values[field_name] = copy_value(value)
        return values

    @staticmethod
    def diff_field_values(
        obj: Model,
        baseline_values: Mapping[str, Any],
        field_names: Iterable[str],
    ) -> dict[str, tuple[Any, Any]]:
        """Returns ``{field: (old, new)}`` for fields whose current value differs from the baseline
        (an unknown old value is reported as ``None``). A field absent from the baseline but
        present on the instance is dirty, including an explicit ``None`` assignment. A JSON
        value that only changed a nested type (``1`` to ``True``) counts as different too.

        Args:
            obj: The model obj.
            baseline_values: The values ``capture_field_values()`` captured.
            field_names: The fields compared.
        """
        changes: dict[str, tuple[Any, Any]] = {}
        fields_map = obj._binding.fields_map
        for field_name in field_names:
            old_value = baseline_values.get(field_name)
            current_value = getattr(obj, field_name, None)
            if field_name not in baseline_values and hasattr(obj, field_name):
                changes[field_name] = (None, current_value)
                continue
            field = fields_map.get(field_name)
            json_field = field if isinstance(field, JSONField) else None
            old_comparison, current_comparison = old_value, current_value
            if field is not None:
                if not isinstance(old_value, PLACEHOLDER_FIELD_VALUE_TYPES):
                    old_comparison = field.get_snapshot_comparison_value(old_value)
                if not isinstance(current_value, PLACEHOLDER_FIELD_VALUE_TYPES):
                    current_comparison = field.get_snapshot_comparison_value(current_value)
            if FieldSnapshot.values_differ(old_comparison, current_comparison, compare_types=json_field is not None):
                changes[field_name] = (old_value, current_value)
        return changes

    @staticmethod
    def snapshot_dirty_fields(obj: Model) -> None:
        """Takes/refreshes the ``.get_dirty_fields()`` baseline - called after hydration from the
        DB and after a successful ``save()``, only when ``Meta.track_dirty_fields`` is set.

        Args:
            obj: The model obj.
        """
        object.__setattr__(obj, "_dirty_snapshot", DirtyFields.capture_field_values(obj, obj._binding.direct_fields))

    @staticmethod
    def sync_dirty_snapshot_fields(
        obj: Model,
        fields: Iterable[str],
        using: DatabaseClient | None = None,
    ) -> None:
        """Updates the ``get_dirty_fields()`` baseline of the written fields only - other unsaved
        changes stay dirty. A field left unloaded loses its old baseline.

        Args:
            obj: The model obj.
            fields: The written field names.
            using: Connection of the write - the previous baseline comes back if its transaction
                rolls back.
        """
        if not obj._binding.track_dirty_fields:
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
                    dirty_snapshot.pop(field, None)
                    continue
                # Copied, not stored as-is - otherwise a later in-place mutation of a JSON dict/
                # list would mutate the baseline right along with the live value.
                dirty_snapshot[field] = copy_value(getattr(obj, field))
