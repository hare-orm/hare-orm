import pytest

from hare.core.hare_context import HareContext
from hare.dialects.dialect_registry import DialectRegistry
from hare.dialects.postgresql.fields.ts_vector_field import TSVectorField
from hare.exceptions import ConfigurationError
from tests.dialects.postgresql.models_tsvector import TSVectorEntry


@pytest.mark.database_independent
def test_generated_tsvector_without_config_raises():
    """A GENERATED ALWAYS AS (...) STORED column requires an IMMUTABLE expression, but
    Postgres's 1-argument TO_TSVECTOR(field) - the fallback used when config is None - is only
    STABLE (it implicitly reads the default_text_search_config GUC). This configuration can
    never work, so it must be rejected at construction time, not left to surface later as a
    cryptic 'generation expression is not immutable' error from generate_schemas()."""
    with pytest.raises(ConfigurationError):
        TSVectorField(source_fields=("title", "body"), weights=("A", "B"))


@pytest.mark.database_independent
def test_a_generated_tsvector_without_source_fields_names_them():
    with pytest.raises(ConfigurationError, match="require source_fields"):
        TSVectorField(generated=True, config="english")


@pytest.mark.database_independent
def test_generated_tsvector_with_explicit_config_succeeds():
    field = TSVectorField(source_fields=("title", "body"), config="english", weights=("A", "B"))
    assert field.config == "english"
    assert field.stored is True


@pytest.mark.asyncio
async def test_tsvector_generated_sql(db_tsvector):
    """Test TSVector field generates correct SQL."""
    field = HareContext.require_current().apps.get_binding(TSVectorEntry).fields_map["search_vector"]
    sql = field.get_generated_sql(DialectRegistry.get_dialect("postgresql"))
    assert sql == (
        "GENERATED ALWAYS AS ((SETWEIGHT(TO_TSVECTOR(CAST('english' AS regconfig),COALESCE(\"title\",'')),'A')"
        " || SETWEIGHT(TO_TSVECTOR(CAST('english' AS regconfig),COALESCE(\"body\",'')),'B'))) STORED"
    )


@pytest.mark.asyncio
async def test_tsvector_generated_field_save(db_tsvector):
    """Test TSVector generated fields can be returned after insert.

    Asserts the actual decoded lexeme text, not just "is a non-empty str" - a driver that
    silently hex-encodes the raw wire bytes instead of decoding them (confirmed as a real,
    live bug on rust_pg - see test_tsvector_field_decodes_readable_lexeme_text_not_raw_hex
    below) would still pass a bare truthiness/isinstance check, since the hex garbage is
    itself a non-empty string.
    """
    entry = await TSVectorEntry.objects.create(title="Hare ORM", body="Async Python ORM")

    assert isinstance(entry.search_vector, str)
    assert entry.search_vector
    assert "'hare':1A" in entry.search_vector


@pytest.mark.asyncio
async def test_tsvector_field_decodes_readable_lexeme_text_not_raw_hex(db_tsvector):
    """rust_pg had no dedicated binary-wire decoder for TSVECTOR (OID 3614) at all - it fell
    through to the generic "unknown type" fallback, which hex-encodes any column whose raw
    binary bytes aren't valid UTF-8 (tsvector's real wire format essentially never is, since it
    starts with a 4-byte lexeme count). Every tsvector value came back as an opaque hex string
    on every read - not a cache-hit-specific regression, just always wrong. Checked against
    both a plain field fetch and an annotation, since a Case()/Coalesce() annotation resolves
    through a separate from_db_value() call path than plain model hydration."""
    from hare.query.expressions import Case, F, When

    entry = await TSVectorEntry.objects.create(title="cat sat", body="on the mat")

    refreshed = await TSVectorEntry.objects.get(pk=entry.pk)
    assert refreshed.search_vector == "'cat':1A 'mat':5B 'sat':2A"

    annotated = (
        await TSVectorEntry.objects.filter(pk=entry.pk)
        .annotate(x=Case(When(id__lt=0, then=F("search_vector")), default=F("search_vector")))
        .values("x")
    )
    assert annotated[0]["x"] == "'cat':1A 'mat':5B 'sat':2A"


@pytest.mark.asyncio
async def test_an_empty_tsvector_in_an_array_is_read_back(db_tsvector):
    """asyncpg's text array parser crashed the process on an empty tsvector in an array."""
    connection = HareContext.require_current().apps.get_queryset(TSVectorEntry).get_connection()
    rows = await connection.execute_dicts("SELECT ARRAY['', $1::tsvector] AS vectors", ["a:1A b\\\\'c:2,3"])
    assert rows[0]["vectors"] == ["", "'a':1A 'b\\\\''c':2,3"]
