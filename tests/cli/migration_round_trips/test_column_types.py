"""Full CLI migration cycles against a real database (a SQLite file, or a database of the reusable test database
pool when HARE_TEST_DB points at a server): makemigrations -> migrate with data -> makemigrations --check ->
drift -> rollback, each command run the way a user runs it - of column types: generated columns, database
defaults, native enums, arrays and encrypted fields."""

from __future__ import annotations

import textwrap

import pytest

from hare.fields.encrypted.field_encryption import FieldEncryption
from tests.utils.database_under_test import DatabaseUnderTest


@pytest.mark.asyncio
async def test_changed_generated_field_is_recreated_after_the_field_it_now_uses(project_factory) -> None:
    project = await project_factory()
    project.write_models("""
    class Item(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(default=1)
        qty = fields.IntField()
        total = fields.GeneratedField(expression=RawSQLTerm("price * qty"), output_field=fields.IntField())
    """)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, price, qty) VALUES (1, 3, 4)")

    project.write_models("""
    class Item(Model):
        id = fields.IntField(primary_key=True)
        price = fields.IntField(default=1)
        qty = fields.IntField()
        discount = fields.BigIntField(default=2)
        total = fields.GeneratedField(expression=RawSQLTerm("price * qty - discount"), output_field=fields.IntField())
    """)
    await project.cli_ok("makemigrations")
    migration_source = project.read_latest_migration()
    assert migration_source.index('name="discount"') < migration_source.index('name="total",')
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await project.query("SELECT id, discount, total FROM item") == [{"id": 1, "discount": 2, "total": 10}]

    await project.cli_ok("migrate", "app", "0001_initial")
    assert await project.query("SELECT id, total FROM item") == [{"id": 1, "total": 12}]


@pytest.mark.asyncio
async def test_a_column_with_a_database_default_and_a_description_is_added_and_removed(project_factory) -> None:
    project = await project_factory()
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True, generated=False)
        {field}
    """
    field = 'count = fields.IntField(db_default=3, description="how many")'
    for step in ("", field, "", field):
        project.write_models(textwrap.dedent(template.format(field=step)))
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
    await project.query("INSERT INTO item (id) VALUES (1)")
    assert await project.query("SELECT count FROM item") == [{"count": 3}]
    await project.cli_ok("migrate", "app", project.migration_files()[1])
    await project.cli_ok("migrate", "app", "0001_initial")
    await project.cli_ok("migrate")
    assert await project.query("SELECT count FROM item") == [{"count": 3}]


@pytest.mark.asyncio
async def test_a_label_added_to_a_native_enum_reaches_the_database(project_factory) -> None:
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("NativeEnumField is Postgres-only")
    template = """
    from enum import Enum

    from hare.dialects.postgresql.fields.native_enum import NativeEnumField

    class Mood(Enum):
        {members}

    class Thing(Model):
        id = fields.IntField(primary_key=True, generated=False)
        mood = NativeEnumField(Mood, type_name="mood_type", null=True)
        moods = fields.ArrayField(NativeEnumField(Mood, type_name="mood_type"), null=True)
    """
    project.write_models(template.format(members='A = "a"\n        C = "c"'))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    # The class gets a member in the middle - the migrations written before keep the members they had.
    project.write_models(template.format(members='A = "a"\n        B = "b"\n        C = "c"'))
    await project.cli_ok("makemigrations")
    assert "AlterEnumType" in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    rows = await project.query("SELECT enum_range(NULL::mood_type)::text AS labels")
    assert rows == [{"labels": "{a,b,c}"}]
    await project.query("INSERT INTO thing (id, mood, moods) VALUES (1, 'b', ARRAY['b', 'c']::mood_type[])")
    # A row holding the new label keeps the type from going back.
    await project.query("DELETE FROM thing")
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_same_named_native_enum_types_of_two_schemas_are_two_types(project_factory) -> None:
    """A field's type_schema names the schema of its type - one name in two schemas was one type, and
    the second field's labels a conflict."""
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("NativeEnumField is Postgres-only")
    template = """
    from enum import Enum

    from hare.dialects.postgresql.fields.native_enum import NativeEnumField

    class Mood(Enum):
        HAPPY = "happy"
        SAD = "sad"

    class Shade(Enum):
        {shade_members}

    class Thing(Model):
        id = fields.IntField(primary_key=True, generated=False)
        mood = NativeEnumField(Mood, type_name="tone")
        shade = NativeEnumField(Shade, type_name="tone", type_schema="paint", null=True)

    class Brush(Model):
        id = fields.IntField(primary_key=True, generated=False)
        shade = NativeEnumField(Shade, type_name="tone", type_schema="paint")

        class Meta:
            schema = "paint"
    """

    async def get_type_labels() -> dict[str, str]:
        rows = await project.query(
            "SELECT n.nspname AS schema_name, array_agg(e.enumlabel ORDER BY e.enumsortorder)::text AS labels "
            "FROM pg_type t "
            "JOIN pg_enum e ON e.enumtypid = t.oid JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE t.typname = 'tone' GROUP BY n.nspname"
        )
        return {row["schema_name"]: row["labels"] for row in rows}

    project.write_models(template.format(shade_members='DARK = "dark"\n        LIGHT = "light"'))
    await project.cli_ok("makemigrations")
    assert (
        'ops.CreateEnumType(name="tone", labels=("dark", "light"), schema="paint")' in project.read_latest_migration()
    )
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await get_type_labels() == {"public": "{happy,sad}", "paint": "{dark,light}"}
    await project.query("INSERT INTO thing (id, mood, shade) VALUES (1, 'sad', 'light')")
    await project.query("INSERT INTO paint.brush (id, shade) VALUES (1, 'dark')")

    # A removed label replaces the type of its schema only.
    project.write_models(template.format(shade_members='DARK = "dark"\n        MATTE = "matte"'))
    await project.query("UPDATE thing SET shade = NULL")
    await project.cli_ok("makemigrations")
    assert "AlterEnumType" in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    assert await get_type_labels() == {"public": "{happy,sad}", "paint": "{dark,matte}"}
    assert await project.query("SELECT shade::text AS shade FROM paint.brush") == [{"shade": "dark"}]

    await project.cli_ok("migrate", "app", "zero")
    assert await get_type_labels() == {}


