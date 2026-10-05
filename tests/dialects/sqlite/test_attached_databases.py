"""SQLite's schemas are attached databases: each connection attaches the ones its settings name, a
model's ``Meta.schema`` names one of them, and the DDL names an index or trigger in it - its table and
the table a foreign key references are of the same database."""

import pytest

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.core.apps.model_connection_checks import ModelConnectionChecks
from hare.dialects.sqlite.drivers.aiosqlite.client.aiosqlite_client import AiosqliteClient
from hare.exceptions import ConfigurationError, OperationalError
from tests.utils.attached_databases import get_url_with_attached_databases

pytestmark = pytest.mark.sqlite_only

SCHEMA_MODULES = ["tests.schema.models_schema_qualified"]


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("setting", "attached"),
    [
        ({"custom": ":memory:"}, {"custom": ":memory:"}),
        ("custom::memory:,archive:C:\\data\\archive.db", {"custom": ":memory:", "archive": "C:\\data\\archive.db"}),
    ],
)
def test_the_attached_databases_are_read_from_a_dict_or_a_url_text(setting, attached):
    client = AiosqliteClient(file_path=":memory:", connection_alias="default", attached_databases=setting)
    assert client.attached_databases == attached


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("setting", "message"),
    [
        ("custom", "name:file pairs"),
        # Texts of no pair were read as no database, a name given twice as its last file.
        ("", "name:file pairs"),
        (",", "name:file pairs"),
        ("a::memory:,a:other.db", "a database name is given twice"),
        (["custom"], "a dict of database names to files"),
        ({"main": ":memory:"}, "keeps the database name 'main'"),
        ({"TEMP": ":memory:"}, "keeps the database name 'TEMP'"),
        ({"": ":memory:"}, "a database name must be a non-empty text"),
        ({"custom": ""}, "the file of database 'custom' must be a non-empty text"),
        ({"custom": 1}, "the file of database 'custom' must be a non-empty text"),
    ],
)
def test_attached_databases_of_no_name_and_file_are_refused(setting, message):
    with pytest.raises(ConfigurationError, match=message):
        AiosqliteClient(file_path=":memory:", connection_alias="default", attached_databases=setting)


@pytest.mark.asyncio
async def test_a_model_of_a_database_not_attached_meets_sqlites_own_error():
    with pytest.raises(OperationalError, match='unknown database "custom"'):
        async with hare_test_context(SCHEMA_MODULES, db_url="sqlite+aiosqlite://:memory:"):
            pass


@pytest.mark.asyncio
async def test_the_ddl_names_indexes_in_the_attached_database_and_references_its_tables():
    async with hare_test_context(
        SCHEMA_MODULES, db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["custom"])
    ) as ctx:
        connection = ctx.apps.get_model("models", "SchemaCategory")._meta.connection
        schema_sql = connection.get_schema_sql(safe=False)
        assert 'CREATE TABLE "custom"."product"' in schema_sql
        assert 'CREATE INDEX "custom"."idx_product_categor_2b519b10056a" ON "product" ("category_id")' in schema_sql
        assert 'REFERENCES "category" ("id")' in schema_sql
        assert '"custom"."category" (' not in schema_sql.split('CREATE TABLE "custom"."product"')[1]
        # The objects are in the attached database, none in the main one.
        _, attached_rows = await connection.execute("SELECT type, name FROM custom.sqlite_master ORDER BY name")
        assert ("index", "idx_product_categor_2b519b10056a") in [tuple(row) for row in attached_rows]
        _, main_rows = await connection.execute("SELECT name FROM main.sqlite_master")
        assert list(main_rows) == []


@pytest.mark.asyncio
async def test_the_migration_safety_check_counts_the_rows_of_the_attached_database():
    async with hare_test_context(
        SCHEMA_MODULES, db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["custom"])
    ) as ctx:
        connection = ctx.apps.get_model("models", "SchemaCategory")._meta.connection
        await connection.execute_script("INSERT INTO \"custom\".\"category\" (id, name) VALUES (1, 'a'), (2, 'b')")
        await connection.execute_script('CREATE TABLE "main"."category" (id INTEGER PRIMARY KEY)')
        rules = connection.dialect.migration_safety_rules
        assert await rules.count_table_rows(connection, "category", "custom", 10) == 2
        assert await rules.count_table_rows(connection, "category", None, 10) == 0
        assert await rules.count_table_rows(connection, "product", None, 10) == 0


@pytest.mark.asyncio
async def test_a_constrained_relation_to_a_model_of_another_schema_is_refused(monkeypatch):
    async with hare_test_context(
        SCHEMA_MODULES, db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["custom"])
    ) as ctx:
        product_model = ctx.apps.get_model("models", "SchemaProduct")
        category_model = ctx.apps.get_model("models", "SchemaCategory")
        tag_model = ctx.apps.get_model("models", "SchemaTag")
        monkeypatch.setattr(category_model._meta, "schema", "archive")
        monkeypatch.setattr(tag_model._meta, "schema", "archive")
        with pytest.raises(
            ConfigurationError,
            match=r"relations category, tags to models of another schema.*reference only tables of their own schema",
        ):
            ModelConnectionChecks.check_model_writes(product_model)
        monkeypatch.setattr(product_model._meta.fields_map["category"], "db_constraint", False)
        monkeypatch.setattr(product_model._meta.fields_map["tags"], "db_constraint", False)
        ModelConnectionChecks.check_model_writes(product_model)


