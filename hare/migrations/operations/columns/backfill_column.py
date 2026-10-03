from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.migrations.operations.base.model_bound_operation import ModelBoundOperation
from hare.migrations.state.project.state import State
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class BackfillColumn(ModelBoundOperation):
    """Fills an existing column in bounded batches instead of one UPDATE over the whole table - the
    data step of expand-contract (add a nullable column, backfill it, then
    ``AlterColumnNotNullSafe``), each in its own migration. Irreversible.
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
        if batch_size <= 0:
            raise ConfigurationError(f"BackfillColumn.batch_size must be a positive integer, got {batch_size!r}")
        self.model_name = model_name
        self.field_name = field_name
        self.value = value
        self.batch_size = batch_size

    def describe(self) -> str:
        return f"Backfill {self.field_name} on {self.model_name}"

    async def database_forward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return

        model = self._model(new_state, app_label)
        column_field_name = self._get_column_field_name(model)
        db_field = model._meta.fields_db_projection[column_field_name]
        pk_column = model._meta.db_pk_column
        qualified_table = state_editor._qualify_table_name(model._meta.db_table, model._meta.schema)

        # A callable is evaluated once - every row gets the same value.
        raw_value = self.value() if callable(self.value) else self.value
        if column_field_name != self.field_name and isinstance(raw_value, Model):
            raw_value = raw_value.pk
        value = state_editor.client.dialect.types.get_db_value(
            model._meta.fields_map[column_field_name], raw_value, model
        )

        # Two parameters bound to the same value - a positional placeholder can't be repeated.
        params = [value, value]
        update_query = state_editor.get_backfill_batch_sql(
            qualified_table, state_editor.quote(db_field), state_editor.quote(pk_column), self.batch_size
        )

        if state_editor.collect_sql:
            state_editor.collected_sql.append(
                f"{update_query}  -- params: {params!r}; repeated in batches of {self.batch_size} "
                "rows until no NULLs remain"
            )
            return

        while True:
            rowcount, _ = await state_editor.client.execute(update_query, params)
            if not rowcount:
                break

    def _get_column_field_name(self, model: type[Model]) -> str:
        """Returns the field backing this operation's column - a foreign key's own key field when
        field_name names the relation itself.

        Args:
            model: The model being backfilled.

        Returns:
            A field name present in the model's column projection.

        Raises:
            ConfigurationError: field_name is unknown or has no single backing column.
        """
        if self.field_name in model._meta.fields_db_projection:
            return self.field_name
        field = model._meta.fields_map.get(self.field_name)
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
