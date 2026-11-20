from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING, Any

from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.inspectdb.generation.declarations import ModelGenerationOptions
from hare.inspectdb.generation.inspected_model_builder import InspectedModelBuilder
from hare.inspectdb.generation.inspected_models import InspectedModels
from hare.migrations.drift.shadow.column_field_names import ColumnFieldNames
from hare.migrations.exceptions import IncompatibleStateError
from hare.migrations.operations.schema_objects.object_types.index_object_type import IndexObjectType
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.dialects.base.dialect import Dialect
    from hare.fields.field import Field
    from hare.inspectdb.generation.inspected_model import InspectedModel
    from hare.inspectdb.introspection.table_info import TableInfo
    from hare.migrations.state.state import State


class ObservedTable:
    """Reconstruct the catalog declaration once so drift repairs can use normal migration operations."""

    def __init__(self, table: TableInfo, dialect: Dialect, expected: ModelState, state: State) -> None:
        default_schema = table.schema if table.is_in_default_schema else None
        alias = state.default_connections.get(expected.app)
        targets = {
            model.table: f"{app}.{name}"
            for (app, name), model in state.models.items()
            if model.options.get(ModelOption.SCHEMA, default_schema) == table.schema
            and state.default_connections.get(app) == alias
        }
        self.binding = state.apps.get_binding(f"{expected.app}.{expected.name}")
        field_names = ColumnFieldNames.column_to_field_name(self.binding, expected.fields)
        columns_per_field = Counter(field_names.values())
        options = ModelGenerationOptions(
            dialect.name,
            app_label=expected.app,
            class_name=expected.name,
            connection_dialect=dialect,
            skip_many_to_many_through_tables=False,
            foreign_key_target_overrides=targets,
            target_models={reference: state.apps.get_model(reference) for reference in targets.values()},
            apps=state.apps,
            field_names_by_column={
                column: name for column, name in field_names.items() if columns_per_field[name] == 1
            },
        )
        builder = InspectedModelBuilder(table, options)
        self.inspected: InspectedModel | None
        try:
            self.inspected = InspectedModels.prepare(builder)
        except ConfigurationError:
            self.inspected = None
        self.field_names_by_column = builder.get_index_field_names_by_column()

    def get_field(self, column: str, name: str) -> Field[Any] | None:
        """The field that would recreate a physical column, under its migration attribute name."""
        inspected = self.inspected
        attribute = self.field_names_by_column.get(column)
        if inspected is None or inspected.state is None or attribute is None or attribute in inspected.todo_reasons:
            return None
        field = inspected.state.fields.get(attribute)
        if field is None:
            return None
        if isinstance(field, ForeignKeyFieldInstance) and field.source_fields:
            return field if name == attribute else None
        if attribute == name:
            return field
        declaration = field.snapshot()
        declaration.source_field = column
        return declaration

    def get_named_object(self, option: ModelOption, name: str) -> Any:
        """Find a reconstructed index or constraint that can be named by a migration."""
        if self.inspected is None or self.inspected.state is None:
            return None
        state = self.inspected.state
        if option is ModelOption.INDEXES:
            try:
                return IndexObjectType.find(state, lambda: self.binding, name=name)
            except IncompatibleStateError:
                return None
        return next(
            (item for item in state.get_option_list(option) if getattr(item, "name", None) == name),
            None,
        )
