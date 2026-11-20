from dataclasses import FrozenInstanceError
from typing import ClassVar
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import AliasChoices, AliasPath, Field, ValidationError, model_validator

from hare import Hare, Model, fields
from hare.contrib.pydantic import PydanticModel, pydantic_model_creator
from hare.contrib.pydantic.creation.pydantic_meta_reading import PydanticMetaReading
from hare.contrib.pydantic.descriptions.pydantic_serialization_description import PydanticSerializationDescription
from hare.exceptions import ConfigurationError, NoValuesFetched
from tests.utils.model_bindings import ModelBindings

pytestmark = pytest.mark.database_independent


class SchemaAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()


class SchemaBook(Model):
    id = fields.IntField(primary_key=True)
    author = fields.ForeignKeyField("schemas.SchemaAuthor", related_name="books")


class SchemaNode(Model):
    id = fields.IntField(primary_key=True)
    parent = fields.ForeignKeyField("schemas.SchemaNode", related_name="children", null=True)


@pytest.mark.asyncio
async def test_schema_description_tracks_inheritance_aliases_rebuild_and_recursive_fetching():
    context = await Hare.init(
        {
            "connections": {"default": "sqlite+aiosqlite://:memory:"},
            "apps": {"schemas": {"models": [__name__], "default_connection": "default"}},
        },
        connect=False,
    )
    try:
        author = context.apps.get_model("schemas.SchemaAuthor")
        book = context.apps.get_model("schemas.SchemaBook")
        node = context.apps.get_model("schemas.SchemaNode")
        schema = pydantic_model_creator(book, include=("id", "author.id", "author.name"))

        class Inherited(schema):
            pass

        assert schema._get_fetch_fields() == Inherited._get_fetch_fields() == ("author",)
        obj = book(id=1, author_id=3)
        with pytest.raises(NoValuesFetched):
            schema.model_validate(obj)
        obj.author = author.construct(id=3, name="Author", _saved_in_db=True)
        assert schema.model_validate(obj).model_dump() == {"id": 1, "author": {"id": 3, "name": "Author"}}

        class Reentrant(schema):
            @model_validator(mode="after")
            def check_another(self):
                schema.model_validate(book(id=2, author_id=3))
                return self

        queryset = Mock(prefetch_related=AsyncMock(return_value=[obj]))
        with pytest.raises(NoValuesFetched):
            await Reentrant.from_queryset(queryset)
        queryset.prefetch_related.assert_awaited_once_with("author")

        class Aliases(schema):
            model_config = {"alias_generator": lambda name: "api_" + name, "populate_by_name": True}

        assert Aliases._get_fetch_fields() == ("author",)
        assert Aliases.model_validate(obj).author.name == "Author"

        author_schema = pydantic_model_creator(author, include=("id", "name"))

        class Aliased(PydanticModel):
            source_binding: ClassVar = context.apps.get_binding(book)
            writer: author_schema = Field(validation_alias="author")

        assert Aliased.source_model is book
        assert Aliased._get_fetch_fields() == ("author",)
        assert Aliased.model_validate(obj).writer.name == "Author"
        description = PydanticSerializationDescription.get(Aliased)
        Aliased.model_rebuild(force=True)
        assert PydanticSerializationDescription.get(Aliased) is not description

        class AlternativeAuthor(PydanticModel):
            source_binding: ClassVar = context.apps.get_binding(book)
            writer: author_schema = Field(validation_alias=AliasChoices("unavailable", "author"))

        class AuthorName(PydanticModel):
            source_binding: ClassVar = context.apps.get_binding(book)
            writer: str = Field(validation_alias=AliasPath("author", "name"))

        class CachedAuthorName(PydanticModel):
            source_binding: ClassVar = context.apps.get_binding(book)
            writer: str = Field(validation_alias=AliasChoices("cached_name", AliasPath("author", "name")))

        class ByName(PydanticModel):
            model_config = {"populate_by_name": True, "validate_by_alias": False, "validate_by_name": True}
            source_binding: ClassVar = context.apps.get_binding(book)
            author: author_schema = Field(validation_alias="unavailable")

        for alias_schema, expected in (
            (AlternativeAuthor, {"writer": {"id": 3, "name": "Author"}}),
            (AuthorName, {"writer": "Author"}),
            (CachedAuthorName, {"writer": "Author"}),
            (ByName, {"author": {"id": 3, "name": "Author"}}),
        ):
            assert alias_schema._get_fetch_fields() == ("author",)
            assert alias_schema.model_validate(obj).model_dump() == expected
            with pytest.raises(NoValuesFetched):
                alias_schema.model_validate(book(id=2, author_id=3))
        cached = book(id=2, author_id=3)
        cached.__dict__["cached_name"] = "Cached"
        assert CachedAuthorName.model_validate(cached).writer == "Cached"
        del obj.author.__dict__["name"]
        with pytest.raises(NoValuesFetched):
            AuthorName.model_validate(obj)

        class NameOrId(PydanticModel):
            source_binding: ClassVar = context.apps.get_binding(author)
            value: str | int = Field(validation_alias=AliasChoices("name", "id"))

        assert NameOrId.model_validate(obj.author).value == 3
        obj.author.name = "Author"

        class FirstBook(PydanticModel):
            source_binding: ClassVar = context.apps.get_binding(author)
            first_id: int = Field(validation_alias=AliasPath("books", 0, "id"))

        assert FirstBook._get_fetch_fields() == ("books",)
        with pytest.raises(NoValuesFetched):
            FirstBook.model_validate(obj.author)
        obj.author.books.relation_rows.set_fetched([obj])
        assert FirstBook.model_validate(obj.author).first_id == 1

        class Tree(PydanticModel):
            source_binding: ClassVar = context.apps.get_binding(node)
            id: int
            parent: "Tree | None" = None

        Tree.model_rebuild()
        with pytest.raises(ConfigurationError, match="finite Pydantic schema"):
            Tree._get_fetch_fields()
        assert Tree.model_validate(node(id=1, parent=None)).parent is None
    finally:
        await Hare.close_connections()
        context.__exit__(None, None, None)


