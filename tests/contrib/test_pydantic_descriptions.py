from dataclasses import FrozenInstanceError
from typing import ClassVar
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import AliasChoices, AliasPath, Field, model_validator

from hare import Hare, Model, fields
from hare.contrib.pydantic import PydanticModel, pydantic_model_creator
from hare.contrib.pydantic.creation.pydantic_meta_reading import PydanticMetaReading
from hare.contrib.pydantic.descriptions.pydantic_serialization_description import PydanticSerializationDescription
from hare.exceptions import ConfigurationError, NoValuesFetched

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
            source_model: ClassVar = book
            writer: author_schema = Field(validation_alias="author")

        assert Aliased._get_fetch_fields() == ("author",)
        assert Aliased.model_validate(obj).writer.name == "Author"
        description = PydanticSerializationDescription.get(Aliased)
        Aliased.model_rebuild(force=True)
        assert PydanticSerializationDescription.get(Aliased) is not description

        class AlternativeAuthor(PydanticModel):
            source_model: ClassVar = book
            writer: author_schema = Field(validation_alias=AliasChoices("unavailable", "author"))

        class AuthorName(PydanticModel):
            source_model: ClassVar = book
            writer: str = Field(validation_alias=AliasPath("author", "name"))

        class CachedAuthorName(PydanticModel):
            source_model: ClassVar = book
            writer: str = Field(validation_alias=AliasChoices("cached_name", AliasPath("author", "name")))

        class ByName(PydanticModel):
            model_config = {"validate_by_alias": False, "validate_by_name": True}
            source_model: ClassVar = book
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
            source_model: ClassVar = author
            value: str | int = Field(validation_alias=AliasChoices("name", "id"))

        assert NameOrId.model_validate(obj.author).value == 3
        obj.author.name = "Author"

        class FirstBook(PydanticModel):
            source_model: ClassVar = author
            first_id: int = Field(validation_alias=AliasPath("books", 0, "id"))

        assert FirstBook._get_fetch_fields() == ("books",)
        with pytest.raises(NoValuesFetched):
            FirstBook.model_validate(obj.author)
        obj.author.books._set_result_for_query([obj])
        assert FirstBook.model_validate(obj.author).first_id == 1

        class Tree(PydanticModel):
            source_model: ClassVar = node
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

    description = PydanticMetaReading.collect_pydantic_meta(Book)
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
    schema = pydantic_model_creator(Book)
    assert "title" not in schema.model_fields and "display" in schema.model_fields
    assert schema.model_config["title"] == "BookRead"
    assert schema.model_json_schema()["x-group"] == ["book", "changed"]
