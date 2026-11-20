from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.dialects.dialect_registry import DialectRegistry
from hare.migrations.operations.dependencies.operation_dependency import OperationDependency
from hare.migrations.operations.enums import DependencyType
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.fields.field import Field


class NativeDependencies:
    """Native prerequisites of the declarations a migration operation installs.

    Extension detection stays in SchemaRequirements, shared with ordinary schema
    creation. This adapter translates those names and declared enum types into
    graph requirements; the sorter binds their connection namespace separately.
    """

    @staticmethod
    def get_required(
        fields: Mapping[str, Field[Any]], options: Mapping[str, Any] | None = None
    ) -> list[OperationDependency]:
        options = options or {}
        dependencies = []
        if schema := options.get(ModelOption.SCHEMA):
            dependencies.append(OperationDependency.of_schema(schema, DependencyType.CREATED))
        for field in fields.values():
            enum_type = field.requires_enum_type
            if enum_type is not None:
                dependencies.append(
                    OperationDependency.of_enum_type(enum_type.schema, enum_type.name, DependencyType.CREATED)
                )
        extensions = ModelState.get_declared_extensions(fields, options, DialectRegistry.get_dialects())
        dependencies.extend(
            OperationDependency.of_extension(name, DependencyType.CREATED) for name in sorted(extensions)
        )
        return list(dict.fromkeys(dependencies))
