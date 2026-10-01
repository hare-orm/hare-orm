from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from hare.dialects.base.client.transaction_client import TransactionClient
from hare.exceptions import ConfigurationError
from hare.migrations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.base.model_bound_operation import ModelBoundOperation
from hare.migrations.state.project.state import State
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.editor import BaseSchemaEditor


class AlterColumnNotNullSafe(ModelBoundOperation):
    """Alters a column to NOT NULL after checking no row holds NULL there - raises
    ``ConfigurationError`` suggesting ``BackfillColumn`` instead of the database's violation. The
    DDL is ``AlterField``'s.

    On Postgres inside an atomic migration the check and the ALTER run under ``LOCK TABLE ... IN
    SHARE ROW EXCLUSIVE MODE``, so no NULL slips in between. Without a transaction
    (``atomic=False``, a schema editor on a plain connection) and on SQLite there is no such lock -
    run it in a maintenance window, or add a ``CHECK (col IS NOT NULL)`` first.
    """

    def __init__(self, model_name: str, field_name: str) -> None:
        self.model_name = model_name
        self.field_name = field_name

    def describe(self) -> str:
        return f"Safely alter {self.field_name} on {self.model_name} to NOT NULL"

    def state_forward(self, app_label: str, state: State) -> None:
        model_state = self.get_model_state(state, app_label, self.model_name)

        old_field = model_state.fields.get(self.field_name)
        if old_field is None:
            raise IncompatibleStateError(
                f"Field {self.field_name} is not present on model {app_label}.{self.model_name}"
            )

        new_field = deepcopy(old_field)
        new_field.null = False
        model_state.fields[self.field_name] = new_field

        models_to_reload = {(app_label, self.model_name)}
        if isinstance(old_field, DIRECT_RELATION_FIELDS):
            models_to_reload.add(state.apps.split_reference(old_field.model_name))

        state.reload_models(models_to_reload)

    async def _lock_table_against_concurrent_writes(self, model: type[Model], state_editor: BaseSchemaEditor) -> None:
        """Locks the table against writes for the rest of the transaction (``get_lock_table_sql()``, a
        no-op where the dialect has none) - only when a transaction is actually open.
        """
        if not isinstance(state_editor.client, TransactionClient):
            return
        qualified_table = state_editor._qualify_table_name(model._meta.db_table, model._meta.schema)
        lock_sql = state_editor.client.dialect.get_lock_table_sql(qualified_table)
        if lock_sql is not None:
            await state_editor.client.execute(lock_sql)

    async def _raise_if_nulls_remain(self, model: type[Model], state_editor: BaseSchemaEditor) -> None:
        db_field = model._meta.fields_db_projection[self.field_name]
        qualified_table = state_editor._qualify_table_name(model._meta.db_table, model._meta.schema)
        rows = await state_editor.client.execute_dicts(
            state_editor.get_null_count_sql(qualified_table, state_editor.quote(db_field))
        )
        null_count = rows[0]["null_count"]
        if null_count:
            raise ConfigurationError(
                f"Cannot set {model.__name__}.{self.field_name} NOT NULL - {null_count} row(s) still "
                f"have NULL there. Run BackfillColumn for {model.__name__}.{self.field_name} first."
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
        old_model = self._model(old_state, app_label)
        new_model = self._model(new_state, app_label)
        if not state_editor.collect_sql:
            await self._lock_table_against_concurrent_writes(old_model, state_editor)
            await self._raise_if_nulls_remain(old_model, state_editor)
        await state_editor.alter_field(old_model, new_model, self.field_name)

    async def database_backward(
        self,
        app_label: str,
        old_state: State,
        new_state: State,
        state_editor: BaseSchemaEditor | None = None,
    ) -> None:
        if not state_editor:
            return
        old_model = self._model(old_state, app_label)
        new_model = self._model(new_state, app_label)
        await state_editor.alter_field(old_model, new_model, self.field_name)
