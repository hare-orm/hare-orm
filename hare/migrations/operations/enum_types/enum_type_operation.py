from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeGuard

from hare.core.log import logger
from hare.ddl.schema_objects.enum_type import EnumType
from hare.exceptions import ConfigurationError
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.operations.hare_operation import HareOperation

if TYPE_CHECKING:
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.migrations.state.state import State


class EnumTypeOperation(HareOperation):
    """Base of the operations on a database ``ENUM`` type - named by its schema and name."""

    name: str
    schema: str | None

    def get_required_dependencies(self, app_label: str, state: State) -> list[OperationDependency]:
        """The schema must exist before any operation addresses a named type in it."""
        return [OperationDependency.of_schema(self.schema, DependencyType.CREATED)] if self.schema is not None else []

    @staticmethod
    def get_checked_labels(labels: Any, label: str) -> tuple[str, ...]:
        """The labels of a type - texts, an empty one too, as a database with ENUM types takes it.

        Args:
            labels: The labels.
            label: The argument, as the message names it.

        Returns:
            The labels.

        Raises:
            ConfigurationError: ``labels`` isn't a sequence of texts, or is itself a text or bytes value.
        """
        if (
            isinstance(labels, (str, bytes, bytearray))
            or not isinstance(labels, Sequence)
            or not all(isinstance(value, str) for value in labels)
        ):
            raise ConfigurationError(f"{label} takes a sequence of texts, got {labels!r}")
        return tuple(labels)

    def get_type_label(self) -> str:
        """The type as a migration describes it - with its schema when it has one."""
        return self.name if self.schema is None else f"{self.schema}.{self.name}"

    def get_shared_types(self, app_label: str, state: State) -> tuple[EnumType, ...]:
        """Find other declarations whose physical schema may coincide on this database."""
        alias = state.default_connections.get(app_label)
        return tuple(
            enum_type
            for (type_alias, schema, name), enum_type in state.enum_types.items()
            if type_alias == alias and name == self.name and (schema is None) != (self.schema is None)
        )

    def _make_type(self, labels: tuple[str, ...]) -> EnumType:
        return EnumType(self.name, labels, self.schema)

    def _record_type(self, app_label: str, state: State, labels: tuple[str, ...] | None) -> None:
        key = (state.default_connections.get(app_label), self.schema, self.name)
        if labels is None:
            state.enum_types.pop(key, None)
        else:
            state.enum_types[key] = self._make_type(labels)
        state.record_native_operation(app_label, self)

    @staticmethod
    def _runs_enum_types(
        state_editor: BaseSchemaEditor | None, operation_description: str
    ) -> TypeGuard[BaseSchemaEditor]:
        """Whether an ``ENUM`` type operation runs on the migration's database - not without an editor,
        nor on a database without ``ENUM`` types: a migration file runs on any database, and such a
        database has no field of one.

        Args:
            state_editor: The migration's schema editor.
            operation_description: What the skipped operation does, for the warning.
        """
        if not state_editor:
            return False
        if not state_editor.client.features.supports_enum_types:
            logger.warning(
                "Skipping %s on %s - it has no ENUM types.", operation_description, state_editor.client.dialect
            )
            return False
        return True

    async def _create_type(
        self, app_label: str, state: State, editor: BaseSchemaEditor | None, labels: tuple[str, ...]
    ) -> None:
        if self._runs_enum_types(editor, f"creating the ENUM type {self.name}"):
            await editor.enum_types.create_enum_type(
                self._make_type(labels), shared_with=self.get_shared_types(app_label, state)
            )

    async def _drop_type(
        self, app_label: str, state: State, editor: BaseSchemaEditor | None, labels: tuple[str, ...]
    ) -> None:
        if self._runs_enum_types(editor, f"dropping the ENUM type {self.name}"):
            await editor.enum_types.drop_enum_type(
                self._make_type(labels), shared_with=self.get_shared_types(app_label, state)
            )
