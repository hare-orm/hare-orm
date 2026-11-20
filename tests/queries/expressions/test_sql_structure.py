import pytest

from hare.exceptions import QueryError
from hare.sql import Table
from hare.sql.analytics import Preceding
from hare.sql.functions.random_number import RandomNumber
from hare.sql.functions.text.lower import Lower
from hare.sql.terms.array import Array
from hare.sql.terms.functions.function import Function
from hare.sql.terms.functions.window_frame_analytic_function import WindowFrameAnalyticFunction
from hare.sql.terms.node_structure import NodeStructure
from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

pytestmark = pytest.mark.database_independent


def test_structural_identity_keeps_operators_sources_values_and_aliases_separate():
    book = Table("book")
    other = book.as_("other")
    assert NodeStructure.get_key(book.price + 1) == NodeStructure.get_key((book.price + 1).as_("amount"))
    assert NodeStructure.get_key(book.price + 1) != NodeStructure.get_key(book.price - 1)
    assert NodeStructure.get_key(book.price + 1) != NodeStructure.get_key(other.price + 1)
    assert NodeStructure.get_key(book.price + 1) != NodeStructure.get_key(book.price + 2)


def test_unknown_terms_and_volatile_calls_are_opaque_even_when_sql_could_match():
    class OpaqueTerm(Term):
        def get_sql(self, sql_context):
            raise AssertionError("Structural operations must not render SQL")

    first, second = OpaqueTerm(), OpaqueTerm()
    assert NodeStructure.get_key(first) == NodeStructure.get_key(first.as_("selected"))
    assert NodeStructure.get_key(first) != NodeStructure.get_key(second)
    random = RandomNumber()
    assert NodeStructure.get_key(random) == NodeStructure.get_key(random.as_("number"))
    assert NodeStructure.get_key(random) != NodeStructure.get_key(RandomNumber())


def test_undeclared_subclass_does_not_inherit_an_incomplete_identity():
    class CustomLower(Lower):
        def __init__(self, term, setting):
            super().__init__(term)
            self.setting = setting

    book = Table("book")
    assert NodeStructure.get_key(CustomLower(book.title, "first")) != NodeStructure.get_key(
        CustomLower(book.title, "second")
    )


def test_window_identity_distinguishes_equal_and_different_frames():
    book = Table("book")
    first = WindowFrameAnalyticFunction("SUM", book.price).orderby(book.id).rows(Preceding(2))
    same = WindowFrameAnalyticFunction("SUM", book.price).orderby(book.id).rows(Preceding(2))
    different = WindowFrameAnalyticFunction("SUM", book.price).orderby(book.id).rows(Preceding(3))
    assert NodeStructure.get_key(first) == NodeStructure.get_key(same)
    assert NodeStructure.get_key(first) != NodeStructure.get_key(different)


def test_structural_rewrite_is_iterative_and_does_not_enter_literal_payloads():
    book = Table("book")
    leaf = book.price
    expression = leaf
    for _ in range(2000):
        expression = expression + 1
    original_key = NodeStructure.get_key(expression)
    replaced = NodeStructure.rewrite(expression, lambda node: book.cost if node is leaf else None)
    rewritten_key = NodeStructure.get_key(replaced)
    hash(original_key)
    hash(rewritten_key)
    assert original_key != rewritten_key
    literal = ValueWrapper({"expression": book.price})
    assert (
        NodeStructure.rewrite(literal, lambda node: book.cost if node is literal.value["expression"] else None)
        is literal
    )


def test_structural_cycles_are_rejected_but_shared_children_are_allowed():
    book = Table("book")
    shared = book.price + 1
    NodeStructure.get_key(shared + shared)
    cyclic = book.price + 1
    cyclic.left = cyclic
    with pytest.raises(QueryError, match="[Cc]ycl"):
        NodeStructure.get_key(cyclic)
    with pytest.raises(QueryError, match="[Cc]ycl"):
        NodeStructure.rewrite(cyclic, lambda node: None)
    with pytest.raises(QueryError, match="[Cc]ycl"):
        cyclic.replace_table(book, Table("other"))


def test_table_replacement_preserves_undeclared_function_and_custom_override_contracts():
    class CustomFunction(Function):
        pass

    class CustomReplacement(Function):
        calls = 0

        def replace_table(self, current_table, new_table):
            type(self).calls += 1
            return super().replace_table(current_table, new_table)

    book, other = Table("book"), Table("other")
    custom = CustomFunction("ABS", book.price)
    assert NodeStructure.get_attributes(custom) is None
    assert NodeStructure.rewrite(custom, lambda node: None) is custom
    rewritten = custom.replace_table(book, other)
    assert rewritten.args[0].table == other
    assert custom.args[0].table == book
    expression = Function("ABS", CustomReplacement("ABS", book.price))
    rewritten = expression.replace_table(book, other)
    assert CustomReplacement.calls == 1
    assert rewritten.args[0].args[0].table == other


def test_table_replacement_invalidates_ancestor_hash_after_mutable_child_changes():
    book, other = Table("book"), Table("other")
    field = book.price
    field.immutable = False
    expression = Function("ABS", field)
    expression._hash_cache = "cached before replacement"
    rewritten = expression.replace_table(book, other)
    assert rewritten.args[0] is field
    assert field.table == other
    assert "_hash_cache" not in vars(rewritten)
    mutable_expression = Function("ABS", book.price)
    mutable_expression.immutable = False
    assert mutable_expression.replace_table(book, other) is mutable_expression
    assert mutable_expression.args[0].table == other


def test_rebuilt_condition_updates_depth_without_changing_the_input():
    book = Table("book")
    leaf = book.price > 0
    child = (book.id > 0) & (book.price < 100)
    condition = leaf | child
    rebuilt = NodeStructure.rewrite(condition, lambda node: leaf if node is child else None)
    assert rebuilt.depth == 1
    assert condition.depth == 2


def test_array_rewrite_updates_the_values_used_for_rendering():
    book = Table("book")
    price = book.price
    array = Array(price, 1)
    rebuilt = NodeStructure.rewrite(array, lambda node: book.cost if node is price else None)
    assert rebuilt.original_value[0].name == "cost"
    assert array.original_value[0].name == "price"
