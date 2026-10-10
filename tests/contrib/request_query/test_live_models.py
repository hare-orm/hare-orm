"""Request queries of models registered while the application runs: a class forgets its declaration
and parameters with the models it reads, so a live model registered again is described as it is
now, and a class of an unregistered model refuses to run."""

import pytest
import pytest_asyncio

from hare import Hare, fields
from hare.contrib.request_query import FilterField, RequestQuery
from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.query.enums import Lookup
from tests.contrib.request_query.models import Author

LIVE_TABLE = "rq_live_item"


def build_item(class_name: str, **extra_fields: fields.Field) -> type[Model]:
    return type(
        class_name,
        (Model,),
        {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True),
            "title": fields.CharField(max_length=32),
            "author": fields.ForeignKeyField("models.Author", related_name="live_items"),
            **extra_fields,
            "Meta": type("Meta", (), {"table": LIVE_TABLE}),
        },
    )


@pytest_asyncio.fixture
async def live_table(db_request_query):
    connection = Author.get_connection()
    await connection.execute_script(f"DROP TABLE IF EXISTS {LIVE_TABLE}")
    await connection.execute_script(
        f"CREATE TABLE {LIVE_TABLE} (id INTEGER PRIMARY KEY, title VARCHAR(32) NOT NULL, "
        "author_id INTEGER NOT NULL, rank INTEGER)"
    )
    registered: list[type[Model]] = []
    yield registered
    for model in registered:
        if model._meta.is_bound:
            Hare.unregister_live_models([model])
    await connection.execute_script(f"DROP TABLE IF EXISTS {LIVE_TABLE}")


def register(registered: list[type[Model]], model: type[Model]) -> type[Model]:
    Hare.register_live_models([model], "live", connection_alias="models")
    registered.append(model)
    return model


@pytest.mark.asyncio
async def test_a_live_model_registered_again_is_described_as_it_is_now(live_table):
    item = register(live_table, build_item("LiveItem", rank=fields.IntField(null=True)))
    author = await Author.objects.create(id=1, name="Anna")
    await item.objects.create(id=1, title="first", author=author, rank=1)
    await item.objects.create(id=2, title="second", author=author, rank=3)
    item_query = RequestQuery.for_model(item, filters=(FilterField("rank", lookups=(Lookup.GTE,)),))
    assert [row.title for row in await item_query.from_query_string("rank__gte=2").fetch()] == ["second"]

    class AuthorByItemQuery(RequestQuery[Author]):
        class Meta:
            queryset = Author.objects.all()
            filters = (FilterField("live_items__rank", lookups=(Lookup.GTE,), parameter="item_rank"),)

    AuthorByItemQuery.prepare_parameters()
    assert AuthorByItemQuery.model_fields["item_rank__gte"].annotation == int | None
    assert [row.name for row in await AuthorByItemQuery.from_query_string("item_rank__gte=2").fetch()] == ["Anna"]

    Hare.unregister_live_models([item])
    with pytest.raises(ConfigurationError, match="LiveItem, which was unregistered"):
        item_query.from_query_string("rank__gte=2")
    with pytest.raises(ConfigurationError, match="LiveItem, which was unregistered"):
        item_query.get_declaration()

    item_with_text_rank = register(live_table, build_item("LiveItem", rank=fields.CharField(max_length=16, null=True)))
    assert AuthorByItemQuery not in RequestQuery.prepared_classes
    assert "item_rank__gte" not in AuthorByItemQuery.model_fields
    AuthorByItemQuery.prepare_parameters()
    assert AuthorByItemQuery.model_fields["item_rank__gte"].annotation == str | None
    new_query = RequestQuery.for_model(item_with_text_rank, filters=(FilterField("rank", lookups=(Lookup.GTE,)),))
    (rank_description,) = [item for item in new_query.describe_parameters() if item.name == "rank__gte"]
    assert rank_description.value_type is str

    Hare.unregister_live_models([item_with_text_rank])
    register(live_table, build_item("LiveItem"))
    with pytest.raises(ConfigurationError, match="has no field 'rank'"):
        AuthorByItemQuery.prepare_parameters()


@pytest.mark.asyncio
async def test_a_field_added_to_a_live_model_becomes_a_parameter(live_table):
    item = register(live_table, build_item("LiveItem"))
    query_without_rank = RequestQuery.for_model(item, filters=(FilterField("title"),))
    assert "rank" not in query_without_rank.model_fields
    Hare.unregister_live_models([item])
    item_with_rank = register(live_table, build_item("LiveItem", rank=fields.IntField(null=True)))
    query_with_rank = RequestQuery.for_model(item_with_rank, filters=(FilterField("title"), FilterField("rank")))
    assert query_with_rank.model_fields["rank"].annotation == int | None
    with pytest.raises(ConfigurationError, match="which was unregistered"):
        query_without_rank.describe_parameters()


@pytest.mark.asyncio
async def test_a_class_on_a_model_registered_again_as_the_same_class_works_again(live_table):
    item = register(live_table, build_item("LiveItem", rank=fields.IntField(null=True)))
    item_query = RequestQuery.for_model(item, filters=(FilterField("rank"),))
    Hare.unregister_live_models([item])
    register(live_table, item)
    author = await Author.objects.create(id=1, name="Anna")
    await item.objects.create(id=1, title="first", author=author, rank=5)
    assert [row.title for row in await item_query.from_query_string("rank=5").fetch()] == ["first"]