@pytest.mark.asyncio
async def test_a_native_enum_column_moves_to_a_type_of_another_schema(project_factory) -> None:
    """The schema of a type_schema no model is in wasn't created, a column moved to another ENUM type
    wasn't cast ("cannot cast type ... to ..."), and an empty label was refused by the operations."""
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("NativeEnumField is Postgres-only")
    template = """
    from enum import Enum

    from hare.dialects.postgresql.fields.native_enum import NativeEnumField

    class Mood(Enum):
        NONE = ""
        HAPPY = "happy"

    class Thing(Model):
        id = fields.IntField(primary_key=True, generated=False)
        mood = NativeEnumField(Mood, type_name="mood"{type_schema})
        moods = fields.ArrayField(NativeEnumField(Mood, type_name="mood"{type_schema}), null=True)
    """
    project.write_models(template.format(type_schema=', type_schema="first_space"'))
    await project.cli_ok("makemigrations")
    assert 'ops.CreateSchema(schema_name="first_space")' in project.read_latest_migration()
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query(
        "INSERT INTO thing (id, mood, moods) VALUES (1, 'happy', ARRAY['', 'happy']::first_space.mood[])"
    )

    project.write_models(template.format(type_schema=', type_schema="second_space"'))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    rows = await project.query(
        "SELECT mood::text AS mood, moods::text AS moods, pg_typeof(mood)::text AS type FROM thing"
    )
    assert rows == [{"mood": "happy", "moods": '{"",happy}', "type": "second_space.mood"}]

    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_an_array_takes_the_type_and_the_extension_of_what_it_holds(project_factory) -> None:
    project = await project_factory()
    if not project.is_postgres:
        pytest.skip("the held types are Postgres-only")
    project.write_models(
        """
        from enum import Enum

        from hare.dialects.postgresql.fields import CitextField
        from hare.dialects.postgresql.fields.native_enum import NativeEnumField

        class Shade(Enum):
            DARK = "dark"
            LIGHT = "light"

        class Palette(Model):
            id = fields.IntField(primary_key=True, generated=False)
            shades = fields.ArrayField(NativeEnumField(Shade, type_name="shade_type"), null=True)
            names = fields.ArrayField(CitextField(), null=True)
            grid = fields.ArrayField(fields.ArrayField(fields.TextField()), null=True)
        """
    )
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.query(
        "INSERT INTO palette (id, shades, names, grid) "
        "VALUES (1, ARRAY['dark']::shade_type[], ARRAY['Ab']::citext[], ARRAY[ARRAY['x']])"
    )
    await project.cli_ok("migrate", "app", "zero")


@pytest.mark.asyncio
async def test_a_blind_index_added_to_an_encrypted_field_round_trips(project_factory) -> None:
    """The migration wrote the blind index as fields.BlindIndexField, which hare.fields didn't export - the
    migration didn't load."""
    FieldEncryption.configure("round-trip-secret")
    try:
        project = await project_factory()
        project.write_models(
            "class Person(Model):\n    id = fields.IntField(primary_key=True)\n"
            "    email = fields.EncryptedTextField(null=True)\n"
        )
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        project.write_models(
            "class Person(Model):\n    id = fields.IntField(primary_key=True)\n"
            "    email = fields.EncryptedTextField(blind_index=True, null=True)\n"
        )
        await project.cli_ok("makemigrations")
        assert "fields.BlindIndexField(" in project.read_latest_migration()
        await project.cli_ok("migrate")
        await project.assert_in_sync()
        await project.cli_ok("migrate", "app", "0001")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
    finally:
        FieldEncryption.reset()


