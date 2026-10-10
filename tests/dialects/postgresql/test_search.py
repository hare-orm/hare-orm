import os

import pytest

from hare import Connections
from hare.contrib.test import requires_features
from hare.dialects.postgresql.search import Lexeme
from hare.search import SearchHeadline, SearchQuery, SearchRank, SearchVector
from tests.dialects.postgresql.models_tsvector import (
    GeneratedTSVectorEntry,
    TSVectorAuthor,
    TSVectorBook,
    TSVectorEntry,
    TSVectorNumberEntry,
)
from tests.testmodels import TextFields


def skip_if_not_postgres():
    """Skip test if not running against PostgreSQL."""
    db_url = os.getenv("HARE_TEST_DB", "")
    # Strip a "+driver" scheme suffix (postgresql+asyncpg://, postgresql://) before comparing -
    # see hare/backends/base/config_generator.py's own handling of that same suffix.
    scheme = db_url.split(":", 1)[0].split("+", 1)[0]
    if scheme not in {"postgres", "postgresql", "asyncpg", "psycopg"}:
        pytest.skip("Postgres-only test.")


def assert_sql(db, sql: str, expected_psycopg: str, expected_asyncpg: str) -> None:
    """Assert SQL matches the expected asyncpg output (the only postgres driver left)."""
    assert sql == expected_asyncpg


