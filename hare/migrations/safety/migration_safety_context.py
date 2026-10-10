from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.migrations.safety.migration_risk import MigrationRisk

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.fields.field import Field
    from hare.migrations.migration import Migration
    from hare.migrations.operations import Operation
    from hare.migrations.safety.enums import MigrationRiskCode
    from hare.migrations.state.state import State
    from hare.models import Model


@dataclass(frozen=True)
class MigrationSafetyContext:
    """What a migration safety rule knows about the operation it checks.

    Attributes:
        migration: The migration.
        operation: The operation checked.
        state_before: The migration state before the operation.
        state_after: The migration state after it.
        dialect: The dialect of the database the migration runs on.
        features: What that database supports - its server version's features when connected.
        large_table_rows: The rows from which a table counts as large.
        table_row_counts: The rows of each table the migration changes, by lowercase model name, up
            to ``large_table_rows`` (None where the database can't tell); None when the database
            wasn't read (``makemigrations``).
        created_model_names: The lowercase names of the models the migration creates - their
            tables are new and empty.
        in_separate_database_and_state: The operation is one of a ``SeparateDatabaseAndState``'s
            database operations - its author keeps the running code and the database in step.
    """

    migration: Migration
    operation: Operation
    state_before: State
    state_after: State
    dialect: Dialect
    features: Features
    large_table_rows: int
    table_row_counts: Mapping[str, int | None] | None
    created_model_names: frozenset[str]
    in_separate_database_and_state: bool = False

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
        """Whether the model's table may hold ``large_table_rows`` rows or more - never for a table the
        migration creates; always when the database wasn't read or can't tell.

        Args:
            model_name: The model's name in the migration's app.

        Returns:
            Whether an operation locking the table may keep it locked for long.
        """
        lowercase_name = model_name.lower()
        if lowercase_name in self.created_model_names:
            return False
        if self.table_row_counts is None:
            return True
        row_count = self.table_row_counts.get(lowercase_name, 0)
        return row_count is None or row_count >= self.large_table_rows

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