def test_schema_options_merge_abstract_bases_and_detach_input_collections():

    class Named(Model):
        title = fields.TextField()

        class Meta:
            abstract = True
            pydantic = {"exclude": ["title"], "max_recursion": 1, "backward_relations": False}

    class Labeled(Model):
        label = fields.TextField()

        class Meta:
            abstract = True

            class pydantic:
                extra_fields = {"display": str}

    class Book(Named, Labeled):
        id = fields.IntField(primary_key=True)

        class Meta:
            pydantic = {"model_config": {"title": "BookRead", "json_schema_extra": {"x-group": ["book"]}}}

    apps = ModelBindings.prepare(Book)
    description = PydanticMetaReading.collect_pydantic_meta(apps.get_binding(Book))
    assert description.max_recursion == 1
    assert description.backward_relations is False
    assert description.exclude == ("Meta", "title")
    assert description.extra_fields == {"display": str}
    with pytest.raises(TypeError):
        description.extra_fields["other"] = int
    with pytest.raises(FrozenInstanceError):
        description.max_recursion = 8
    Book.Meta.pydantic["model_config"]["json_schema_extra"]["x-group"].append("changed")
    assert description.model_config["json_schema_extra"]["x-group"] == ["book"]
    schema = pydantic_model_creator(Book, apps=apps)
    assert "title" not in schema.model_fields and "display" in schema.model_fields
    assert schema.model_config["title"] == "BookRead"
    assert schema.model_json_schema()["x-group"] == ["book"]


def test_a_manual_schema_rejects_a_model_that_disagrees_with_its_binding():

    apps = ModelBindings.prepare(SchemaAuthor, SchemaBook, SchemaNode, app_label="schemas")
    with pytest.raises(ConfigurationError, match="source_model must match"):

        class WrongSource(PydanticModel):
            source_binding: ClassVar = apps.get_binding(SchemaBook)
            source_model: ClassVar = SchemaAuthor

    class MissingBinding(PydanticModel):
        source_model: ClassVar = SchemaBook

    with pytest.raises(ConfigurationError, match="explicit source_binding"):
        MissingBinding._get_fetch_fields()


def test_a_pydantic_configuration_class_is_captured_with_its_model():
    class Book(Model):
        title = fields.TextField()

        class Meta:
            class pydantic:
                exclude = ["title"]
                model_config = {"json_schema_extra": {"labels": ["book"]}}

    Book.Meta.pydantic.exclude.clear()
    Book.Meta.pydantic.model_config["json_schema_extra"]["labels"].append("changed")
    schema = pydantic_model_creator(Book, apps=ModelBindings.prepare(Book))
    assert "title" not in schema.model_fields
    assert schema.model_json_schema()["labels"] == ["book"]


@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize(
    "path,payload,expected",
    [
        ("author.name", {"author": {}}, {"author": {"name": None}}),
        ("author.name", {"author": {"name": None}}, {"author": {"name": None}}),
        ("author.bio", {"author": {}}, {"author": {"bio": "default-bio"}}),
        ("author.bio", {"author": {"bio": None}}, {"author": {"bio": None}}),
        ("author.publisher.name", {"author": {"publisher": {}}}, {"author": {"publisher": {"name": None}}}),
        ("author.publisher", {"author": {}}, {"author": {"publisher": None}}),
    ],
)
def test_nested_optional_fields_reach_their_schema_and_do_not_change_cached_required_fields(
    composite, path, payload, expected
):
    class Publisher(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        name = fields.TextField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Author(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        name = fields.TextField()
        bio = fields.TextField(default="default-bio")
        publisher = fields.ForeignKeyField("schemas.Publisher", related_name="authors")
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    class Book(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        author = fields.ForeignKeyField("schemas.Author", related_name="books")
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

    apps = ModelBindings.prepare(Publisher, Author, Book, app_label="schemas")
    include = (path if path != "author.publisher" else "author.publisher.name",)
    required = pydantic_model_creator(Book, apps=apps, include=include)
    optional = pydantic_model_creator(Book, apps=apps, include=include, optional=(path,))
    assert optional.model_validate(payload).model_dump() == expected
    assert pydantic_model_creator(Book, apps=apps, include=include) is required
    with pytest.raises(ValidationError):
        required.model_validate({"author": {"bio": None}} if path == "author.bio" else payload)
    backwards = pydantic_model_creator(
        Author, apps=apps, include=("books.author.name",), optional=("books.author.name",), allow_cycles=True
    )
    assert backwards.model_validate({"books": [{"author": {}}]}).model_dump() == {
        "books": [{"author": {"name": None}}]
    }


@pytest.mark.parametrize("argument", ["include", "exclude", "optional"])
@pytest.mark.parametrize("path", ["name.missing", "books.id.missing", "books.author_id.missing", "books.unknown"])
def test_schema_selection_rejects_invalid_full_paths_even_when_the_relation_is_not_selected(argument, path):
    apps = ModelBindings.prepare(SchemaAuthor, SchemaBook, app_label="schemas")
    options = {"include": ("name",), argument: (path,)}
    with pytest.raises(ConfigurationError):
        pydantic_model_creator(SchemaAuthor, apps=apps, **options)
