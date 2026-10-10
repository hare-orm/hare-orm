from __future__ import annotations

from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.clickhouse.lookups.constants import CLICKHOUSE_VALUE_SET_MINIMUM


class ClickhouseParameters(SqlParameters):
    """ClickHouse's parameters - numbered ``$n`` placeholders, each replaced by its value's literal
    (``ClickhouseLiterals``) by the client before the statement is sent."""

    placeholder_template = "${}"
    # A long list of values of one plain type is one parameter, an external table of a read.
    single_parameter_in_list_min_length = CLICKHOUSE_VALUE_SET_MINIMUM
    # JSON containment is built from the compared value's keys and nesting.
    describes_json_containment_by_shape = True

    def supports_copy_column_type(self, column_type: str) -> bool:
        # A bulk load writes every column in the type the table declares for it.
        return True
