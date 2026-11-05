"""Computed vectors share generation and retain explicit language choices without mutating inputs."""

import pytest

from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.ts_vector_field import TSVectorField
from hare.dialects.postgresql.lookups.postgresql_filter_operators import PostgresqlFilterOperators
from hare.exceptions import ConfigurationError, QueryError
from hare.query.expressions import ExpressionContext
from hare.search import SearchQuery, SearchVector
from tests.dialects.postgresql.models_tsvector import TSVectorEntry

pytestmark = pytest.mark.database_independent


def bind_field(expression, config=None):
    field = TSVectorField(expression=expression, config=config)
    field.model = TSVectorEntry
    field.model_field_name = "search_vector"
    return field


def test_defaults_apply_to_each_vector_without_overwriting_explicit_configuration():
    expression = SearchVector("title", weight="A") + SearchVector("body", config="english", weight="B")
    field = bind_field(expression, "russian")
    sql = field.get_generated_sql(DialectRegistry.get_dialect("postgresql"))
    assert "TO_TSVECTOR('russian',COALESCE(\"title\",''))" in sql
    assert "TO_TSVECTOR('english',COALESCE(\"body\",''))" in sql
    assert expression.left.config is None
    assert expression.right.config == "english"


def test_expression_dependencies_and_renaming_preserve_configuration_literals():
    expression = SearchVector("title", config="title") + SearchVector("body", config="english")
    field = bind_field(expression)
    renamed = field.with_renamed_generated_from_field("title", "heading")
    assert renamed.get_generated_from_field_names() == ("body", "heading")
    assert renamed.expression.left.config == "title"
    assert field.get_generated_from_field_names() == ("body", "title")
    assert field.expression is expression


@pytest.mark.parametrize("declaration", [{"source_fields": ()}, {"source_fields": ("title",)}, {"weights": ()}])
def test_expression_rejects_conflicting_short_form(declaration):
    with pytest.raises(ConfigurationError, match="cannot be combined"):
        TSVectorField(expression=SearchVector("title"), **declaration)


def test_each_generated_vector_requires_a_configuration():
    field = bind_field(SearchVector("title", config="english") + SearchVector("body"))
    with pytest.raises(ConfigurationError, match="explicit config"):
        field.get_generated_sql(DialectRegistry.get_dialect("postgresql"))


def test_search_without_common_configuration_requires_explicit_query_languages():
    dialect = DialectRegistry.get_dialect("postgresql")
    context = ExpressionContext(
        model=TSVectorEntry, table=TSVectorEntry.get_table(), annotations={}, dialect=dialect, connection=None
    )
    term = context.table.search_vector
    with pytest.raises(QueryError, match="SearchQuery"):
        PostgresqlFilterOperators.get_search_criterion(term, "word", True, requires_search_query_config=True)
    with pytest.raises(QueryError, match="SearchQuery"):
        PostgresqlFilterOperators.get_search_criterion(
            term, SearchQuery("word").get_result(context).term, True, requires_search_query_config=True
        )
    query = SearchQuery("word", config="english") | SearchQuery("слово", config="russian")
    query_term = query.get_result(context).term
    criterion = PostgresqlFilterOperators.get_search_criterion(
        term, query_term, True, requires_search_query_config=True
    )
    assert criterion.right is query_term


def test_plain_column_and_short_generated_form_remain_distinct():
    plain = TSVectorField()
    assert not plain.generated
    assert plain.get_generated_sql(DialectRegistry.get_dialect("postgresql")) is None
    assert not plain.get_lookups()["search"].requires_search_query_config
    short = TSVectorField(source_fields=("title",), config="english", weights=("A",))
    assert short.generated
    assert short.get_generated_from_field_names() == ("title",)
