"""Included class references use the historical target, including fallback app labels."""

import pytest

from hare import Model, fields
from hare.contrib.test import requires_features
from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.instrumentation.queries.query_tags import QueryTags
from hare.migrations import Migration, MigrationRunner, State
from hare.migrations.operations import AlterField
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION


@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("reference_is_class", [False, True], ids=["label", "class"])
async def test_app_label_fallback_binds_relations_to_updated_historical_models(
    contract_database, composite, reference_is_class
):
    declaration_graph = ModelBindingGraph()
    connection = contract_database.get_connection()

    class Author(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField()
            pk = fields.CompositePrimaryKey("region", "number")

    class Book(Model):
        number = fields.IntField(primary_key=True, generated=False)
        author = fields.ForeignKeyField(
            Author if reference_is_class else f"{CONTRACT_APP}.Author",
            db_constraint=connection.features.supports_foreign_keys,
        )

    state = State.from_models(
        [Author, Book], app_label=CONTRACT_APP, default_connections={CONTRACT_APP: CONTRACT_CONNECTION}
    )
    state.validate_relations_initialized()
    assert state.apps.get_binding(f"{CONTRACT_APP}.Book").fields_map["author"].related_model is state.apps.get_model(
        f"{CONTRACT_APP}.Author"
    )
    AlterField("Author", "number", fields.BigIntField(primary_key=not composite, generated=False)).state_forward(
        CONTRACT_APP, state
    )
    author_model_apps = state.apps
    author_model = author_model_apps.get_model(f"{CONTRACT_APP}.Author")
    book_model_apps = state.apps
    book_model = book_model_apps.get_model(f"{CONTRACT_APP}.Book")
    assert state.apps.get_binding(book_model).fields_map["author"].related_model is author_model
    editor = connection.dialect.schema_editor_class(connection)
    for model in (author_model, book_model):
        await editor.create_model(state.apps.get_binding(model))
    author = await author_model_apps.get_queryset(author_model).create(
        number=2**40, **({"region": 7} if composite else {})
    )
    await book_model_apps.get_queryset(book_model).create(number=1, author=author)
    assert await book_model_apps.get_queryset(book_model).values_list(
        "author_number" if composite else "author_id", flat=True
    ) == [2**40]
    assert type(declaration_graph.include(Author).fields_map["number"]) is fields.IntField
    assert declaration_graph.include(Author).app is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
async def test_a_historical_queryset_retains_its_bindings_after_a_schema_change(contract_database, composite):
    connection = contract_database.get_connection()

    class Author(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        if composite:
            region = fields.IntField()
            pk = fields.CompositePrimaryKey("region", "number")
        name = fields.CharField(max_length=12)

    class Book(Model):
        number = fields.IntField(primary_key=True, generated=False)
        author = fields.ForeignKeyField(Author)

    before = State.from_models(
        [Author, Book], app_label=CONTRACT_APP, default_connections={CONTRACT_APP: CONTRACT_CONNECTION}
    )
    runner = MigrationRunner(connection)
    editor = connection.dialect.schema_editor_class(connection)
    old_apps = before.apps
    old_author = old_apps.get_model(f"{CONTRACT_APP}.Author")
    old_book = old_apps.get_model(f"{CONTRACT_APP}.Book")
    for model in (old_author, old_book):
        await editor.create_model(old_apps.get_binding(model))
    author = await old_apps.get_queryset(old_author).create(
        number=4, name="before", **({"region": 7} if composite else {})
    )
    await old_apps.get_queryset(old_book).create(number=1, author=author)
    old_query = old_apps.get_queryset(old_book).filter(number=1).select_related("author")
    assert (await old_query.get()).author.name == "before"
    migration = Migration(
        "widen_author_name", CONTRACT_APP, operations=[AlterField("Author", "name", fields.CharField(max_length=24))]
    )
    after = await runner.apply(migration, before)
    new_book = after.apps.get_model(f"{CONTRACT_APP}.Book")
    # Give PgBouncer a new SQL identity for the changed result type after the migration.
    with QueryTags.scope(schema_phase="widened"):
        fresh = await after.apps.get_queryset(new_book).filter(number=1).select_related("author").get()
        retained = await old_query.get()
    assert (fresh.author.pk, fresh.author.name) == (author.pk, "before")
    assert (retained.author.pk, retained.author.name) == (author.pk, "before")
    assert type(retained.author) is old_author
    assert type(fresh.author) is after.apps.get_model(f"{CONTRACT_APP}.Author")
    assert type(fresh.author) is not old_author
    assert old_apps.get_binding(old_author).fields_map["name"].max_length == 12
    assert after.apps.get_binding(type(fresh.author)).fields_map["name"].max_length == 24
    await runner.unapply(migration, state_before=before)
    assert (await old_query.get()).author.name == "before"
