import pytest

from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.query.statements.select.values.window_filter_wrapping import WindowFilterWrapping
from hare.sql import Query, Table
from hare.sql.functions.random_number import RandomNumber
from hare.sql.functions.text.lower import Lower
from hare.sql.terms.select_reference import SelectReference
from hare.sql.terms.term import Term

pytestmark = pytest.mark.database_independent


def test_projection_reuses_computed_columns_without_rendering_sql():
    class OpaqueTerm(Term):
        def get_sql(self, sql_context):
            raise AssertionError("Projection preparation must not render SQL")

    expression = OpaqueTerm()
    query = Query.from_(Table("book")).select(expression.as_("label"))
    projection = DerivedProjection(query, column_prefix="projected ")
    assert projection.get_reference(expression).name == "projected 0"
    assert len(projection.columns) == 1
    assert query._selects[0].alias == "label"


def test_window_filter_operands_are_hidden_without_changing_output_or_source():
    book = Table("book")
    query = Query.from_(book).select(Lower(book.title).as_("label"))
    projection = DerivedProjection(query, column_prefix="projected ")
    WindowFilterWrapping.get_derived_table_criterion(
        (Lower(book.title) == "alpha") | (book.id > 1), projection.get_reference
    )
    assert len(projection.columns) == 2
    outer = Query.from_(projection.query).select(*projection.get_output(("label",)))
    assert len(outer._selects) == 1
    assert outer._selects[0].alias == "label"
    assert outer._selects[0].name == projection.by_alias["label"].name
    assert len(query._selects) == 1


def test_projection_preserves_select_position_references_after_renaming():
    book = Table("book")
    selected = Lower(book.title).as_("label")
    query = Query.from_(book).select(selected).groupby(SelectReference(selected))
    projection = DerivedProjection(query, column_prefix="projected ")
    assert projection.query._groupbys[0].get_select_position(projection.query._selects) == 1
    assert query._groupbys[0].get_select_position(query._selects) == 1


def test_projection_keeps_independent_random_values_separate():
    first = RandomNumber()
    query = Query.from_(Table("book")).select(first.as_("random"))
    projection = DerivedProjection(query)
    projection.include(first)
    projection.include(RandomNumber())
    assert len(projection.columns) == 2