# =============================================================================
# TestPostgresSearchExpressions - uses standard testmodels (test.TestCase equivalent)
# =============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_vector(db_postgres):
    """Test SearchVector expression."""
    db = Connections.get("models")
    sql = TextFields.objects.all().annotate(search=SearchVector("text")).values("search").sql()
    # Wrapped in COALESCE(..., '') even for a single field - Postgres `TO_TSVECTOR(NULL)` is
    # itself NULL, so a NULL-valued field would otherwise make the whole row unsearchable.
    assert_sql(
        db,
        sql,
        'SELECT TO_TSVECTOR(COALESCE("text",%s)) "search" FROM "textfields"',
        'SELECT TO_TSVECTOR(COALESCE("text",$1)) "search" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_vector_config_weight(db_postgres):
    """Test SearchVector with config and weight.

    The weight letter renders as an inline SQL literal ('A'), not a bind parameter ($N/%s) -
    setweight(tsvector, "char")'s second argument is Postgres's internal "char" type, which
    asyncpg can't bind a plain str parameter as (see test_search_vector_weight_executes_on_both_
    drivers below for the live crash this avoids)."""
    db = Connections.get("models")
    sql = (
        TextFields.objects.all()
        .annotate(search=SearchVector("text", config="english", weight="A"))
        .values("search")
        .sql()
    )
    assert_sql(
        db,
        sql,
        'SELECT SETWEIGHT(TO_TSVECTOR(%s,COALESCE("text",%s)),\'A\') "search" FROM "textfields"',
        'SELECT SETWEIGHT(TO_TSVECTOR($1,COALESCE("text",$2)),\'A\') "search" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_vector_weight_executes_on_both_drivers(db_postgres):
    """setweight(tsvector, "char")'s second argument is Postgres's internal single-byte "char"
    type, not text - asyncpg binds a plain str bind parameter as text and rejects it outright
    ("a bytes-like object is required, not 'str'"), confirmed live before this fix crashed on
    EVERY use of SearchVector(weight=...), on asyncpg specifically (rust_pg was unaffected).
    Combining two weighted vectors with `+` (a common real usage, e.g. title=A/body=B) also
    exercises CombinedSearchVector."""
    await TextFields.objects.create(text="hello world", text_null=None)

    combined = SearchVector("text", weight="A") + SearchVector("text", weight="B")
    row = await TextFields.objects.all().annotate(vec=combined).values("vec")
    assert row == [{"vec": "'hello':1A,3B 'world':2A,4B"}]

    with pytest.raises(ValueError, match="not a valid TsWeight"):
        await TextFields.objects.all().annotate(vec=SearchVector("text", weight="Z")).values("vec")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_query_types(db_postgres):
    """Test SearchQuery with different search types."""
    db = Connections.get("models")
    sql = TextFields.objects.all().annotate(query=SearchQuery("fat", search_type="phrase")).values("query").sql()
    assert_sql(
        db,
        sql,
        'SELECT PHRASETO_TSQUERY(%s) "query" FROM "textfields"',
        'SELECT PHRASETO_TSQUERY($1) "query" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_query_combine_and_invert(db_postgres):
    """Test SearchQuery combine and invert operations."""
    db = Connections.get("models")
    query = SearchQuery("fat") & SearchQuery("rat")
    sql = TextFields.objects.all().annotate(query=query).values("query").sql()
    assert_sql(
        db,
        sql,
        'SELECT (PLAINTO_TSQUERY(%s) && PLAINTO_TSQUERY(%s)) "query" FROM "textfields"',
        'SELECT (PLAINTO_TSQUERY($1) && PLAINTO_TSQUERY($2)) "query" FROM "textfields"',
    )

    sql = TextFields.objects.all().annotate(query=SearchQuery("fat", invert=True)).values("query").sql()
    assert_sql(
        db,
        sql,
        'SELECT !!(PLAINTO_TSQUERY(%s)) "query" FROM "textfields"',
        'SELECT !!(PLAINTO_TSQUERY($1)) "query" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_combined_search_query_invert(db_postgres):
    """~ on a CombinedSearchQuery (the result of combining two SearchQuery instances with |/&)
    used to raise TypeError - only the bare SearchQuery it's built from had its own
    __invert__, CombinedSearchQuery itself had none at all."""
    db = Connections.get("models")
    query = ~(SearchQuery("fat") & SearchQuery("rat"))
    sql = TextFields.objects.all().annotate(query=query).values("query").sql()
    assert_sql(
        db,
        sql,
        'SELECT !!((PLAINTO_TSQUERY(%s) && PLAINTO_TSQUERY(%s))) "query" FROM "textfields"',
        'SELECT !!((PLAINTO_TSQUERY($1) && PLAINTO_TSQUERY($2))) "query" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_query_lexeme(db_postgres):
    """Test SearchQuery with Lexeme."""
    db = Connections.get("models")
    lexeme_query = SearchQuery(Lexeme("fat") & Lexeme("rat"))
    sql = TextFields.objects.all().annotate(query=lexeme_query).values("query").sql()
    assert_sql(
        db,
        sql,
        'SELECT TO_TSQUERY(%s) "query" FROM "textfields"',
        'SELECT TO_TSQUERY($1) "query" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_rank(db_postgres):
    """Test SearchRank expression."""
    db = Connections.get("models")
    sql = (
        TextFields.objects.all()
        .annotate(rank=SearchRank(SearchVector("text"), SearchQuery("fat")))
        .values("rank")
        .sql()
    )
    assert_sql(
        db,
        sql,
        'SELECT TS_RANK(TO_TSVECTOR(COALESCE("text",%s)),PLAINTO_TSQUERY(%s)) "rank" FROM "textfields"',
        'SELECT TS_RANK(TO_TSVECTOR(COALESCE("text",$1)),PLAINTO_TSQUERY($2)) "rank" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_rank_accepts_a_bare_string_naming_an_already_stored_tsvector_field(db_tsvector):
    """SearchRank("vector_field_name", ...) unconditionally wrapped a bare-string vector= argument
    in SearchVector(...), which builds TO_TSVECTOR(...) - correct for a plain text column, but
    Postgres has no to_tsvector(tsvector) overload, so naming an ALREADY-stored TSVectorField
    column this way (the natural way to reference one) raised "function to_tsvector(tsvector)
    does not exist" instead of ranking against the column directly. Matches
    postgres_search()'s own is_tsvector check for the identical .filter(field__search=...) shape."""
    await TSVectorEntry.objects.create(title="Hare ORM", body="Async Python ORM framework")
    await TSVectorEntry.objects.create(title="Unrelated", body="Nothing to do with the query")

    ranked = await (
        TSVectorEntry.objects.filter(search_vector__isnull=False)
        .annotate(rank=SearchRank("search_vector", SearchQuery("python")))
        .filter(rank__gt=0)
        .values("title", "rank")
    )

    assert [row["title"] for row in ranked] == ["Hare ORM"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_rank_accepts_a_relation_crossing_name_of_an_already_stored_tsvector_field(db_tsvector):
    """Sibling of the bare-name test above, but for a name crossing a relation
    ("author__bio_vector") - _meta.fields_map (a plain dict of THIS model's own direct field
    names) has no entries for a dotted path, so the bare-name-only guard silently missed this
    case entirely, always falling through to SearchVector(...) and building a doubly-wrapped,
    Postgres-rejected TO_TSVECTOR(<tsvector column>)."""
    author = await TSVectorAuthor.objects.create(bio="Async Python ORM framework")
    await TSVectorAuthor.objects.create(bio="Nothing to do with the query")
    await TSVectorBook.objects.create(title="Hare ORM", author=author)

    ranked = await (
        TSVectorBook.objects.all()
        .annotate(rank=SearchRank("author__bio_vector", SearchQuery("python")))
        .filter(rank__gt=0)
        .values("title", "rank")
    )

    assert [row["title"] for row in ranked] == ["Hare ORM"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_rank_accepts_a_generated_field_wrapped_tsvector(db_tsvector):
    """GeneratedField.output_field is composed in, not inherited from - isinstance(terminal_
    field, TSVectorField) was False for GeneratedField(output_field=TSVectorField()) even though
    the generated column IS a tsvector at the SQL level, so SearchRank fell through to
    SearchVector(...) and built a doubly-wrapped, Postgres-rejected TO_TSVECTOR(<tsvector
    column>). Confirmed live before this fix."""
    await GeneratedTSVectorEntry.objects.create(title="Hare ORM", body="Async Python ORM framework")
    await GeneratedTSVectorEntry.objects.create(title="Unrelated", body="Nothing to do with the query")

    ranked = await (
        GeneratedTSVectorEntry.objects.all()
        .annotate(rank=SearchRank("search_vector", SearchQuery("python")))
        .filter(rank__gt=0)
        .values("title", "rank")
    )

    assert [row["title"] for row in ranked] == ["Hare ORM"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_lookup_on_a_generated_field_wrapped_tsvector(db_tsvector):
    """Sibling gap to the SearchRank fix above, in get_filters_for_field()'s own `__search`
    lookup registration - the ONE spot in that function that read the raw (possibly
    GeneratedField-wrapped) `field` instead of the already-unwrapped `effective_field` every
    other branch there uses. Confirmed live before this fix: `.filter(search_vector__search=
    ...)` on a GeneratedField(output_field=TSVectorField()) column raised the same doubly-
    wrapped "function to_tsvector(tsvector) does not exist"."""
    await GeneratedTSVectorEntry.objects.create(title="Hare ORM", body="Async Python ORM framework")
    await GeneratedTSVectorEntry.objects.create(title="Unrelated", body="Nothing to do with the query")

    matched = await GeneratedTSVectorEntry.objects.filter(search_vector__search="python").values("title")
    assert [row["title"] for row in matched] == ["Hare ORM"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_headline(db_postgres):
    """Test SearchHeadline expression."""
    db = Connections.get("models")
    sql = (
        TextFields.objects.all()
        .annotate(
            headline=SearchHeadline(
                "text",
                SearchQuery("fat"),
                start_sel="<b>",
                stop_sel="</b>",
            )
        )
        .values("headline")
        .sql()
    )
    assert_sql(
        db,
        sql,
        'SELECT TS_HEADLINE("text",PLAINTO_TSQUERY(%s),%s) "headline" FROM "textfields"',
        'SELECT TS_HEADLINE("text",PLAINTO_TSQUERY($1),$2) "headline" FROM "textfields"',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_lookup_text(db_postgres):
    """Test search lookup on text field."""
    db = Connections.get("models")
    sql = TextFields.objects.filter(text__search="fat").values("id").sql()
    assert_sql(
        db,
        sql,
        'SELECT "id" "id" FROM "textfields" WHERE TO_TSVECTOR("text") @@ PLAINTO_TSQUERY(%s)',
        'SELECT "id" "id" FROM "textfields" WHERE TO_TSVECTOR("text") @@ PLAINTO_TSQUERY($1)',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_lookup_text_searchquery(db_postgres):
    """Test search lookup with SearchQuery on text field."""
    db = Connections.get("models")
    sql = TextFields.objects.filter(text__search=SearchQuery("fat", search_type="raw")).values("id").sql()
    assert_sql(
        db,
        sql,
        'SELECT "id" "id" FROM "textfields" WHERE TO_TSVECTOR("text") @@ TO_TSQUERY(%s)',
        'SELECT "id" "id" FROM "textfields" WHERE TO_TSVECTOR("text") @@ TO_TSQUERY($1)',
    )


# =============================================================================
# TestPostgresSearchLookupTSVector - uses TSVector models (IsolatedTestCase equivalent)
# =============================================================================


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_lookup_tsvector(db_search):
    """Test search lookup on TSVector field.

    TSVectorEntry.search_vector is configured with config="english" - the search lookup must
    pass that same config into PLAINTO_TSQUERY(), or a non-default-language column silently
    stops matching (the query text gets parsed under Postgres's session default_text_search_config
    instead of the config the stored vector was actually built with).
    """
    skip_if_not_postgres()
    db = Connections.get("models")
    sql = TSVectorEntry.objects.filter(search_vector__search="fat").values("id").sql()
    assert_sql(
        db,
        sql,
        'SELECT "id" "id" FROM "tsvector_entry" WHERE "search_vector" @@ PLAINTO_TSQUERY(%s,%s)',
        'SELECT "id" "id" FROM "tsvector_entry" WHERE "search_vector" @@ PLAINTO_TSQUERY($1,$2)',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_lookup_tsvector_searchquery(db_search):
    """Test search lookup with SearchQuery on TSVector field."""
    skip_if_not_postgres()
    db = Connections.get("models")
    sql = TSVectorEntry.objects.filter(search_vector__search=SearchQuery("fat", search_type="raw")).values("id").sql()
    assert_sql(
        db,
        sql,
        'SELECT "id" "id" FROM "tsvector_entry" WHERE "search_vector" @@ TO_TSQUERY(%s)',
        'SELECT "id" "id" FROM "tsvector_entry" WHERE "search_vector" @@ TO_TSQUERY($1)',
    )


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_vector_casts_a_non_text_source_to_text(db_tsvector):
    """COALESCE(<int column>, '') can't be typed - a non-text SearchVector source (and a generated
    TSVectorField's non-text source column) is cast to text first."""
    skip_if_not_postgres()
    await TSVectorNumberEntry.objects.create(id=1, title="cat", number=42, data={"key": "value"})

    vectors = await TSVectorNumberEntry.objects.annotate(
        number_vector=SearchVector("number", config="simple"),
        combined_vector=SearchVector("title", "number", config="simple"),
        data_vector=SearchVector("data", config="simple"),
    ).values("number_vector", "combined_vector", "data_vector")

    assert vectors == [
        {"number_vector": "'42':1", "combined_vector": "'42':2 'cat':1", "data_vector": "'key':1 'value':2"}
    ]
    assert await TSVectorNumberEntry.objects.filter(search_vector__search="42").values_list("id", flat=True) == [1]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_lookup_vectorizes_the_column_with_the_query_config(db_tsvector):
    """field__search=SearchQuery(..., config=X) on a text column vectorized the column with the
    session's default config, so a stemmed non-default-language query never matched."""
    skip_if_not_postgres()
    await TSVectorEntry.objects.create(id=1, title="Кошки бегают")
    await TSVectorEntry.objects.create(id=2, title="Собаки лают")

    queryset = TSVectorEntry.objects.filter(title__search=SearchQuery("кошка", config="russian")).values_list(
        "id", flat=True
    )

    assert await queryset == [1]
    assert 'TO_TSVECTOR($1,"title")' in queryset.sql() or 'TO_TSVECTOR(%s,"title")' in queryset.sql()
    combined = SearchQuery("кошка", config="russian") | SearchQuery("собака", config="russian")
    assert sorted(await TSVectorEntry.objects.filter(title__search=combined).values_list("id", flat=True)) == [1, 2]
    inverted = ~SearchQuery("кошка", config="russian")
    assert await TSVectorEntry.objects.filter(title__search=inverted).values_list("id", flat=True) == [2]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_lexeme_escapes_a_backslash(db_tsvector):
    """A backslash in a Lexeme escaped the closing quote of the raw tsquery, a syntax error."""
    skip_if_not_postgres()
    assert Lexeme("x\\")._as_tsquery() == "'x\\\\'"
    assert Lexeme("it's\\")._as_tsquery() == "'it''s\\\\'"
    await TSVectorEntry.objects.create(id=1, title="x")

    queries = await TSVectorEntry.objects.annotate(query=SearchQuery(Lexeme("x\\"), config="simple")).values_list(
        "query", flat=True
    )

    assert queries == ["'x'"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_vector_weight_expression_executes_on_both_drivers(db_tsvector):
    """A weight= Expression was bound as a "char" parameter, which asyncpg can't encode from a str."""
    from hare.query.expressions import Value

    skip_if_not_postgres()
    await TSVectorEntry.objects.create(id=1, title="cat")

    vectors = await TSVectorEntry.objects.annotate(
        vector=SearchVector("title", config="simple", weight=Value("A"))
    ).values_list("vector", flat=True)

    assert vectors == ["'cat':1A"]


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_search_query_annotation_reads_as_tsquery_text(db_tsvector):
    """rust_pg returned an annotated tsquery as its binary wire bytes."""
    skip_if_not_postgres()
    await TSVectorEntry.objects.create(id=1, title="cat")

    row = await TSVectorEntry.objects.annotate(
        plain=SearchQuery("fat cats", config="english"),
        raw=SearchQuery(Lexeme("cat", prefix=True, weight="A") & ~Lexeme("dog")),
    ).values("plain", "raw")

    assert row == [{"plain": "'fat' & 'cat'", "raw": "'cat':*A & !'dog'"}]
