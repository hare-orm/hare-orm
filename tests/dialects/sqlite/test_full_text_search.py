"""SQLite full-text search: FullTextIndex's FTS5 table kept in step with its table, ``__search``,
SearchQuery's search types, SearchRank and SearchHeadline."""

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context
from hare.dialects.sqlite.indexes import FullTextIndex
from hare.dialects.sqlite.search import SqliteTextSearch
from hare.dialects.sqlite.search.sqlite_full_text_query import SqliteFullTextQuery
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.query.expressions import F, Q
from hare.query.functions import Upper
from hare.search import SearchHeadline, SearchQuery, SearchRank, SearchType, SearchVector
from tests.dialects.sqlite.models_full_text import SearchArticle, SearchComment, UnindexedNote

MODULES = ["tests.dialects.sqlite.models_full_text"]


@pytest_asyncio.fixture
async def search_db():
    async with hare_test_context(MODULES, db_url="sqlite+aiosqlite://:memory:") as context:
        yield context


async def create_articles() -> tuple[SearchArticle, SearchArticle, SearchArticle]:
    orm = await SearchArticle.objects.create(title="Hare ORM", body="An async object mapper for Python", rating=3)
    rabbits = await SearchArticle.objects.create(
        title="Rabbits", body="Running hares and rabbits in the field", rating=5
    )
    empty = await SearchArticle.objects.create(title="Nothing here", body=None, rating=1)
    return orm, rabbits, empty


async def get_ids(queryset) -> list[int]:
    return sorted(await queryset.values_list("id", flat=True))


@pytest.mark.asyncio
async def test_the_index_is_an_fts5_table_with_triggers(search_db):
    index_name = SearchArticle._meta.indexes[0].get_table_name(SearchArticle)
    _, rows = await SearchArticle.get_connection().execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name LIKE ? ORDER BY type, name", [f"{index_name}%"]
    )
    table_rows = [row for row in rows if row["type"] == "table" and "VIRTUAL" in row["sql"]]
    trigger_names = [row["name"] for row in rows if row["type"] == "trigger"]
    assert len(table_rows) == 1
    table_sql = table_rows[0]["sql"]
    assert "USING fts5" in table_sql
    assert "content='search_article'" in table_sql
    assert "content_rowid='id'" in table_sql
    assert "tokenize='porter unicode61'" in table_sql
    assert table_rows[0]["name"] == index_name
    assert sorted(trigger_names) == sorted(f"{index_name}__{event}" for event in ("insert", "delete", "update"))


@pytest.mark.asyncio
async def test_search_matches_a_word_in_the_field(search_db):
    orm, rabbits, _ = await create_articles()
    assert await get_ids(SearchArticle.objects.filter(title__search="orm")) == [orm.id]
    # porter stems "hares" and "hare" alike - only in the body column of the rabbits.
    assert await get_ids(SearchArticle.objects.filter(body__search="hare")) == [rabbits.id]
    assert await get_ids(SearchArticle.objects.filter(title__search="hares")) == [orm.id]
    assert await get_ids(SearchArticle.objects.filter(body__search="python rabbits")) == []
    assert await get_ids(SearchArticle.objects.filter(body__search="async python")) == [orm.id]


@pytest.mark.asyncio
async def test_search_follows_inserts_updates_and_deletes(search_db):
    orm, rabbits, empty = await create_articles()
    empty.body = "a hare finally"
    await empty.save()
    assert await get_ids(SearchArticle.objects.filter(body__search="hare")) == [rabbits.id, empty.id]
    await rabbits.delete()
    assert await get_ids(SearchArticle.objects.filter(body__search="hare")) == [empty.id]
    await SearchArticle.objects.filter(id=orm.id).update(title="Turtles")
    assert await get_ids(SearchArticle.objects.filter(title__search="orm")) == []
    assert await get_ids(SearchArticle.objects.filter(title__search="turtle")) == [orm.id]
    await SearchArticle.objects.bulk_create([SearchArticle(title="Bulk hare", body="")])
    assert len(await get_ids(SearchArticle.objects.filter(title__search="hare"))) == 1


@pytest.mark.asyncio
async def test_search_combines_with_other_filters_and_negation(search_db):
    orm, rabbits, empty = await create_articles()
    assert await get_ids(SearchArticle.objects.exclude(body__search="hare")) == [orm.id, empty.id]
    assert await get_ids(SearchArticle.objects.filter(Q(title__search="orm") | Q(body__search="rabbit"))) == [
        orm.id,
        rabbits.id,
    ]
    assert await get_ids(SearchArticle.objects.filter(body__search="hare", rating__gt=4)) == [rabbits.id]
    assert await SearchArticle.objects.filter(body__search="hare").count() == 1


