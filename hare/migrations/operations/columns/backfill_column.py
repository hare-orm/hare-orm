from __future__ import annotations

from collections.abc import Callable
from copy import copy
from typing import TYPE_CHECKING, Any

from hare.classes.value_descriptions import ValueDescriptions
from hare.dialects.base.schema.plans.column_batch_backfill import ColumnBatchBackfill
from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.state.state import State

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.models.model_binding import ModelBinding


class BackfillColumn(ModelBoundOperation):
    """Fill an existing column using the dialect's backfill strategy. Irreversible.

    The dialect uses bounded UPDATE batches or one mutation over all matching rows;
    ``batch_size`` does not limit the latter. The expand-contract data step adds
    a nullable column, fills it, then uses ``AlterColumnNotNullSafe`` in separate migrations.
    """

    reversible = False

    def __init__(
        self,
        model_name: str,
        field_name: str,
        value: Any | Callable[[], Any],
        *,
        batch_size: int = 1000,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ConfigurationError(
                f"BackfillColumn.batch_size must be a positive integer, got "
                f"{ValueDescriptions.get_shown_value(batch_size)}"
            )
        self.model_name = model_name
        self.field_name = field_name
        self.value = value
        self.batch_size = batch_size

    def describe(self) -> str:
        return f"Backfill {self.field_name} on {self.model_name}"

    def raise_if_unsupported(
        self, app_label: str, old_state: State, new_state: State, schema_editor: BaseSchemaEditor
    ) -> None:
        binding = self.get_model_binding(new_state, app_label)
        schema_editor.column_backfill.get_backfill_key_sql(
            binding,
            self._get_column_field_name(binding),
        )

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        await self.get_backfill_plan(self.get_model_binding(new_state, app_label), state_editor).execute(state_editor)

    def get_backfill_plan(self, binding: ModelBinding, state_editor: BaseSchemaEditor) -> SchemaChangePlan:
        """Prepare the batch statement and recovery without evaluating the value factory.

        Args:
            binding: The model with the target column and backfill key.
            state_editor: The dialect's schema editor.

        Returns:
            One runtime step holding the prepared SQL and a snapshot of the field.
        """
        model = binding._model
        column_field_name = self._get_column_field_name(binding)
        db_field = binding.fields_db_projection[column_field_name]
        quoted_key_columns = state_editor.column_backfill.get_backfill_key_sql(binding, self.field_name)

        update_query = state_editor.column_backfill.get_backfill_batch_sql(
            state_editor.get_row_change_table_sql(binding),
            state_editor.quote(db_field),
            quoted_key_columns,
            self.batch_size,
        )

        return SchemaChangePlan(
            (
                ColumnBatchBackfill(
                    update_query,
                    model,
                    copy(binding.fields_map[column_field_name]),
                    self.value,
                    column_field_name != self.field_name,
                    state_editor.get_row_recovery(binding),
                ),
            )
        )

    def _get_column_field_name(self, binding: ModelBinding) -> str:
        """Returns the field backing this operation's column - a foreign key's own key field when
        field_name names the relation itself.

        Args:
            binding: The model being backfilled.

        Returns:
            A field name present in the model's column projection.

        Raises:
            ConfigurationError: field_name is unknown or has no single backing column.
        """
        if self.field_name in binding.fields_db_projection:
            return self.field_name
        field = binding.fields_map.get(self.field_name)
        if isinstance(field, ForeignKeyFieldInstance) and len(field.source_fields) <= 1 and field.source_field:
            return field.source_field
        raise ConfigurationError(
            f"BackfillColumn cannot backfill {self.model_name}.{self.field_name} - it is not a "
            f"field with a single database column."
        )

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        raise NotImplementedError("BackfillColumn is not reversible - backfilled data can't be safely undone.")
