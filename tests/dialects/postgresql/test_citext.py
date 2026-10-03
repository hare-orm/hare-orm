"""Tests for CitextField (hare.dialects.postgresql.fields.citext.CitextField).

Uses the db_citext fixture (tests/dialects/postgresql/conftest.py), which manually creates the
citext extension and runs generate_schemas() against tests.dialects.postgresql.models_citext -
same pattern as db_postgis for PostGISField.
"""

import os

import pytest

from hare.dialects.registry import DialectRegistry
from hare.exceptions import UnSupportedError, ValidationError
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_citext import CitextContact, CitextTagged
from tests.dialects.postgresql.models_generated_citext import GeneratedCitextContact


@pytest.mark.asyncio
async def test_column_type_is_citext(db_citext):
    """The generated column's DB type is citext, not text/varchar."""
    conn = db_citext.db()
    _, rows = await conn.execute(
        "SELECT udt_name FROM information_schema.columns WHERE table_name = 'citext_contact' AND column_name = 'email'"
    )
    assert dict(rows[0])["udt_name"] == "citext"


@pytest.mark.asyncio
async def test_stores_value_as_entered(db_citext):
    """citext preserves the original case on read, it only compares case-insensitively."""
    contact = await CitextContact.objects.create(email="Alice@Example.com")
    fetched = await CitextContact.objects.get(id=contact.id)
    assert fetched.email == "Alice@Example.com"


@pytest.mark.asyncio
async def test_exact_filter_is_case_insensitive_without_any_trick(db_citext):
    """Plain equality (`__exact`, no `i` prefix, no icontains/ILIKE/UPPER()) already matches
    regardless of case - the column's own CITEXT type does the folding, not the lookup."""
    await CitextContact.objects.create(email="Alice@Example.com")

    found = await CitextContact.objects.filter(email="ALICE@EXAMPLE.COM")
    assert len(found) == 1
    assert found[0].email == "Alice@Example.com"


@pytest.mark.asyncio
async def test_posix_regex_filter_ignores_case(db_citext):
    """citext's own `~` ignores case, as its equality and LIKE do - the column isn't cast to text."""
    await CitextContact.objects.create(id=1, email="Alice@Example.com")
    await CitextContact.objects.create(id=2, email="Ärger@X.de")

    assert await CitextContact.objects.filter(email__posix_regex="^ALI").values_list("id", flat=True) == [1]
    assert await CitextContact.objects.filter(email__posix_regex="^ärg").values_list("id", flat=True) == [2]
    assert await CitextContact.objects.filter(email__iposix_regex="^ali").values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_icontains_filter_matches_case_insensitively(db_citext):
    """`__icontains` matches a different-case substring - unsurprising on its own (icontains
    Upper()-folds both sides for any field type), but confirms CitextField works with the
    ordinary filter API end to end."""
    await CitextContact.objects.create(email="alice@example.com", name="Alice")

    found = await CitextContact.objects.filter(email__icontains="EXAMPLE")
    assert len(found) == 1
    assert found[0].name == "Alice"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored_email,filter_value",
    [
        ("привет@пример.рф", "ПРИВЕТ@ПРИМЕР.РФ"),
        ("Привет@Пример.РФ", "привет@пример.рф"),
    ],
)
async def test_exact_filter_matches_cyrillic_case_insensitively(db_citext, stored_email, filter_value):
    """Postgres's citext folding is locale-aware (unlike SQLite's own ASCII-only UPPER()) - a
    Cyrillic address matches too, still via plain equality."""
    await CitextContact.objects.create(email=stored_email)

    found = await CitextContact.objects.filter(email=filter_value)
    assert len(found) == 1
    assert found[0].email == stored_email


@pytest.mark.asyncio
async def test_citext_field_on_sqlite_raises_unsupported_error():
    """CitextField is Postgres-only (PostgresOnlyFieldMixin) - resolving its DDL for another
    dialect must raise clearly instead of silently emitting Postgres-flavored SQL."""
    from hare.dialects.postgresql.fields.citext import CitextField

    field = CitextField()
    field.model_field_name = "email"
    with pytest.raises(UnSupportedError):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))


@pytest.mark.asyncio
async def test_generate_schemas_creates_the_extension_itself():
    """generate_schemas() (the no-migrations quick-start path, distinct from makemigrations/
    migrate) used to never create Meta.extensions/field.requires_extension at all - only the
    real migrations path's OperationGenerator._collect_extensions()/CreateExtension did. A
    CitextField model run through plain generate_schemas() on a fresh database (no manual
    `CREATE EXTENSION` beforehand, unlike the db_citext fixture above, which works around this
    gap on purpose - see its own docstring) used to fail outright with
    'type "citext" does not exist'. Confirms get_create_schema_sql() now collects and emits
    CreateExtension-equivalent DDL itself, the same way it already did for Meta.schema."""
    from hare.contrib.test.helpers import hare_test_context

    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")

    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_citext"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        # No manual `CREATE EXTENSION` here - that's the entire point of this test.
        await ctx.generate_schemas(safe=False)

        contact = await CitextContact.objects.create(email="Someone@Example.com")
        found = await CitextContact.objects.filter(email="someone@example.com").first()
        assert found is not None
        assert found.id == contact.id