@pytest.mark.asyncio
async def test_search_text_is_never_read_as_query_syntax(search_db):
    orm, *_ = await create_articles()
    assert await get_ids(SearchArticle.objects.filter(title__search='orm" OR "rabbits')) == []
    assert await get_ids(SearchArticle.objects.filter(title__search="orm*")) == [orm.id]
    assert await get_ids(SearchArticle.objects.filter(title__search="")) == []
    assert await get_ids(SearchArticle.objects.filter(title__search="- ! ?")) == []
    assert await get_ids(SearchArticle.objects.filter(title__search="NOT orm")) == []


@pytest.mark.asyncio
async def test_search_query_types(search_db):
    orm, rabbits, _ = await create_articles()
    phrase = SearchQuery("hares and rabbits", search_type=SearchType.PHRASE)
    assert await get_ids(SearchArticle.objects.filter(body__search=phrase)) == [rabbits.id]
    reversed_phrase = SearchQuery("rabbits and hares", search_type="phrase")
    assert await get_ids(SearchArticle.objects.filter(body__search=reversed_phrase)) == []
    websearch = SearchQuery("python or rabbits", search_type=SearchType.WEBSEARCH)
    assert await get_ids(SearchArticle.objects.filter(body__search=websearch)) == [orm.id, rabbits.id]
    excluding = SearchQuery('"running hares" -python', search_type=SearchType.WEBSEARCH)
    assert await get_ids(SearchArticle.objects.filter(body__search=excluding)) == [rabbits.id]
    raw = SearchQuery("pyth* OR rabb*", search_type=SearchType.RAW)
    assert await get_ids(SearchArticle.objects.filter(body__search=raw)) == [orm.id, rabbits.id]


@pytest.mark.asyncio
async def test_search_across_a_relation(search_db):
    orm, rabbits, _ = await create_articles()
    await SearchComment.objects.create(article=orm, text="great mapper")
    await SearchComment.objects.create(article=rabbits, text="cute animals")
    assert await get_ids(SearchComment.objects.filter(article__title__search="orm")) == [1]
    assert await get_ids(SearchArticle.objects.filter(comments__text__search="animals")) == [rabbits.id]


@pytest.mark.asyncio
async def test_search_rank_orders_by_relevance(search_db):
    orm, rabbits, empty = await create_articles()
    ranked = await (
        SearchArticle.objects.annotate(rank=SearchRank(("title", "body"), "hare"))
        .filter(rank__gt=0)
        .order_by("-rank")
        .values_list("id", "rank")
    )
    assert [article_id for article_id, _ in ranked] in ([orm.id, rabbits.id], [rabbits.id, orm.id])
    assert all(rank > 0 for _, rank in ranked)
    unmatched = await SearchArticle.objects.annotate(rank=SearchRank("title", "hare")).get(id=empty.id)
    assert unmatched.rank == 0
    # Weighted, a title match outranks a body match.
    weighted = await (
        SearchArticle.objects.annotate(
            rank=SearchRank(("title", "body"), "hare", weights={"title": 10.0, "body": 0.1})
        )
        .filter(rank__gt=0)
        .order_by("-rank")
        .values_list("id", flat=True)
    )
    assert weighted == [orm.id, rabbits.id]
    body_only = await (
        SearchArticle.objects.annotate(rank=SearchRank("body", "hare")).filter(rank__gt=0).values_list("id", flat=True)
    )
    assert body_only == [rabbits.id]


@pytest.mark.asyncio
async def test_search_rank_arguments_are_checked():
    with pytest.raises(ConfigurationError, match="weight"):
        SearchRank("title", "hare", weights={"title": -1})
    with pytest.raises(ConfigurationError, match="weight"):
        SearchRank("title", "hare", weights={"title": float("nan")})
    with pytest.raises(ConfigurationError, match="weight"):
        SearchRank("title", "hare", weights={"title": True})
    with pytest.raises(ConfigurationError, match="fields"):
        SearchRank((), "hare")
    with pytest.raises(ConfigurationError, match="search_type"):
        SearchQuery("hare", search_type="fuzzy")


@pytest.mark.asyncio
async def test_search_rank_weights_must_name_index_fields(search_db):
    await create_articles()
    with pytest.raises(ConfigurationError, match="rating"):
        await SearchArticle.objects.annotate(rank=SearchRank("title", "hare", weights={"rating": 2.0}))


