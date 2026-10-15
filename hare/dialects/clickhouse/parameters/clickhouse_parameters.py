from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_VALUE_SET_MINIMUM
from hare.dialects.clickhouse.types.clickhouse_nullable_types import ClickhouseNullableTypes
from hare.dialects.clickhouse.types.clickhouse_type_names import ClickhouseTypeNames

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field


class ClickhouseParameters(SqlParameters):
    """ClickHouse's parameters - numbered ``$n`` placeholders, each replaced by its value's literal
    (``ClickhouseLiterals``) by the client before the statement is sent."""

    placeholder_template = "${}"
    # A long list of values of one plain type is one parameter, an external table of a read.
    single_parameter_in_list_min_length = CLICKHOUSE_VALUE_SET_MINIMUM
    # JSON containment is built from the compared value's keys and nesting.
    describes_json_containment_by_shape = True

    def get_field_parameter_cast_type(self, field: Field[Any]) -> str | None:
        # A bare literal takes the narrowest type holding it - the rows of a bulk update or of a
        # VALUES list holding values of different widths (an Int256 and a UInt256) share no type.
        column_type = ClickhouseTypeNames.get_server_type(field.get_column_type(self.dialect))
        return ClickhouseNullableTypes.get_nullable_type(column_type) if field.null else column_type

    def get_row_key_cast_type(self, field: Field[Any]) -> str | None:
        # The keys of the rows make a set the table's rows are looked up in. A bare literal takes the
        # narrowest type holding it (1 is a UInt8), and the server refuses to look a wider key of
        # another table row up in such a set.
        return ClickhouseTypeNames.get_server_type(field.get_column_type(self.dialect))

    def supports_copy_column_type(self, column_type: str) -> bool:
        # A bulk load writes every column in the type the table declares for it.
        return True