@pytest.mark.asyncio
async def test_a_full_text_index_and_a_trigger_of_an_attached_database():
    from hare.search import SearchHeadline, SearchRank
    from tests.dialects.sqlite.models_attached_database import ArchivedArticle

    async with hare_test_context(
        ["tests.dialects.sqlite.models_attached_database"],
        db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["archive"]),
    ):
        hares = await ArchivedArticle.objects.create(title="Hares", body="Running hares in the field")
        await ArchivedArticle.objects.create(title="Owls", body="Owls at night")

        assert [article.id for article in await ArchivedArticle.objects.filter(body__search="hare")] == [hares.id]
        ranked = await (
            ArchivedArticle.objects.annotate(rank=SearchRank(("title", "body"), "hare"))
            .filter(rank__gt=0)
            .values_list("id", flat=True)
        )
        assert ranked == [hares.id]
        headline = await ArchivedArticle.objects.annotate(
            headline=SearchHeadline("body", "hare", start_sel="[", stop_sel="]")
        ).get(id=hares.id)
        assert headline.headline == "Running [hares] in the field"
        # The trigger of the attached database counts the update.
        await ArchivedArticle.objects.filter(id=hares.id).update(title="Hares and rabbits")
        assert (await ArchivedArticle.objects.get(id=hares.id)).edits == 1
        assert [article.id for article in await ArchivedArticle.objects.filter(title__search="rabbits")] == [hares.id]

        connection = ArchivedArticle._meta.connection
        _, rows = await connection.execute("SELECT type, name FROM archive.sqlite_master WHERE type = 'trigger'")
        assert {row[1] for row in rows} >= {"archived_article_counts_edits"}
        _, main_rows = await connection.execute("SELECT name FROM main.sqlite_master")
        assert list(main_rows) == []


@pytest.mark.asyncio
async def test_a_spatial_index_of_an_attached_database_is_refused(monkeypatch):
    from hare.dialects.sqlite.indexes import SpatialiteIndex
    from hare.exceptions import UnSupportedError
    from tests.dialects.sqlite.models_attached_database import ArchivedArticle

    async with hare_test_context(
        ["tests.dialects.sqlite.models_attached_database"],
        db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["archive"]),
    ):
        connection = ArchivedArticle._meta.connection
        monkeypatch.setattr(connection, "features", connection.features.replace(supports_spatial_index=True))
        editor = connection.dialect.schema_editor_class(connection)
        with pytest.raises(UnSupportedError, match="geometry columns of the main database only"):
            SpatialiteIndex(fields=("body",)).get_create_sqls(editor, ArchivedArticle, safe=False)


@pytest.mark.asyncio
async def test_inspectdb_reads_the_tables_of_an_attached_database_as_models_of_its_schema():
    from hare.inspectdb import SchemaInspector, SchemaNotFoundError

    async with hare_test_context(
        SCHEMA_MODULES, db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["custom"])
    ) as ctx:
        connection = ctx.apps.get_model("models", "SchemaCategory")._meta.connection
        await connection.execute_script('CREATE TABLE "main"."category" (id INTEGER PRIMARY KEY)')

        source = await SchemaInspector.inspect(connection, schema="custom")

        assert source.count("schema = 'custom'") == 3
        assert "category = fields.ForeignKeyField" in source
        assert "tags = fields.ManyToManyField('models.Tag', through='product_tags'" in source
        assert "schema = 'custom'" not in await SchemaInspector.inspect(connection)
        with pytest.raises(SchemaNotFoundError):
            await SchemaInspector.inspect(connection, schema="archive")


@pytest.mark.asyncio
async def test_drift_reads_the_indexes_and_triggers_of_an_attached_database():
    from hare.migrations.drift import detect_drift_for_alias

    async with hare_test_context(
        ["tests.dialects.sqlite.models_attached_database"],
        db_url=get_url_with_attached_databases("sqlite+aiosqlite://:memory:", ["archive"]),
        app_label="models",
        connection_label="models",
    ) as ctx:
        apps_config = {"models": {"models": [], "default_connection": "models"}}
        assert not (await detect_drift_for_alias(ctx.apps, apps_config, "models")).has_drift

        connection = ctx.connections.get("models")
        await connection.execute_script('DROP TRIGGER "archive"."archived_article_counts_edits"')

        result = await detect_drift_for_alias(ctx.apps, apps_config, "models")

        assert [type(operation).__name__ for operation in result.operations] == ["AddTrigger"]
