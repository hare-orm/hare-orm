from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from typing import Any

from hare.dialects.base.schema.shadow.shadow_database import ShadowDatabase
from hare.fields.data.containers.container_field import ContainerField
from hare.fields.field import Field
from hare.fields.generated_field import GeneratedField
from hare.migrations.state.model_state import ModelState
from hare.migrations.state.state import State
from hare.models.enums import ModelOption


class ShadowState:
    """The state a shadow's schema is built from - the declared models with every schema they, and the
    ``ENUM`` types of their fields, are in made the shadow's, and their table options as the shadow takes
    them."""

    @staticmethod
    def get(
        model_states: Iterable[tuple[tuple[str, str], ModelState]], shadow: ShadowDatabase, source_state: State
    ) -> State:
        """The state of the models as the shadow builds them.

        Args:
            model_states: The models, by their (app, name).
            shadow: The shadow.
            source_state: Supplies the captured application and connection assignments.

        Returns:
            The state.
        """
        shadow_state = State(
            default_connections=dict(source_state.default_connections), environment=source_state.environment
        )
        for key, model_state in model_states:
            shadow_model_state = model_state.clone()
            options = shadow_model_state.options
            if schema := options.get(ModelOption.SCHEMA):
                options[ModelOption.SCHEMA] = ShadowState.get_schema(schema, shadow)
            if table_options := options.get(ModelOption.TABLE_OPTIONS):
                options[ModelOption.TABLE_OPTIONS] = tuple(
                    shadow.get_shadow_table_options(entry) for entry in table_options
                )
            if materialized_views := options.get(ModelOption.MATERIALIZED_VIEWS):
                options[ModelOption.MATERIALIZED_VIEWS] = tuple(
                    shadow.get_shadow_materialized_view(view) for view in materialized_views
                )
            for field in shadow_model_state.fields.values():
                enum_type = field.requires_enum_type
                if enum_type is not None and enum_type.schema:
                    ShadowState.place_enum_types(field, shadow)
            shadow_state.models[key] = shadow_model_state
        return shadow_state

    @staticmethod
    def get_schema(schema: str, shadow: ShadowDatabase) -> str | None:
        """The schema of the shadow a model or ``ENUM`` type of ``schema`` is built in - None for the shadow
        of the default schema, which the shadow makes itself: a schema made twice fails.

        Args:
            schema: The declared schema.
            shadow: The shadow.
        """
        shadow_schema = shadow.get_shadow_schema_name(schema)
        return None if shadow_schema == shadow.get_shadow_schema_name(None) else shadow_schema

    @staticmethod
    def place_enum_types(field: Field[Any], shadow: ShadowDatabase) -> None:
        """Makes the schema of each ``ENUM`` type a field and the fields it holds name the shadow's.

        Args:
            field: The field, a copy of the shadow's own.
            shadow: The shadow.
        """
        # A type of its own schema - a field's, not one derived from the field's parts.
        enum_type = vars(field).get("requires_enum_type")
        if enum_type is not None and enum_type.schema:
            field.requires_enum_type = dataclasses.replace(
                enum_type, schema=ShadowState.get_schema(enum_type.schema, shadow)
            )
        if isinstance(field, ContainerField):
            for child_field in field.get_child_fields():
                ShadowState.place_enum_types(child_field, shadow)
        elif isinstance(field, GeneratedField):
            ShadowState.place_enum_types(field.output_field, shadow)
