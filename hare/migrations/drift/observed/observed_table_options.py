from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.ddl.table_options import TableOptions
from hare.inspectdb import SchemaIntrospector
from hare.inspectdb.introspection.table_info import TableInfo
from hare.migrations.drift.observed.column_field_names import ColumnFieldNames
from hare.migrations.state.model_state import ModelState
from hare.models.enums import ModelOption

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient


class ObservedTableOptions:
    """The table options of the connection's dialect replaced with the ones the table has - the entries
    of other dialects kept as declared, since this database can't show them."""

    @staticmethod
    async def apply_observed_table_options(
        connection: DatabaseClient,
        introspector_class: type[SchemaIntrospector],
        dialect_name: str,
        model_state: ModelState,
        table_info: TableInfo,
        options: dict[str, Any],
    ) -> None:
        """Replaces the ``Meta.table_options`` entry of the connection's dialect in ``options``
        with the table's observed options - the entries of other dialects stay as declared, since
        this database can't show them.

        Args:
            connection: The connection the table was read on.
            introspector_class: The connection's introspector.
            dialect_name: The connection's dialect.
            model_state: The model's declared state.
            table_info: The introspected table.
            options: The observed options of the model, changed in place.
        """
        declared_table_options: tuple[TableOptions, ...] = tuple(
            model_state.options.get(ModelOption.TABLE_OPTIONS, ())
        )
        declared = next((entry for entry in declared_table_options if entry.dialect_name == dialect_name), None)
        observed = await introspector_class.fetch_declared_table_options(
            connection,
            table_info.table_options,
            declared,
            table_info,
            ColumnFieldNames.column_to_field_name(model_state.fields),
        )
        # The observed entry takes the declared one's place - the options compare as a sequence.
        table_options = tuple(
            entry
            for declared_entry in declared_table_options
            for entry in ((observed,) if declared_entry.dialect_name == dialect_name else (declared_entry,))
            if entry is not None
        )
        if declared is None and observed is not None:
            table_options = (*table_options, observed)
        if table_options:
            options[ModelOption.TABLE_OPTIONS] = table_options
        else:
            options.pop(ModelOption.TABLE_OPTIONS, None)
