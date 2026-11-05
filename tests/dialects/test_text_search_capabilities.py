from types import SimpleNamespace

import pytest

from hare import fields
from hare.dialects.base.search.enums import TextSearchOperation
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.dialects.postgresql.search import Lexeme
from hare.dialects.postgresql.search.criterion.search_criterion import SearchCriterion
from hare.dialects.postgresql.search.criterion.ts_query_function import TsQueryFunction
from hare.dialects.postgresql.search.criterion.ts_query_invert import TsQueryInvert
from hare.dialects.sqlite.search.terms.full_text_match import FullTextMatch
from hare.dialects.sqlite.search.terms.full_text_rank import FullTextRank
from hare.exceptions import UnSupportedError
from hare.migrations.state.state_apps import StateApps
from hare.models import Model
from hare.query.expressions import ExpressionContext
from hare.search import SearchHeadline, SearchQuery, SearchRank, SearchVector
from hare.sql import Table

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize("term_class", [FullTextMatch, FullTextRank])
def test_search_replacement_updates_query_and_row_key_without_changing_source(term_class):
    source = Table("book", alias="source")
    target = Table("book", alias="target")
    term = term_class(source.id, "book_search", source.title)
    replaced = term.replace_table(source, target)
    assert replaced.row_key.table is target
    assert replaced.query.table is target
    assert term.row_key.table is source
    assert term.query.table is source


def test_inverted_search_replaces_tables_and_quotes_its_alias():
    source = Table("book", alias="source")
    target = Table("book", alias="target")
    term = TsQueryInvert(source.title).as_('search"result')
    replaced = term.replace_table(source, target)
    context = PostgresqlDialect().sql_context.copy(with_namespace=True, with_alias=True)
    assert replaced.get_sql(context) == '!!("target"."title") "search""result"'
    assert replaced.get_sql(context.copy(with_alias=False)) == '!!("target"."title")'
    assert term.term.table is source


@pytest.mark.parametrize("configured", [False, True])
def test_inverted_search_keeps_configuration_with_rewritten_arguments(configured):
    source, target = Table("source"), Table("target")
    query = TsQueryFunction("PLAINTO_TSQUERY", source.title, source.language if configured else None)
    term = TsQueryInvert(query)
    replaced = term.replace_table(source, target)
    assert replaced.term.args[-1].table is target
    assert query.args[-1].table is source
    assert SearchCriterion.has_explicit_query_config(replaced) is configured
    if configured:
        assert SearchCriterion.get_query_config(replaced).table is target
        assert query.config_term.table is source
    else:
        assert SearchCriterion.get_query_config(replaced) is None


@pytest.fixture
def search_context():
    class Book(Model):
        id = fields.IntField(primary_key=True)
        title = fields.TextField()

    apps = StateApps()
    apps.register_model("models", Book)
    apps.init_relations()
    dialect = PostgresqlDialect()
    return ExpressionContext(Book, Table("book"), {}, dialect=dialect, connection=None)


@pytest.mark.parametrize("with_connection", [False, True])
@pytest.mark.parametrize(
    ("operation", "expression"),
    [
        (TextSearchOperation.VECTOR, SearchVector("title")),
        (TextSearchOperation.VECTOR_CONCATENATION, SearchVector("title") + SearchVector("title")),
        (TextSearchOperation.VECTOR_WEIGHT, SearchVector("title", weight="A")),
        (TextSearchOperation.RAW_QUERY_EXPRESSION, SearchQuery(Lexeme("hare"))),
        (TextSearchOperation.RANK_LABEL_WEIGHTS, SearchRank("title", "hare", weights=[0.1, 0.2, 0.4, 1.0])),
        (TextSearchOperation.RANK_NORMALIZATION, SearchRank("title", "hare", normalization=1)),
        (TextSearchOperation.RANK_COVER_DENSITY, SearchRank("title", "hare", cover_density=True)),
        *[
            (TextSearchOperation(f"headline_{name}"), SearchHeadline("title", "hare", **{name: value}))
            for name, value in (
                ("start_sel", "<b>"),
                ("stop_sel", "</b>"),
                ("max_words", 20),
                ("min_words", 2),
                ("short_word", 3),
                ("highlight_all", True),
                ("max_fragments", 2),
                ("fragment_delimiter", "..."),
            )
        ],
    ],
)
def test_search_capabilities_are_independent(search_context, operation, expression, with_connection):
    from dataclasses import replace

    dialect = search_context.dialect
    # Configuration support is unrelated to any of these operations.
    enabled = dialect.features.replace(supports_text_search_configurations=False)
    if with_connection:
        client = SimpleNamespace(features=enabled, connection_alias="search")
        context = replace(search_context, connection=client)
    else:
        dialect.features = enabled
        context = search_context
    expression.get_result(context)

    disabled = enabled.replace(text_search_operations=enabled.text_search_operations - {operation})
    if with_connection:
        client.features = disabled
    else:
        dialect.features = disabled
    with pytest.raises(UnSupportedError, match=operation.value):
        expression.get_result(context)

    # A plain query remains available without any optional operation.
    SearchQuery("hare").get_result(context)


def test_configuration_support_does_not_enable_vector_operations(search_context):
    search_context.dialect.features = search_context.dialect.features.replace(text_search_operations=frozenset())
    SearchQuery("hare", config="english").get_result(search_context)
    with pytest.raises(UnSupportedError, match="vector"):
        SearchVector("title").get_result(search_context)
