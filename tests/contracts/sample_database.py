from __future__ import annotations

import sys

import pytest

from hare.contrib.outbox import OutboxEvent
from hare.core.hare_context import HareContext
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.fields.field import Field
from hare.migrations.migration import Migration
from hare.migrations.state.state import State
from hare.migrations.writer.migration_writer import MigrationWriter
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP, SAMPLED_FIELD_NAME
from tests.contracts.contract_models import ContractModels
from tests.contracts.field_sample import FieldSample
from tests.contracts.hare_registry import HareRegistry
from tests.utils.database_under_test import DatabaseUnderTest


class SampleDatabase:
    """The tables of a field sample's models on the database under test."""

    @staticmethod
    def has_column(sample: FieldSample, dialect_name: str) -> bool:
        """Whether a sample's field has a column on a dialect.

        Args:
            sample: The sample.
            dialect_name: The dialect.

        Returns:
            False when the dialect refuses the field.
        """
        field = sample.make_fields()[SAMPLED_FIELD_NAME]
        if not isinstance(field, Field):
            return True
        dialect = DialectRegistry.get_dialect(dialect_name)
        try:
            field.check_dialect_supported(dialect)
            field.get_column_type(dialect)
        except UnSupportedError:
            return False
        return True

    @staticmethod
    def get_shipped_model_bases() -> set[type[Model]]:
        """The models hare ships for applications to build on, of the packages the environment imports.

        Returns:
            Every model class defined in hare but ``Model`` itself.
        """
        HareRegistry.get_installed_modules()
        shipped: set[type[Model]] = set()
        pending: list[type[Model]] = [Model]
        while pending:
            model = pending.pop()
            # A model another test built at run time has a module of hare but no name in it.
            if (
                model.__module__.startswith("hare.")
                and model is not Model
                and getattr(sys.modules.get(model.__module__), model.__name__, None) is model
            ):
                shipped.add(model)
            pending.extend(model.__subclasses__())
        return shipped

    @staticmethod
    def get_unsupported_field_names(models: list[type[Model]], dialect_name: str) -> list[str]:
        """The fields of models that have no column on a dialect.

        Args:
            models: The models.
            dialect_name: The dialect.

        Returns:
            Each as ``Model.field``.
        """
        dialect = DialectRegistry.get_dialect(dialect_name)
        unsupported = []
        for model in models:
            for field_name, field in model._meta.fields_map.items():
                if not isinstance(field, Field):
                    continue
                try:
                    field.check_dialect_supported(dialect)
                    field.get_column_type(dialect)
                except UnSupportedError:
                    unsupported.append(f"{model.__name__}.{field_name}")
        return unsupported

    @staticmethod
    def is_capturable(sample: FieldSample) -> bool:
        """Whether a change of a sample's row can be captured - its models are registered while the application
        runs, and a database keeping unique keys, the only one an outbox is bound on, has a column for it.

        Args:
            sample: The sample.

        Returns:
            True also for a sample that can't be built here: its ``requires`` mark deselects it, or the test
            fails building it.
        """
        if not sample.registered_live:
            return False
        try:
            return any(
                dialect.features.supports_unique_constraints and SampleDatabase.has_column(sample, dialect.name)
                for dialect in DialectRegistry.get_dialects()
            )
        except (ImportError, ConfigurationError):
            return True

    @staticmethod
    def skip_unless_stored(sample: FieldSample) -> None:
        """Skips the test when the database under test has no column for the sample, or stores none of its
        values.

        Args:
            sample: The sample.
        """
        SampleDatabase.skip_unless_column(sample)
        dialect_name = DatabaseUnderTest.get_dialect().name
        if sample.value_dialects is not None and dialect_name not in sample.value_dialects:
            pytest.skip(f"{dialect_name} stores none of the values")

    @staticmethod
    def skip_unless_column(sample: FieldSample) -> None:
        """Skips the test when the database under test has no column for the sample, or keeps no foreign key a
        relation sample declares.

        Args:
            sample: The sample.
        """
        dialect = DatabaseUnderTest.get_dialect()
        dialect_name = dialect.name
        if not SampleDatabase.has_column(sample, dialect_name):
            pytest.skip(f"the field has no column on {dialect_name}")
        if sample.related and not dialect.features.supports_foreign_keys:
            pytest.skip(f"{dialect_name} keeps no foreign key - a relation on it is refused when bound")

    @staticmethod
    async def create(
        context: HareContext, sample: FieldSample, outbox: type[OutboxEvent] | None = None
    ) -> list[type[Model]]:
        """Makes the models of a sample and their tables.

        Args:
            context: The contract test's database.
            sample: The sample.
            outbox: The outbox the row's changes are captured into; None captures none.

        Returns:
            The models; the row's is the last.
        """
        models = ContractModels.make_models(sample, outbox)
        context.register_live_models(models, CONTRACT_APP, "models")
        await context.generate_schemas(safe=True)
        if sample.related:
            await models[-2].objects.create(id=1)
        return models

    @staticmethod
    async def create_rows(row_model: type[Model], sample: FieldSample) -> list[Model]:
        """Creates a row for each value of a sample, numbered from 1 - one row of no value when it has none.

        Args:
            row_model: The sample's row model.
            sample: The sample.

        Returns:
            The created rows.
        """
        if not sample.values:
            return [await row_model.objects.create(id=1)]
        return [
            await row_model.objects.create(id=number, **{sample.column_name: value})
            for number, value in enumerate(sample.values, 1)
        ]

    @staticmethod
    def get_written_migration(models: list[type[Model]]) -> tuple[Migration, State]:
        """The first migration of models, written and read back from its source, and their state.

        Args:
            models: The models.

        Returns:
            The migration and the state of the models.
        """
        new_state = State.from_models(models, app_label=CONTRACT_APP)
        operations = list(State().get_operations(new_state, CONTRACT_APP).operations)
        writer = MigrationWriter("0001_initial", CONTRACT_APP, operations)
        migration = Migration.from_source(writer.as_string(), name="0001_initial", app_label=CONTRACT_APP)
        return migration, new_state

    @staticmethod
    async def get_create_sql(context: HareContext, models: list[type[Model]]) -> list[str]:
        """The statements creating the tables of models, as their migration would run them.

        Args:
            context: The contract test's database.
            models: The models.

        Returns:
            The statements, in order.
        """
        migration, _ = SampleDatabase.get_written_migration(models)
        connection = context.get_connection()
        editor = connection.dialect.schema_editor_class(connection, atomic=False, collect_sql=True)
        await migration.apply(State(), schema_editor=editor)
        return list(editor.collected_sql)

    @staticmethod
    async def apply_migration(context: HareContext, models: list[type[Model]]) -> None:
        """Creates the tables of models by their migration, as migrate does.

        Args:
            context: The contract test's database.
            models: The models.
        """
        migration, _ = SampleDatabase.get_written_migration(models)
        connection = context.get_connection()
        editor = connection.dialect.schema_editor_class(connection, atomic=True, collect_sql=False)
        await migration.apply(State(), schema_editor=editor)