@pytest.mark.asyncio
async def test_generate_schemas_creates_the_extension_for_a_generated_citext_column():
    """Sibling of the test above, for a citext column built via GeneratedField(output_field=
    CitextField(...)) instead of a plain CitextField() - GeneratedField never delegated
    requires_extension to its own output_field (unlike SQL_TYPE/to_db_value/etc, which it
    already did), so every requires_extension reader (generate_schemas() here, and separately
    the real migrations path's OperationGenerator._collect_extensions()) silently never saw the
    citext extension this generated column ALSO needs. Confirmed live before this fix: CREATE
    TABLE failed with 'type "citext" does not exist', same raw error as the plain-field case
    used to give before ITS fix."""
    from hare.contrib.test.helpers import hare_test_context

    skip_if_not_postgres()
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")

    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_generated_citext"],
        db_url=db_url,
        app_label="models",
        connection_label="models",
        _generate_schemas=False,
    ) as ctx:
        # No manual `CREATE EXTENSION` here - that's the entire point of this test. Loads
        # ONLY models_generated_citext (not models_citext, which also has a plain
        # CitextField() model) - a shared module would silently mask this exact bug, since
        # the OTHER model's own correctly-declared requires_extension would create the
        # extension regardless of whether GeneratedField's own delegation works.
        await ctx.generate_schemas(safe=False)

        contact = await GeneratedCitextContact.objects.create(email="Someone@Example.com")
        await contact.refresh_from_db()
        assert contact.email_citext == "Someone@Example.com"
        found = await GeneratedCitextContact.objects.filter(email_citext="someone@example.com").first()
        assert found is not None
        assert found.id == contact.id


@pytest.mark.asyncio
async def test_use_copy_with_citext_field_raises_clear_unsupported_error(db_citext):
    """COPY BINARY has no server-side type inference - a CitextField column isn't in either
    driver's supported-type allowlist (COPY_SUPPORTED_SQL_TYPES). Before this check existed,
    asyncpg's own copy_records_to_table let its codec registry fail with a raw
    "no binary format encoder for type citext" that never named the field; this must raise a
    clear, field-naming ConfigurationError before either driver is reached."""
    with pytest.raises(UnSupportedError, match="email"):
        await CitextContact.objects.bulk_create([CitextContact(id=1, email="a@example.com", name="A")], use_copy=True)


@pytest.mark.asyncio
async def test_citext_arrays_are_written_filtered_and_read(db_citext):
    """rust_pg refused a list of strings bound to a citext[] (and read one back as a raw blob) -
    every array path works on both drivers."""
    from hare.dialects.postgresql.functions.aggregates import ArrayAgg

    tagged = await CitextTagged.objects.create(id=1, tags=["Abc", "dEf"])
    await CitextTagged.objects.create(id=2, tags=["xyz"])
    await CitextContact.objects.bulk_create(
        [CitextContact(id=index, email=f"Name{index}@x.io") for index in range(1, 5)]
    )

    await tagged.refresh_from_db()
    assert tagged.tags == ["Abc", "dEf"]
    assert await CitextTagged.objects.filter(tags__contains=["abc"]).values_list("id", flat=True) == [1]
    assert await CitextTagged.objects.filter(tags__overlap=["XYZ", "none"]).values_list("id", flat=True) == [2]
    aggregated = await CitextContact.objects.all().aggregate(emails=ArrayAgg("email"))
    assert sorted(aggregated["emails"]) == [f"Name{index}@x.io" for index in range(1, 5)]
    many_emails = [f"NAME{index}@X.IO" for index in range(1, 30)]
    assert sorted(await CitextContact.objects.filter(email__in=many_emails).values_list("id", flat=True)) == [
        1,
        2,
        3,
        4,
    ]
    assert await CitextContact.objects.filter(email__not_in=many_emails).values_list("id", flat=True) == []


@pytest.mark.asyncio
async def test_null_byte_rejected_on_every_write_path(db_citext):
    """A null byte fails with the same ValidationError as on CharField/TextField, not a raw
    driver error, and on the bulk path too."""
    value = "a" + chr(0) + "@example.com"
    contact = await CitextContact.objects.create(id=1, email="a@example.com")
    with pytest.raises(ValidationError, match="null byte"):
        await CitextContact.objects.create(id=2, email=value)
    with pytest.raises(ValidationError, match="null byte"):
        await CitextContact.objects.bulk_create([CitextContact(id=3, email=value)])
    with pytest.raises(ValidationError, match="null byte"):
        await CitextContact.objects.filter(id=contact.id).update(email=value)
    contact.email = value
    with pytest.raises(ValidationError, match="null byte"):
        await contact.save()
    with pytest.raises(ValidationError, match=r"tags\[1\]: .*null byte"):
        await CitextTagged.objects.create(id=1, tags=["ok", value])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lookup", "value"),
    [("contains", "EXAMPLE"), ("startswith", "ALICE"), ("endswith", ".COM"), ("iexact", "ALICE@EXAMPLE.COM")],
)
async def test_text_lookups_are_case_insensitive(db_citext, lookup, value):
    """`__contains`/`__startswith`/`__endswith` on a citext column match regardless of case, as
    its equality does - the column used to be cast to VARCHAR first, which made them
    case-sensitive."""
    await CitextContact.objects.create(email="alice@example.com", name="Alice")
    await CitextContact.objects.create(email="bob@other.org", name="Bob")

    found = await CitextContact.objects.filter(**{f"email__{lookup}": value}).values_list("name", flat=True)

    assert found == ["Alice"]