@pytest.mark.asyncio
async def test_a_text_field_with_rows_becomes_an_encrypted_field_with_a_blind_index(project_factory) -> None:
    """SQLite rebuilt the table copying the blind index column from the old table, which had none yet."""
    FieldEncryption.configure("round-trip-secret")
    try:
        project = await project_factory()
        project.write_models(
            "class Person(Model):\n    id = fields.IntField(primary_key=True)\n"
            "    email = fields.TextField(null=True)\n"
        )
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.query("INSERT INTO person (id, email) VALUES (1, NULL)")
        project.write_models(
            "class Person(Model):\n    id = fields.IntField(primary_key=True)\n"
            "    email = fields.EncryptedTextField(blind_index=True, null=True)\n"
        )
        await project.cli_ok("makemigrations")
        await project.cli_ok("migrate")
        await project.assert_in_sync()
        assert await project.query("SELECT id FROM person") == [{"id": 1}]
    finally:
        FieldEncryption.reset()


DECIMAL_FIELDS = pytest.mark.decimal_fields
TEXT_TO_NUMBER_CHANGES = [
    ("fields.IntField()", "99999999999", False),
    ("fields.IntField()", "-42", True),
    ("fields.SmallIntField()", "40000", False),
    ("fields.BigIntField()", "9223372036854775808", False),
    pytest.param("fields.DecimalField(max_digits=5, decimal_places=2)", "123456.78", False, marks=DECIMAL_FIELDS),
    pytest.param("fields.DecimalField(max_digits=5, decimal_places=2)", "1.234", False, marks=DECIMAL_FIELDS),
    pytest.param("fields.DecimalField(max_digits=5, decimal_places=2)", "-999.99", True, marks=DECIMAL_FIELDS),
    # PostgreSQL and ClickHouse read it as NaN; SQLite keeps the text, no number.
    ("fields.FloatField()", "nan", None),
    ("fields.FloatField()", "2.5e3", True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("declaration", "stored", "applies"), TEXT_TO_NUMBER_CHANGES)
async def test_a_text_column_becomes_a_number_only_with_numbers_it_holds(
    project_factory, declaration, stored, applies
) -> None:
    """SQLite took a number beyond the new type's range or places, and kept "nan" as text - the row then
    neither saved nor matched a filter; PostgreSQL rounded the places away."""
    project = await project_factory()
    text_model = """
    class Doc(Model):
        id = fields.IntField(primary_key=True, generated=False)
        v = fields.CharField(max_length=40)
    """
    project.write_models(text_model)
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(f"INSERT INTO doc (id, v) VALUES (1, '{stored}')")

    project.write_models(text_model.replace("fields.CharField(max_length=40)", declaration))
    await project.cli_ok("makemigrations")
    result = await project.cli("migrate")

    if applies is None:
        applies = DatabaseUnderTest.get_dialect().name != "sqlite"
    assert (result.exit_code in (0, None)) is applies, result.output
    if not applies:
        assert [str(row["v"]) for row in await project.query("SELECT v FROM doc")] == [stored]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old_declaration", "new_declaration", "stored_sql", "new_default"),
    [
        ('fields.CharField(max_length=5, db_default="5")', "fields.IntField(db_default=7)", "'3'", 7),
        ('fields.CharField(max_length=5, db_default="5")', "fields.IntField(default=0)", "'3'", None),
        ("fields.IntField(db_default=1)", "fields.BooleanField(db_default=True)", "0", True),
    ],
)
async def test_a_column_with_a_database_default_changes_its_type(
    project_factory, old_declaration, new_declaration, stored_sql, new_default
) -> None:
    """PostgreSQL cast the old DEFAULT with the column and refused it ("default for column cannot be cast
    automatically") - on an empty table too."""
    project = await project_factory()
    template = """
    class Doc(Model):
        id = fields.IntField(primary_key=True, generated=False)
        v = {declaration}
    """
    project.write_models(template.format(declaration=old_declaration))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query(f"INSERT INTO doc (id, v) VALUES (1, {stored_sql})")

    project.write_models(template.format(declaration=new_declaration))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    await project.assert_in_sync()
    if new_default is not None:
        await project.query("INSERT INTO doc (id) VALUES (2)")
        rows = await project.query("SELECT v FROM doc WHERE id = 2")
        assert rows[0]["v"] == new_default


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "declaration",
    [
        "fields.CharField(max_length=10, db_default=SqlDefault(\"'x' || 'y'\"))",
        "fields.CharField(max_length=10, null=True, db_default=SqlDefault(\"'x' || 'y'\"))",
        "fields.DatetimeField(db_default=Now())",
    ],
)
async def test_a_column_with_a_database_default_expression_is_added_to_a_table_with_rows(
    project_factory, declaration
) -> None:
    """SQLite refused it ("Cannot add a column with non-constant default") - an empty table took it."""
    project = await project_factory()
    template = """
    from hare.fields.db_defaults import Now, SqlDefault

    class Doc(Model):
        id = fields.IntField(primary_key=True, generated=False)
    {added}
    """
    project.write_models(template.format(added=""))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")
    await project.query("INSERT INTO doc (id) VALUES (1), (2)")

    project.write_models(template.format(added=f"    added = {declaration}"))
    await project.cli_ok("makemigrations")
    await project.cli_ok("migrate")

    await project.assert_in_sync()
    rows = await project.query("SELECT id, added FROM doc ORDER BY id")
    assert [row["id"] for row in rows] == [1, 2]
    assert all(row["added"] not in (None, "") for row in rows)
