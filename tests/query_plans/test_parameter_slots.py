"""Prepared bindings retain structure, preserve parameter ordering and expose validation errors."""

import gc
import weakref

import pytest

from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT
from hare.dialects.clickhouse.types.declarations import ClickhouseTypedValue
from hare.query.expressions.value_references.literal_value_reference import LiteralValueReference
from hare.query.plans.statement.parameter_slot import ParameterSlot
from hare.query.plans.statement.statement_plan import StatementPlan
from hare.sql import Query, Table
from hare.sql.constants import DEFAULT_SQL_CONTEXT
from hare.sql.terms.parameters.query_parameters import QueryParameters
from hare.sql.terms.values.value_wrapper import ValueWrapper


def test_prepared_plan_releases_input_values_and_source_tree():
    class Text(str):
        pass

    value = Text("first")
    wrapper = ValueWrapper(value)
    value_ref = weakref.ref(value)
    wrapper_ref = weakref.ref(wrapper)
    book = Table("book")
    query = Query.from_(book).select(book.id).where(book.title == wrapper)
    statement, plan = StatementPlan.prepare(query, ((LiteralValueReference(wrapper),),))
    assert plan is not None
    assert statement.parameters[0] is value
    del statement, query, value, wrapper
    gc.collect()

    assert value_ref() is None
    assert wrapper_ref() is None
    assert plan.parameters == [None]
    assert plan.bind([Text("second")], object, None) == ["second"]


def test_repeated_sql_occurrences_bind_fresh_sensitive_values():
    book = Table("book")
    wrapper = ValueWrapper("first")
    wrapper.sensitive = True
    query = Query.from_(book).select(book.id).where((book.title == wrapper) | (book.subtitle == wrapper))
    _, plan = StatementPlan.prepare(query, ((LiteralValueReference(wrapper),),))
    assert plan is not None

    bindings = []
    for value in ("second", "third"):
        parameters = plan.bind([value], object, None)
        assert parameters == [value, value]
        assert isinstance(parameters, QueryParameters)
        assert parameters.hidden_indexes == {0, 1}
        bindings.append(parameters)
    assert bindings[0] == ["second", "second"]
    assert bindings[0] is not bindings[1]
    bindings[0][0] = "changed"
    assert bindings[1] == ["third", "third"]
    assert plan.bind(["fourth"], object, None) == ["fourth", "fourth"]
    assert plan.parameters == [None, None]


@pytest.mark.parametrize(
    ("slot", "value", "forced", "accepted"),
    [
        (ParameterSlot((0,), str), "title", False, True),
        (ParameterSlot((0,), str), "*", False, False),
        (ParameterSlot((0,), str), "*", True, True),
        (ParameterSlot((0,), int), True, False, False),
        (ParameterSlot((0,), str, literal=True), "title", False, False),
        (ParameterSlot((), str), object(), False, True),
    ],
)
def test_slot_accepts_only_values_preserving_rendered_shape(slot, value, forced, accepted):
    parameters = [None]
    assert slot.bind(parameters, value, forced) is accepted
    assert parameters == ([value] if accepted and slot.positions else [None])


def test_value_conversion_errors_are_not_cache_misses():
    def convert(value):
        raise ValueError("refused input")

    book = Table("book")
    wrapper = ValueWrapper("first")
    query = Query.from_(book).select(book.id).where(book.title == wrapper)
    _, plan = StatementPlan.prepare(query, ((LiteralValueReference(wrapper, convert),),))
    assert plan is not None
    with pytest.raises(ValueError, match="refused input"):
        plan.bind(["second"], object, None)


@pytest.mark.parametrize(
    "replacement",
    [ClickhouseTypedValue("5", "String"), ClickhouseTypedValue(5, "Int64"), ClickhouseTypedValue(5, "Int32")],
)
def test_prepared_parameters_preserve_encoded_type_metadata(replacement):
    first = ClickhouseTypedValue(3, "Int32")
    wrapper = ValueWrapper(first)
    book = Table("book")
    query = Query.from_(book).select(book.id).where(book.value == wrapper)
    _, plan = StatementPlan.prepare(
        query,
        ((LiteralValueReference(wrapper),),),
        sql_context=DEFAULT_SQL_CONTEXT.copy(dialect=CLICKHOUSE_DIALECT),
    )
    expected = [replacement] if replacement.column_type == first.column_type else None
    assert plan is not None
    assert plan.bind([replacement], object, CLICKHOUSE_DIALECT) == expected
