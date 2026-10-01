import os

import pytest

from hare.dialects.postgresql.fields.search import TSVectorField
from hare.dialects.registry import DialectRegistry
from hare.exceptions import ConfigurationError, UnSupportedError
from tests.dialects.postgresql.models_tsvector import TSVectorEntry


def test_sql_type_raises_unsupported_error_for_non_postgres_dialect():
    """TSVECTOR is a Postgres-only column type - resolving it for another dialect's DDL (e.g. a
    model with a TSVectorField that ends up on a SQLite connection) must raise a clear
    ConfigurationError instead of silently handing back Postgres syntax for schema generation to
    choke on, or worse, partially accept."""
    field = TSVectorField()
    field.model_field_name = "search_vector"
    with pytest.raises(UnSupportedError, match="TSVectorField.*search_vector.*sqlite"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))


def test_sql_type_still_resolves_for_postgres_dialect():
    field = TSVectorField()
    field.model_field_name = "search_vector"
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == "TSVECTOR"


def test_generated_tsvector_without_config_raises():
    """A GENERATED ALWAYS AS (...) STORED column requires an IMMUTABLE expression, but
    Postgres's 1-argument TO_TSVECTOR(field) - the fallback used when config is None - is only
    STABLE (it implicitly reads the default_text_search_config GUC). This configuration can
    never work, so it must be rejected at construction time, not left to surface later as a
    cryptic 'generation expression is not immutable' error from generate_schemas()."""
    with pytest.raises(ConfigurationError):
        TSVectorField(source_fields=("title", "body"), weights=("A", "B"))


def test_generated_tsvector_with_explicit_config_succeeds():
    field = TSVectorField(source_fields=("title", "body"), config="english", weights=("A", "B"))
    assert field.config == "english"
    assert field.stored is True


def skip_if_not_postgres():
    """Skip test if not running against PostgreSQL."""
    db_url = os.getenv("HARE_TEST_DB", "")
    # Strip a "+driver" scheme suffix (postgresql+asyncpg://, postgresql://) before comparing -
    # see hare/backends/base/config_generator.py's own handling of that same suffix.
    scheme = db_url.split(":", 1)[0].split("+", 1)[0]
    if scheme not in {"postgres", "postgresql", "asyncpg", "psycopg"}:
        pytest.skip("Postgres-only test.")


@pytest.mark.asyncio
async def test_tsvector_generated_sql(db_tsvector):
    """Test TSVector field generates correct SQL."""
    skip_if_not_postgres()
    field = TSVectorEntry._meta.fields_map["search_vector"]
    sql = field.get_generated_sql(DialectRegistry.get_dialect("postgresql"))
    assert sql == (
        "GENERATED ALWAYS AS (SETWEIGHT(TO_TSVECTOR('english',COALESCE(\"title\", '')),'A')"
        " || SETWEIGHT(TO_TSVECTOR('english',COALESCE(\"body\", '')),'B')) STORED"
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
    skip_if_not_postgres()
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
    skip_if_not_postgres()
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
