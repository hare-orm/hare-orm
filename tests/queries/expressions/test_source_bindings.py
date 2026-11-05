import pytest

from hare.exceptions import UnSupportedError
from hare.sql import Query, Table
from hare.sql.builder.queries.correlated_subqueries import CorrelatedSubqueries
from hare.sql.constants import DEFAULT_SQL_CONTEXT

pytestmark = pytest.mark.database_independent


def test_reused_subquery_gets_parent_local_alias_without_mutation():
    book = Table("book")
    child = Query.from_(book).select(book.id)
    saved = {child: "original"}
    first = Query.from_(child).select(child.id)
    second = Query.from_(Query.from_(book).select(book.id)).from_(child).select(child.id)
    first_sql = first.get_sql()
    assert child.alias is None
    assert saved[child] == "original"
    assert '"sq0"."id"' in first_sql
    assert '"sq1"."id"' in second.get_sql()
    assert first.get_sql() == first_sql


def test_join_and_star_use_the_local_source_binding():
    book = Table("book")
    child = Query.from_(book).select(book.id)
    joined = Query.from_(book).join(child).on(book.id == child.id).select(child.star)
    assert child.alias is None
    sql = joined.get_sql()
    assert '"sq0".*' in sql
    assert '"sq0"."id"' in sql


def test_source_identity_does_not_depend_on_printed_alias_or_from_clause():
    source = Query.from_(Table("book")).select("id")
    first, second = source.as_("same"), source.as_("same")
    saved = {first: 1, second: 2}
    assert len(saved) == 2
    first.alias = "different"
    first._from = [Table("author")]
    assert saved[first] == 1
    assert saved[second] == 2
    assert first != second


@pytest.mark.parametrize(
    ("feature", "position", "make_query"),
    [
        ("supports_correlated_select", "SELECT", lambda outer, child: Query.from_(outer).select(child)),
        (
            "supports_correlated_order_by",
            "ORDER BY",
            lambda outer, child: Query.from_(outer).select(outer.id).orderby(child),
        ),
        (
            "supports_correlated_group_by",
            "GROUP BY",
            lambda outer, child: Query.from_(outer).select(outer.id).groupby(child),
        ),
        ("supports_correlated_update", "UPDATE", lambda outer, child: Query.update(outer).set("id", child)),
        (
            "supports_correlated_delete",
            "DELETE",
            lambda outer, child: Query.from_(outer).delete().where(outer.id == child),
        ),
    ],
)
def test_correlation_capabilities_are_specific_to_statement_position(feature, position, make_query):
    outer, inner = Table("author"), Table("book")
    child = Query.from_(inner).select(inner.id).where(inner.author_id == outer.id)
    query = make_query(outer, child)
    query.get_sql(DEFAULT_SQL_CONTEXT)
    disabled = DEFAULT_SQL_CONTEXT.dialect.features.replace(**{feature: False})
    with pytest.raises(UnSupportedError, match=position):
        query.get_sql(DEFAULT_SQL_CONTEXT.copy(features=disabled))
    # The dialect itself was not changed by the connection's capabilities.
    query.get_sql(DEFAULT_SQL_CONTEXT)


def test_correlation_distinguishes_schemas_with_the_same_table_name():
    outer = Table("book", schema="public")
    inner = Table("book", schema="archive")
    query = Query.from_(inner).select(inner.id).where(inner.id == outer.id)
    assert CorrelatedSubqueries.query_reads_other_tables(query, set())


def test_source_names_in_a_sibling_subquery_do_not_hide_correlation():
    outer, inner, sibling = Table("author"), Table("book"), Table("review")
    correlated = Query.from_(inner).select(inner.id).where(inner.author_id == outer.id)
    unrelated = Query.from_(outer).select(outer.id)
    query = Query.from_(sibling).select(correlated, unrelated)
    assert CorrelatedSubqueries.query_reads_other_tables(query, set())


def test_derived_column_reference_is_not_an_inline_nested_query():
    book = Table("book")
    child = Query.from_(book).select(book.id).as_("selected")
    parent = Query.from_(child).select(child.id)
    assert list(CorrelatedSubqueries.iterate_term_queries(child.id)) == []
    assert not CorrelatedSubqueries.query_reads_other_tables(parent, set())


def test_set_operation_branches_are_independent_correlation_scopes():
    outer, inner = Table("author"), Table("book")
    correlated = Query.from_(inner).select(inner.id).where(inner.author_id == outer.id)
    local = Query.from_(outer).select(outer.id)
    combined = local.union_all(correlated)
    assert len(list(CorrelatedSubqueries.iterate_term_queries(combined))) == 2
    assert CorrelatedSubqueries.reads_other_tables(combined, set())
    assert not CorrelatedSubqueries.reads_other_tables(combined, {outer})