@pytest.mark.asyncio
async def test_search_headline_marks_matches(search_db):
    orm, rabbits, empty = await create_articles()
    headlines = dict(
        await SearchArticle.objects.annotate(headline=SearchHeadline("body", "hare rabbit")).values_list(
            "id", "headline"
        )
    )
    assert headlines[rabbits.id] == "Running <b>hares</b> and <b>rabbits</b> in the field"
    assert headlines[orm.id] == "An async object mapper for Python"
    assert headlines[empty.id] is None
    snippets = dict(
        await SearchArticle.objects.annotate(
            headline=SearchHeadline(
                "body", "rabbits", start_sel="[", stop_sel="]", max_words=3, fragment_delimiter="~"
            )
        ).values_list("id", "headline")
    )
    assert "[rabbits]" in snippets[rabbits.id]
    assert "~" in snippets[rabbits.id]


@pytest.mark.asyncio
async def test_search_headline_arguments_are_checked():
    with pytest.raises(ConfigurationError, match="max_words"):
        SearchHeadline("body", "hare", max_words=0)
    with pytest.raises(ConfigurationError, match="start_sel"):
        SearchHeadline("body", "hare", start_sel=1)


@pytest.mark.asyncio
async def test_search_without_a_full_text_index_is_refused_before_sql(search_db):
    with pytest.raises(UnSupportedError, match="FullTextIndex"):
        await UnindexedNote.objects.filter(text__search="hare")
    with pytest.raises(UnSupportedError, match="FullTextIndex"):
        await SearchArticle.objects.filter(Q(rating__search="3"))
    with pytest.raises(UnSupportedError, match="FullTextIndex"):
        await UnindexedNote.objects.annotate(rank=SearchRank("text", "hare"))
    with pytest.raises(UnSupportedError, match="annotation"):
        await SearchArticle.objects.annotate(upper_title=Upper("title")).filter(upper_title__search="hare")


@pytest.mark.asyncio
async def test_search_on_a_field_annotation_searches_the_field(search_db):
    orm, *_ = await create_articles()
    assert await get_ids(SearchArticle.objects.annotate(heading=F("title")).filter(heading__search="orm")) == [orm.id]


@pytest.mark.asyncio
async def test_a_connection_without_fts5_refuses_search_expressions(search_db):
    connection = SearchArticle.get_connection()
    features = connection.features
    connection.features = features.replace(supports_full_text_index=False)
    try:
        with pytest.raises(UnSupportedError, match="supports_full_text_index"):
            await SearchArticle.objects.annotate(rank=SearchRank("title", "hare"))
        with pytest.raises(UnSupportedError, match="supports_full_text_index"):
            await SearchArticle.objects.annotate(headline=SearchHeadline("title", "hare"))
    finally:
        connection.features = features


def test_full_text_index_arguments_are_checked():
    with pytest.raises(ConfigurationError, match="fields"):
        FullTextIndex(fields=())
    with pytest.raises(ConfigurationError, match="fields"):
        FullTextIndex(fields="title")
    with pytest.raises(ConfigurationError, match="key order"):
        FullTextIndex(fields=("-title",))
    with pytest.raises(ConfigurationError, match="tokenizer"):
        FullTextIndex(fields=("title",), tokenizer="")
    with pytest.raises(ConfigurationError, match="tokenizer"):
        FullTextIndex(fields=("title",), tokenizer=3)


def test_full_text_index_deconstructs_with_its_tokenizer():
    index = FullTextIndex(fields=("title", "body"), tokenizer="trigram", name="article_text")
    path, args, kwargs = index.deconstruct()
    assert path == "hare.dialects.sqlite.indexes.FullTextIndex"
    assert args == []
    assert kwargs == {"fields": ["title", "body"], "name": "article_text", "tokenizer": "trigram"}
    assert FullTextIndex(**kwargs) == index
    assert FullTextIndex(fields=("title",), tokenizer="trigram") != FullTextIndex(fields=("title",))


@pytest.mark.parametrize(
    ("text", "search_type", "column_filter", "expected"),
    [
        ("Hare ORM", "plain", "", '"Hare" "ORM"'),
        ("hare, orm!", "plain", '{"title"}', '{"title"} : ("hare" "orm")'),
        ('say "hi" NOT', "plain", "", '"say" "hi" "NOT"'),
        ("", "plain", "", '""'),
        (None, "plain", "", '""'),
        ("hares and rabbits", "phrase", "", '"hares and rabbits"'),
        ("...", "phrase", "", '""'),
        ("python or rabbits", "websearch", "", '("python") OR ("rabbits")'),
        ('"running hares" -python cute', "websearch", "", '("running hares" AND "cute" NOT "python")'),
        ("-python", "websearch", "", '""'),
        ("pyth* OR rabb*", "raw", "", "pyth* OR rabb*"),
        ("  ", "raw", "", '""'),
    ],
)
def test_search_text_becomes_an_fts5_query(text, search_type, column_filter, expected):
    assert SqliteFullTextQuery.get_query(text, search_type, column_filter) == expected


