from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from hare.ddl.constraints.check_constraint import CheckConstraint
from hare.ddl.enums import GeneratedNamePrefix
from hare.ddl.generated_names import GeneratedNames
from hare.exceptions import ConfigurationError, IntegrityError
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.constants import DIRECT_RELATION_FIELDS
from hare.migrations.operations.model_bound_operation import ModelBoundOperation
from hare.migrations.state.state import State
from hare.models import Model
from hare.query.expressions import Q

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor


class AlterColumnNotNullSafe(ModelBoundOperation):
    """Alters a column to NOT NULL after checking no row holds NULL there - raises
    ``ConfigurationError`` suggesting ``BackfillColumn`` instead of the database's violation. The
    DDL is ``AlterField``'s.

    On Postgres in a migration with ``atomic = False`` the table is never scanned while it is locked:
    a ``CHECK (col IS NOT NULL)`` is added ``NOT VALID``, validated with a lock that doesn't block
    writes, the column set NOT NULL - Postgres reads it off the validated check - and the check
    dropped. Inside an atomic migration the NULL check and the ALTER run under ``LOCK TABLE ... IN
    SHARE ROW EXCLUSIVE MODE``, so no NULL slips in between - writes wait while the table is
    scanned. SQLite rebuilds the table after the NULL check.
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
        if not state_editor.client.is_transaction_client:
            return
        qualified_table = state_editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        lock_sql = state_editor.client.dialect.schema_editor_class.table_locks_class.get_lock_table_sql(
            qualified_table
        )
        if lock_sql is not None:
            await state_editor.client.execute(lock_sql)

    async def _raise_if_nulls_remain(self, model: type[Model], state_editor: BaseSchemaEditor) -> None:
        db_field = model._meta.fields_db_projection[self.field_name]
        qualified_table = state_editor.qualify_table_name(model._meta.db_table, model._meta.schema)
        rows = await state_editor.client.execute_dicts(
            state_editor.column_backfill.get_null_count_sql(qualified_table, state_editor.quote(db_field))
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
        if (
            not state_editor.client.is_transaction_client
            and state_editor.client.features.supports_not_valid_constraints
        ):
            await self._set_not_null_through_check_constraint(old_model, new_model, state_editor)
            return
        if not state_editor.collect_sql:
            await self._lock_table_against_concurrent_writes(old_model, state_editor)
            await self._raise_if_nulls_remain(old_model, state_editor)
        await state_editor.alter_field(old_model, new_model, self.field_name)

    async def _set_not_null_through_check_constraint(
        self, old_model: type[Model], new_model: type[Model], state_editor: BaseSchemaEditor
    ) -> None:
        """Sets NOT NULL outside a transaction without scanning the table while it is locked: a
        ``CHECK (column IS NOT NULL)`` is added unvalidated, validated with a lock that doesn't block
        writes, NOT NULL is set - the database reads it off the validated check instead of the rows -
        and the check is dropped.

        Raises:
            ConfigurationError: A row still holds NULL there - the check is dropped again.
        """
        db_field = old_model._meta.fields_db_projection[self.field_name]
        check = CheckConstraint(
            check=Q(**{f"{self.field_name}__isnull": False}),
            name=GeneratedNames.get_index_name(GeneratedNamePrefix.NOT_NULL_CHECK, old_model, [db_field]),
        )
        await state_editor.constraint_statements.add_check_constraint_not_valid(old_model, check)
        try:
            await state_editor.constraint_statements.validate_constraint(old_model, check.name)
        except IntegrityError:
            await state_editor.remove_constraint(old_model, check)
            await self._raise_if_nulls_remain(old_model, state_editor)
            raise
        await state_editor.alter_field(old_model, new_model, self.field_name)
        await state_editor.remove_constraint(new_model, check)

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
