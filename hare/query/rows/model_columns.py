from __future__ import annotations

import datetime
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.models.enums import FieldBucket
from hare.query.plans.statement_plans import StatementPlans
from hare.query.rows.hydrate_accelerator import HydrateAccelerator
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.query.rows.hydration_layout import HydrationEntry, HydrationLayout

#: Builds one instance from one row: ``(row, tz_ctx, connection_name) -> instance``, None for
#: the columns of a related model a LEFT JOIN matched no row for.
HydrateFunction = Callable[[Any, "tuple[bool, datetime.tzinfo | None] | None", "str | None"], Any]


@dataclass(slots=True)
class ModelColumns:
    """The columns of one model in the rows of a result: what each holds and where it sits.

    Attributes:
        model: The model.
        entries: The model field of each column.
        keys: Where each column is read from a row - its position, or its name on a driver
            whose rows aren't read by position.
        is_partial: Whether only some of the model's columns are selected.
        path: The relation path to the instance - empty for the queried model itself.
        is_related: Whether the columns are those of a joined relation - all NULL when the
            LEFT JOIN matched no row.
    """

    model: type[Model]
    entries: tuple[HydrationEntry, ...]
    keys: tuple[int | str, ...]
    is_partial: bool
    path: tuple[str, ...] = ()
    is_related: bool = False

    @staticmethod
    def build(
        model: type[Model],
        layout: HydrationLayout,
        names: Sequence[str],
        row_keys: Sequence[int | str],
        *,
        path: tuple[str, ...] = (),
        is_related: bool = False,
    ) -> ModelColumns:
        """The columns of ``model`` among a result's columns.

        Args:
            model: The model.
            layout: How the model's rows are read on the connection.
            names: What each of the model's columns in the result is named after - a column name
                of the model, or a field name of it (a joined relation's columns).
            row_keys: Where each is read from a row.
            path: The relation path to the instance.
            is_related: Whether the columns are those of a joined relation.

        Returns:
            The columns - a column that is neither a column nor a field of the model is left out.
        """
        entries: list[HydrationEntry] = []
        keys: list[int | str] = []
        for name, row_key in zip(names, row_keys, strict=True):
            entry = layout.entry_by_column.get(name)
            if entry is None:
                field_entry = layout.entry_by_field_name.get(name)
                if field_entry is None:
                    continue
                _column, field, bucket, dialect_reader = field_entry
                entry = (name, field, bucket, dialect_reader)
            entries.append(entry)
            keys.append(row_key)
        return ModelColumns(
            model,
            tuple(entries),
            tuple(keys),
            len(entries) < layout.full_column_count,
            path,
            is_related,
        )

    def get_hydrate_function(self) -> HydrateFunction:
        """The function building an instance from a row.

        Returns:
            The function, compiled once per model and columns and kept with the model's plans.
        """
        model = self.model
        key = (self.entries, self.keys, self.is_partial, self.is_related)
        hydrate_functions = StatementPlans.hydrate_functions
        function = hydrate_functions.get_for_model(model, key)
        if function is None:
            function = ModelColumns.compile_hydrate_function(model, *key)
            hydrate_functions[(model, *key)] = function
        return cast("HydrateFunction", function)

    @staticmethod
    def compile_hydrate_function(
        model: type[Model],
        entries: tuple[HydrationEntry, ...],
        keys: tuple[int | str, ...],
        is_partial: bool,
        is_related: bool,
    ) -> HydrateFunction:
        """Compiles the function building ``model``'s instance from a row: one literal attribute
        assignment per column - which the interpreter specializes, unlike ``setattr()`` by name -
        with each column's conversion decided once.

        Args:
            model: The model.
            entries: The field of each column.
            keys: Where each column is read from a row.
            is_partial: Whether only some of the model's columns are selected.
            is_related: Whether all-NULL columns mean no instance.

        Returns:
            The function.
        """
        meta = model._meta
        namespace: dict[str, Any] = {
            "_setattr": object.__setattr__,
            "_model": model,
            "_new": model.__new__,
        }
        lines: list[str] = []
        if is_related:
            all_null = " and ".join(f"row[{key!r}] is None" for key in keys) or "True"
            lines.append(f"    if {all_null}:")
            lines.append("        return None")
        lines.append("    self = _new(_model)")
        lines.append(f"    _setattr(self, '_partial', {is_partial!r})")
        lines.append("    _setattr(self, '_saved_in_db', True)")
        lines.append(f"    _setattr(self, '_custom_generated_pk', {meta.default_custom_generated_pk!r})")
        lines.append("    _setattr(self, '_await_when_save', {})")
        lines.append("    _setattr(self, '_db_connection_name', connection_name)")
        for index, ((model_field, field, bucket, dialect_reader), key) in enumerate(zip(entries, keys, strict=True)):
            value = f"row[{key!r}]"
            if bucket == FieldBucket.DEFAULT:
                field_name = f"_field_{index}"
                namespace[field_name] = field
                lines.append(f"    v = {value}")
                lines.append(f"    if v is not None: v = {field_name}.field_type(v)")
                lines.append(f"    _setattr(self, {model_field!r}, v)")
            elif bucket == FieldBucket.COMPLEX and dialect_reader is not None:
                reader_name = f"_reader_{index}"
                namespace[reader_name] = dialect_reader
                lines.append(f"    _setattr(self, {model_field!r}, {reader_name}({value}))")
            elif bucket == FieldBucket.COMPLEX:
                field_name = f"_field_{index}"
                namespace[field_name] = field
                if type(field) is DatetimeField:
                    # The timezone settings are resolved once per result, not once per row.
                    lines.append(f"    v = {value}")
                    lines.append("    if tz_ctx is not None:")
                    lines.append(f"        v = {field_name}.get_python_value_with_tz(v, tz_ctx[0], tz_ctx[1])")
                    lines.append("    else:")
                    lines.append(f"        v = {field_name}.from_db_value(v)")
                    lines.append(f"    _setattr(self, {model_field!r}, v)")
                else:
                    lines.append(f"    _setattr(self, {model_field!r}, {field_name}.from_db_value({value}))")
            else:
                lines.append(f"    _setattr(self, {model_field!r}, {value})")
        if meta.track_dirty_fields:
            lines.append("    self._snapshot_dirty_fields()")
        lines.append("    return self")
        source = "def _hydrate(row, tz_ctx, connection_name):\n" + "\n".join(lines)
        exec(source, namespace)  # noqa: S102 # nosec B102 - model-derived source, no external input
        return cast("HydrateFunction", namespace["_hydrate"])

    def hydrate_with_accelerator(
        self, rows: Sequence[Any], connection_name: str, types: TypeRegistry
    ) -> list[Any] | None:
        """Builds the instances of every row in one ``rust.native.rows`` call.

        Args:
            rows: The rows, read by position.
            connection_name: The connection the rows were read on.
            types: The connection dialect's type registry.

        Returns:
            The instances (None for a related model's all-NULL columns), or None when the
            columns don't sit next to each other - the compiled function reads them then.
        """
        positions: Any = self.keys
        # Positions only ever grow - they sit next to each other when the last is as far from the
        # first as there are columns.
        if not positions or positions[-1] - positions[0] != len(positions) - 1:
            return None
        model = self.model
        reader = HydrateAccelerator.get_model_reader(
            model, self.entries, self.is_partial, types, Timezone.get_aware_zone_name()
        )
        instances: list[Any] = reader.read(
            rows if isinstance(rows, list) else list(rows),
            connection_name,
            positions[0],
            self.is_related,
        )
        return instances
