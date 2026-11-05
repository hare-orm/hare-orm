from types import SimpleNamespace

import pytest

from hare.dialects.clickhouse.client.clickhouse_statement_result import ClickhouseStatementResult
from hare.dialects.clickhouse.query.clickhouse_totals import ClickhouseTotals
from hare.query.queryset.extensions.query_set_extension_result import QuerySetExtensionResult
from hare.query.rows.values_rows.dict_rows import DictRows
from hare.query.rows.values_rows.flat_rows import FlatRows

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("by_position", [False, True])
@pytest.mark.parametrize("flat", [False, True])
def test_totals_and_ordinary_rows_share_the_result_layout(by_position, flat):
    reader = FlatRows(("total",), (("total",),)) if flat else DictRows(("total",), (("total",),))
    result = ClickhouseStatementResult(
        1,
        [("2",)] if by_position else [{"total": "2"}],
        description=(("total",),),
    )
    result.totals = ("7",) if by_position else {"total": "7"}
    connection = SimpleNamespace(features=SimpleNamespace(supports_positional_rows=by_position))
    converted = []

    def decode(value):
        converted.append(value)
        return int(value)

    read_row = reader.get_result_reader(connection, result, [("total", decode)])
    rows = [read_row(row) for row in result.rows]
    output = ClickhouseTotals.transform_result(QuerySetExtensionResult(rows, result, read_row))
    assert output == ([2] if flat else [{"total": 2}])
    assert output.totals == (7 if flat else {"total": 7})
    assert converted == ["2", "7"]
