"""Every result shape uses the same column selection, decoding and NULL grouping."""

from contextlib import aclosing
from types import SimpleNamespace

import pytest

from hare.dialects.base.results.statement_result import StatementResult
from hare.query.rows.row_column import RowColumn
from hare.query.rows.row_layout import RowLayout
from hare.query.rows.values_rows.dict_rows import DictRows
from hare.query.rows.values_rows.flat_rows import FlatRows
from hare.query.rows.values_rows.named_rows import NamedRows
from hare.query.rows.values_rows.tuple_rows import TupleRows

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("row_type", [DictRows, TupleRows, FlatRows, NamedRows])
@pytest.mark.parametrize("by_position", [False, True])
@pytest.mark.parametrize(
    ("aliases", "expected_values", "expected_calls"),
    [
        (("first",), [1, None, 3], ["1", None, "3"]),
        (("first", "second"), [(1, 2), None, (3, None)], ["1", "2", None, None, "3", None]),
    ],
    ids=["scalar", "composite"],
)
def test_result_shapes_share_column_decoding(row_type, by_position, aliases, expected_values, expected_calls):
    reader = row_type(("target",), (aliases,))
    calls = []

    def decode(value):
        calls.append(value)
        return None if value is None else int(value)

    converters = [(alias, decode) for alias in aliases]
    names = ("ignored", "second", "first")
    values = [(99, "2", "1"), (99, None, None), (99, None, "3")]
    rows = values if by_position else [dict(zip(names, row, strict=True)) for row in values]
    original_rows = [row.copy() if isinstance(row, dict) else row for row in rows]
    result = StatementResult(len(rows), rows, description=tuple((name,) for name in names))
    connection = SimpleNamespace(features=SimpleNamespace(supports_positional_rows=by_position))
    actual = reader.convert_batch(connection, result, converters)
    if row_type is DictRows:
        assert actual == [{"target": value} for value in expected_values]
    elif row_type is FlatRows:
        assert actual == expected_values
    else:
        assert actual == [(value,) for value in expected_values]
        if row_type is NamedRows:
            assert [row.target for row in actual] == expected_values
    assert calls == expected_calls
    assert rows == original_rows
    assert values == [(99, "2", "1"), (99, None, None), (99, None, "3")]


@pytest.mark.parametrize("by_position", [False, True])
@pytest.mark.parametrize(
    ("raw", "expected"),
    [((99, "2", "1"), (1, 2)), ((99, None, None), (None, None)), ((99, None, "3"), (3, None))],
)
def test_attribute_assignment_preserves_column_selection_and_custom_readers(by_position, raw, expected):
    calls = []

    def decode(value):
        calls.append(value)
        return None if value is None else int(value)

    names = ("ignored", "second", "first")
    row = raw if by_position else dict(zip(names, raw, strict=True))
    layout = RowLayout(tuple(RowColumn(key, decode) for key in ((2, 1) if by_position else ("first", "second"))))
    instance = SimpleNamespace()
    layout.assign(instance, row, ("first", "second"))
    assert vars(instance) == dict(zip(("first", "second"), expected, strict=True))
    assert calls == [raw[2], raw[1]]
    assert row == (raw if by_position else dict(zip(names, raw, strict=True)))


@pytest.mark.parametrize("row_type", [DictRows, TupleRows, FlatRows, NamedRows])
@pytest.mark.parametrize("by_position", [False, True])
@pytest.mark.parametrize("stop_early", [False, True])
@pytest.mark.asyncio
async def test_streamed_result_shapes_close_the_cursor_and_preserve_empty_batches(row_type, by_position, stop_early):
    class DriverRow(tuple):
        def keys(self):
            return ("ignored", "value")

    closed = []
    requests = []
    converted = []

    def decode(value):
        converted.append(value)
        return int(value)

    async def stream(sql, parameters, *, chunk_size):
        requests.append((sql, parameters, chunk_size))
        try:
            yield []
            for value in ("1", "2"):
                yield [DriverRow((99, value)) if by_position else {"ignored": 99, "value": value}]
                yield []
        finally:
            closed.append(True)

    connection = SimpleNamespace(features=SimpleNamespace(supports_positional_rows=by_position), stream_batches=stream)
    reader = row_type(("value",), (("value",),))
    batches = []
    async with aclosing(reader.stream_batches(connection, "SELECT value", [7], [("value", decode)], 2)) as source:
        async for batch in source:
            batches.append(batch)
            if stop_early and batch:
                break
    expected = [1] if stop_early else [1, 2]
    assert batches[0] == []
    actual = [row for batch in batches for row in batch]
    if row_type is DictRows:
        assert actual == [{"value": value} for value in expected]
    elif row_type is FlatRows:
        assert actual == expected
    else:
        assert actual == [(value,) for value in expected]
        if row_type is NamedRows:
            assert [row.value for row in actual] == expected
    assert requests == [("SELECT value", [7], 2)]
    assert converted == [str(value) for value in expected]
    assert closed == [True]


@pytest.mark.parametrize("by_position", [False, True])
@pytest.mark.asyncio
async def test_a_streamed_conversion_error_closes_the_cursor(by_position):
    class DriverRow(tuple):
        def keys(self):
            return ("value",)

    closed = []

    async def stream(sql, parameters, *, chunk_size):
        try:
            for value in ("1", "invalid", "2"):
                yield [DriverRow((value,)) if by_position else {"value": value}]
        finally:
            closed.append(True)

    connection = SimpleNamespace(features=SimpleNamespace(supports_positional_rows=by_position), stream_batches=stream)
    reader = TupleRows(("value",), (("value",),))
    received = []
    with pytest.raises(ValueError, match="invalid literal"):
        async with aclosing(reader.stream_batches(connection, "SELECT value", [], [("value", int)], 1)) as source:
            async for batch in source:
                received.extend(batch)
    assert received == [(1,)]
    assert closed == [True]
