from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.declaration_snapshot import DeclarationSnapshot
from hare.models.class_building.extension_model_options import ExtensionModelOptions
from hare.models.class_building.model_definition_checks import ModelDefinitionChecks
from hare.models.class_building.schema_object_declarations import SchemaObjectDeclarations
from hare.models.enums import ModelOption

if TYPE_CHECKING:
    from hare.models.model import Model


class ModelOptions:
    """Capture and validate class options before any application prepares their fields.

    The private Meta snapshot preserves extension attributes and ordinary attribute lookup.
    Its nested declarations are copied through the same protocol as field declarations;
    changing the user's Meta afterwards cannot change this class declaration.
    """

    @staticmethod
    def snapshot(meta: type[Model.Meta]) -> type[Any]:
        """Copy effective inherited options without invoking callbacks or descriptors."""
        ModelDefinitionChecks.raise_if_unsupported_option_declared(meta)
        ModelDefinitionChecks.raise_if_option_values_mistyped(meta)
        values = ModelOptions.get_values(meta)
        for name in ExtensionModelOptions.namespaces:
            value = values.get(name)
            if isinstance(value, type):
                values[name] = type(
                    value.__name__, (), DeclarationSnapshot.copy_values(ModelOptions.get_values(value), {})
                )
        snapshot: type[Any] = type("Meta", (), DeclarationSnapshot.copy_values(values, {}))
        # Schema options are normalized once at the declaration boundary.
        from hare.ddl.schema_objects.database_function import DatabaseFunction
        from hare.ddl.schema_objects.database_sequence import DatabaseSequence
        from hare.ddl.schema_objects.dictionary import Dictionary
        from hare.ddl.schema_objects.materialized_view import MaterializedView
        from hare.ddl.schema_objects.view import View
        from hare.ddl.security.grant import Grant
        from hare.ddl.security.policy import Policy

        snapshot.views = SchemaObjectDeclarations.get_schema_objects(
            snapshot, ModelOption.VIEWS, View, MaterializedView
        )
        snapshot.materialized_views = SchemaObjectDeclarations.get_schema_objects(
            snapshot, ModelOption.MATERIALIZED_VIEWS, MaterializedView
        )
        snapshot.dictionaries = SchemaObjectDeclarations.get_schema_objects(
            snapshot, ModelOption.DICTIONARIES, Dictionary
        )
        snapshot.functions = SchemaObjectDeclarations.get_schema_objects(
            snapshot, ModelOption.FUNCTIONS, DatabaseFunction
        )
        snapshot.sequences = SchemaObjectDeclarations.get_schema_objects(
            snapshot, ModelOption.SEQUENCES, DatabaseSequence
        )
        snapshot.policies = SchemaObjectDeclarations.get_schema_objects(snapshot, ModelOption.POLICIES, Policy)
        snapshot.grants = SchemaObjectDeclarations.get_schema_objects(snapshot, ModelOption.GRANTS, Grant)
        indexes = getattr(snapshot, ModelOption.INDEXES, ()) or ()
        if indexes and isinstance(indexes, (list, tuple)) and isinstance(indexes[0], str):
            indexes = (indexes,)
        snapshot.indexes = tuple(tuple(index) if isinstance(index, list) else index for index in indexes)
        snapshot.table_options = SchemaObjectDeclarations.get_declared_table_options(snapshot)
        snapshot.row_level_security = SchemaObjectDeclarations.get_row_level_security(snapshot)
        ModelDefinitionChecks.raise_if_relation_names_repeat(snapshot)
        return snapshot

    @staticmethod
    def get_values(meta: type) -> dict[str, Any]:
        """Read the effective configuration namespace without invoking descriptors."""
        return {
            name: value
            for base in reversed(meta.__mro__)
            if base is not object
            for name, value in vars(base).items()
            if not name.startswith("__")
        }