def test_search_query_column_filter_quotes_names():
    assert SqliteTextSearch.get_column_filter(["title", 'odd"name']) == '{"title" "odd""name"}'


@pytest.mark.asyncio
async def test_search_headline_max_words_is_fts5_snippets_limit(search_db):
    await create_articles()
    with pytest.raises(ConfigurationError, match="max_words"):
        await SearchArticle.objects.annotate(headline=SearchHeadline("body", "hare", max_words=65))


@pytest.mark.asyncio
async def test_search_queries_combine(search_db):
    orm, rabbits, _ = await create_articles()
    either = SearchQuery("python") | SearchQuery("rabbits")
    assert await get_ids(SearchArticle.objects.filter(body__search=either)) == [orm.id, rabbits.id]
    both = SearchQuery("running") & SearchQuery("rabbits")
    assert await get_ids(SearchArticle.objects.filter(body__search=both)) == [rabbits.id]
    without = SearchQuery("hares") & ~SearchQuery("python")
    assert await get_ids(SearchArticle.objects.filter(body__search=without)) == [rabbits.id]
    nested = (SearchQuery("async") & SearchQuery("mapper")) | SearchQuery("field", search_type=SearchType.PHRASE)
    assert await get_ids(SearchArticle.objects.filter(body__search=nested)) == [orm.id, rabbits.id]
    empty_side = SearchQuery("") | SearchQuery("python")
    assert await get_ids(SearchArticle.objects.filter(body__search=empty_side)) == [orm.id]
    ranked = await (
        SearchArticle.objects.annotate(rank=SearchRank(("title", "body"), SearchQuery("orm") | SearchQuery("rabbits")))
        .filter(rank__gt=0)
        .values_list("id", flat=True)
    )
    assert sorted(ranked) == [orm.id, rabbits.id]


@pytest.mark.asyncio
async def test_search_negation_only_on_the_right_of_and(search_db):
    await create_articles()
    with pytest.raises(UnSupportedError, match="right side of &"):
        await SearchArticle.objects.filter(body__search=~SearchQuery("hare"))
    with pytest.raises(UnSupportedError, match="right side of &"):
        await SearchArticle.objects.filter(body__search=SearchQuery("hare") | ~SearchQuery("python"))
    with pytest.raises(UnSupportedError, match="right side of &"):
        await SearchArticle.objects.filter(body__search=~(SearchQuery("hare") & SearchQuery("python")))


@pytest.mark.asyncio
async def test_search_rank_takes_a_search_vector_of_fields(search_db):
    orm, rabbits, _ = await create_articles()
    ranked = await (
        SearchArticle.objects.annotate(rank=SearchRank(SearchVector("title", "body"), "hare"))
        .filter(rank__gt=0)
        .values_list("id", flat=True)
    )
    assert sorted(ranked) == [orm.id, rabbits.id]


@pytest.mark.asyncio
async def test_text_search_configuration_arguments_are_refused_before_sql(search_db):
    await create_articles()
    feature = "supports_text_search_configurations"
    with pytest.raises(UnSupportedError, match=feature):
        await SearchArticle.objects.filter(body__search=SearchQuery("hare", config="english"))
    with pytest.raises(UnSupportedError, match=feature):
        await SearchArticle.objects.annotate(vector=SearchVector("title"))
    with pytest.raises(UnSupportedError, match=feature):
        await SearchArticle.objects.annotate(rank=SearchRank("title", "hare", weights=[0.1, 0.2, 0.4, 1.0]))
    with pytest.raises(UnSupportedError, match=feature):
        await SearchArticle.objects.annotate(rank=SearchRank("title", "hare", cover_density=True))
    with pytest.raises(UnSupportedError, match=feature):
        await SearchArticle.objects.annotate(headline=SearchHeadline("title", "hare", max_fragments=2))
    with pytest.raises(UnSupportedError, match="field name"):
        await SearchArticle.objects.annotate(headline=SearchHeadline(Upper("title"), "hare"))
    with pytest.raises(UnSupportedError, match="field names"):
        await SearchArticle.objects.annotate(rank=SearchRank(SearchVector("title", weight="A"), "hare"))
