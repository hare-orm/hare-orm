import pytest

from hare.exceptions import QueryError
from hare.sql import Table
from hare.sql.enums import Arithmetic
from hare.sql.terms.arithmetic_expression import ArithmeticExpression
from hare.sql.terms.array import Array
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.functions.analytic_function import AnalyticFunction

pytestmark = pytest.mark.database_independent


def test_deep_sql_tree_uses_direct_children():
    book = Table("book")
    expression = book.price
    for _ in range(2000):
        expression = ArithmeticExpression(Arithmetic.ADD, expression, book.tax)
    assert len(expression.find_(type(book.price))) == 2001


def test_shared_column_after_aggregate_still_requires_grouping():
    book = Table("book")
    price = book.price
    expression = AggregateFunction("SUM", price) + price
    assert expression.contains_aggregate
    assert expression.get_group_by_column_terms() == [price]


def test_window_arguments_and_ordering_can_read_group_aggregates():
    book = Table("book")
    plain = AnalyticFunction("SUM", book.price).over(book.category)
    grouped = AnalyticFunction("ROW_NUMBER").orderby(AggregateFunction("SUM", book.price))
    assert not plain.contains_aggregate
    assert plain.get_window_column_terms() == [book.price, book.category]
    assert grouped.contains_aggregate
    assert grouped.get_window_column_terms() == []


def test_cyclic_array_literal_is_rejected():
    values = []
    values.append(values)
    with pytest.raises(QueryError):
        list(Array(values).nodes_())
