from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

from hare.migrations.operations.fields.add_field import AddField
from hare.migrations.operations.fields.alter_field import AlterField
from hare.migrations.safety.migration_risk import MigrationRisk

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.dialects.base.schema.plans.schema_change_plan import SchemaChangePlan
    from hare.fields.field import Field
    from hare.migrations.migration import Migration
    from hare.migrations.operations import Operation
    from hare.migrations.safety.enums import MigrationRiskCode
    from hare.migrations.state.prepared_operation_states import PreparedOperationStates
    from hare.migrations.state.state import State
    from hare.models import Model


@dataclass(frozen=True)
class MigrationSafetyContext:
    """What a migration safety rule knows about the operation it checks.

    Attributes:
        migration: The migration.
        operation: The operation checked.
        operation_states: The states between the migration's operations - ``state_before`` and
            ``state_after`` are read from the prepared snapshots.
        position: The operation's position among them.
        dialect: The dialect of the database the migration runs on.
        features: What that database supports - its server version's features when connected.
        schema_editor: Collection-only editor for the configured client; prepares the actual
            field plans without executing SQL or runtime defaults.
        large_table_rows: The rows from which a table counts as large.
        table_row_counts: Independent row estimates at this operation's boundary, by lowercase
            model name, up to ``large_table_rows``. None means unknown, including offline checks;
            zero means a known empty table. Physical renames retain the estimate.
        in_separate_database_and_state: The operation is one of a ``SeparateDatabaseAndState``'s
            database operations - its author keeps the running code and the database in step.
    """

    migration: Migration
    operation: Operation
    operation_states: PreparedOperationStates
    position: int
    dialect: Dialect
    features: Features
    schema_editor: BaseSchemaEditor
    large_table_rows: int
    table_row_counts: Mapping[str, int | None]
    in_separate_database_and_state: bool = False

    @cached_property
    def schema_plan(self) -> SchemaChangePlan | None:
        """The actual field plan, prepared once without catalog IO or default evaluation."""
        operation = self.operation
        if not isinstance(operation, (AddField, AlterField)):
            return None
        old_model = self.get_model_before(operation.model_name)
        new_model = self.get_model_after(operation.model_name)
        if old_model is None or new_model is None:
            return None
        if isinstance(operation, AddField):
            return self.schema_editor.get_add_field_plan(
                old_model, new_model, operation.name, foreign_key_not_valid=operation.not_valid
            )
        return self.schema_editor.get_alter_field_plan(
            old_model, new_model, operation.name, self.state_after.get_models_with_tables()
        )

    @property
    def state_before(self) -> State:
        """The migration state before the operation."""
        return self.operation_states.get(self.position)

    @property
    def state_after(self) -> State:
        """The migration state after the operation."""
        return self.operation_states.get(self.position + 1)

    def get_model_before(self, model_name: str) -> type[Model] | None:
        """The model as it is before the operation, None when the state has no such model.

        Args:
            model_name: The model's name in the migration's app.

        Returns:
            The rendered model.
        """
        return self._get_model(self.state_before, model_name)

    def get_model_after(self, model_name: str) -> type[Model] | None:
        """The model as it is after the operation, None when the state has no such model.

        Args:
            model_name: The model's name in the migration's app.

        Returns:
            The rendered model.
        """
        return self._get_model(self.state_after, model_name)

    def get_field_before(self, model_name: str, field_name: str) -> Field[Any] | None:
        """A field of a model as it is before the operation.

        Args:
            model_name: The model.
            field_name: The field.

        Returns:
            The field, None when the model or the field isn't there yet.
        """
        model = self.get_model_before(model_name)
        return model._meta.fields_map.get(field_name) if model is not None else None

    def get_field_after(self, model_name: str, field_name: str) -> Field[Any] | None:
        """A field of a model as it is after the operation.

        Args:
            model_name: The model.
            field_name: The field.

        Returns:
            The field, None when the model or the field is gone.
        """
        model = self.get_model_after(model_name)
        return model._meta.fields_map.get(field_name) if model is not None else None

    def _get_model(self, state: State, model_name: str) -> type[Model] | None:
        if (self.migration.app_label, model_name) not in state.models:
            return None
        return state.apps.get_model(f"{self.migration.app_label}.{model_name}")

    def may_be_large(self, model_name: str) -> bool:
        """Whether the estimate reaches ``large_table_rows`` or the row count is unknown.

        Physical creation starts at zero; opaque data operations invalidate that evidence.

        Args:
            model_name: The model's name in the migration's app.

        Returns:
            Whether an operation locking the table may keep it locked for long.
        """
        row_count = self.table_row_counts.get(model_name.lower())
        return row_count is None or row_count >= self.large_table_rows

    def may_hold_rows(self, model_name: str) -> bool:
        """Whether the estimate is positive or unknown at this operation's boundary.

        Args:
            model_name: The model's name in the migration's app.

        Returns:
            Whether an operation failing on a table with rows may fail.
        """
        row_count = self.table_row_counts.get(model_name.lower())
        return row_count is None or row_count > 0

    def get_risk(self, code: MigrationRiskCode, message: str, safe_alternative: str) -> MigrationRisk:
        """A risk of the checked operation.

        Args:
            code: The rule's code.
            message: What goes wrong on a database in use.
            safe_alternative: How to make the same change safely.

        Returns:
            The risk, exempted when the migration lists the code in ``safety_exemptions``.
        """
        return MigrationRisk(
            code=code,
            app_label=self.migration.app_label,
            migration_name=self.migration.name,
            operation=self.operation.describe(),
            message=message,
            safe_alternative=safe_alternative,
            exempted=code in self.migration.safety_exemptions,
        )
