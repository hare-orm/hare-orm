from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.inspectdb import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.drift.constants import DRIFT_COMPARED_SCHEMA_OBJECT_OPTIONS
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ObservedSchemaObjects:
    """The views, materialized views, dictionaries, functions, sequences, policies, grants and row
    level security of a model state replaced with the ones the database has."""

    @staticmethod
    async def apply_observed_schema_objects(
        connection: DatabaseClient,
        introspector_class: type[SchemaIntrospector],
        model_state: ModelState,
        schema: str,
        table_info: TableInfo,
        column_to_field_name: Mapping[str, str],
        options: dict[str, Any],
    ) -> None:
        """Replaces the objects a model declares beside its table in ``options`` with the ones the
        database has of them.

        Args:
            connection: The connection.
            introspector_class: The connection's introspector.
            model_state: The model's declared state.
            schema: The schema of the model.
            table_info: The model's introspected table.
            column_to_field_name: Column name -> the name of the model field owning it.
            options: The observed options of the model, changed in place.
        """
        row_level_security = await introspector_class.fetch_row_level_security(
            connection, schema, table_info.name, model_state.options.get(ModelOption.ROW_LEVEL_SECURITY)
        )
        if row_level_security is None:
            options.pop(ModelOption.ROW_LEVEL_SECURITY, None)
        else:
            options[ModelOption.ROW_LEVEL_SECURITY] = row_level_security
        declared = {
            option: tuple(model_state.options[option])
            for option in DRIFT_COMPARED_SCHEMA_OBJECT_OPTIONS
            if model_state.options.get(option)
        }
        if not declared:
            return
        observed = await introspector_class.fetch_declared_schema_objects(
            connection, schema, declared, table_info, column_to_field_name
        )
        for option in declared:
            if observed.get(option):
                options[option] = tuple(observed[option])
            else:
                options.pop(option, None)
