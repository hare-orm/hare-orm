from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event
from typing import Annotated, ForwardRef

import pytest
import pytest_asyncio
from pydantic import AliasChoices, AliasPath, Field, PydanticUndefinedAnnotation, TypeAdapter, create_model
from pydantic.fields import FieldInfo

from hare import Hare, Model, fields
from hare.contrib.request_query import FilterField, NoFilter, OrderingConfig, RequestQuery
from hare.contrib.request_query.constants import GENERATION_BUCKET_KEY
from hare.contrib.request_query.declaration.declaration_builder import DeclarationBuilder
from hare.contrib.request_query.declaration.filter_parameter_builder import FilterParameterBuilder
from hare.contrib.request_query.declaration.request_query_registry import RequestQueryRegistry
from hare.contrib.request_query.exceptions import InvalidRequestQuery
from hare.core.hare_context import HareContext
from hare.dialects.dialect_registry import DialectRegistry
from hare.exceptions import ConfigurationError
from hare.query.expressions import F


class SchemaBook(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)


@pytest_asyncio.fixture
async def book_query():
    async with HareContext() as context:
        await Hare.init(
            {
                "connections": {"default": "sqlite+aiosqlite://:memory:"},
                "apps": {"schemas": {"models": [__name__], "default_connection": "default"}},
            },
            connect=False,
        )
        model = context.apps.get_model("schemas.SchemaBook")

        class BookQuery(RequestQuery[model]):
            class Meta:
                queryset = model.objects.all()
                filters = (FilterField("title", lookups=("icontains",)),)
                ordering = OrderingConfig(fields=("title",))

        yield BookQuery


pytestmark = [pytest.mark.database_independent, pytest.mark.asyncio]


async def test_invalid_declaration_restores_schema_before_retry(book_query):
    original_fields = book_query.model_fields
    original_validator = book_query.__pydantic_validator__
    original_serializer = book_query.__pydantic_serializer__
    book_query.Meta.ordering = OrderingConfig(fields=("missing",))
    with pytest.raises(ConfigurationError):
        book_query.prepare_parameters()
    assert book_query.get_cache_bucket().get(GENERATION_BUCKET_KEY) is None
    assert book_query.model_fields is original_fields
    assert book_query.__pydantic_validator__ is original_validator
    assert book_query.__pydantic_serializer__ is original_serializer
    assert "title__icontains" not in original_fields
    book_query.Meta.ordering = OrderingConfig(fields=("title",))
    assert book_query(title__icontains="book").model_dump()["title__icontains"] == "book"


async def test_failed_candidate_keeps_the_previous_generation(book_query, monkeypatch):
    schema = book_query.prepare_parameters()
    query = book_query(title__icontains="book")
    original_fields = book_query.model_fields
    original_validator = book_query.__pydantic_validator__
    original_serializer = book_query.__pydantic_serializer__
    monkeypatch.setattr(
        FilterParameterBuilder,
        "build_fields",
        lambda self, queryset: {"broken": FieldInfo(annotation=ForwardRef("UnavailableParameterType"))},
    )
    RequestQueryRegistry.forget_parameters(book_query)
    with pytest.raises(PydanticUndefinedAnnotation):
        book_query.prepare_parameters()
    assert book_query.model_fields is original_fields
    assert "broken" not in original_fields
    assert book_query.__pydantic_validator__ is original_validator
    assert book_query.__pydantic_serializer__ is original_serializer
    assert query.model_dump()["title__icontains"] == "book"
    assert type(query) is schema
    assert schema(title__icontains="another").title__icontains == "another"


async def test_generation_retains_fields_declaration_and_pydantic_serialization(book_query):
    schema = book_query.prepare_parameters()
    declaration = schema.get_declaration()
    query = book_query(title__icontains="book")
    adapter = TypeAdapter(schema)
    assert adapter.dump_python(adapter.validate_python({"title__icontains": "book"})) == query.model_dump()
    assert "title__icontains" in adapter.json_schema()["properties"]
    assert "title__icontains" not in book_query.model_fields
    book_query.Meta.filters = (FilterField("title", lookups=("startswith",)),)
    RequestQueryRegistry.forget_parameters(book_query)
    new_schema = book_query.prepare_parameters()
    assert new_schema is not schema
    assert "title__startswith" in new_schema.model_fields
    assert "title__icontains" not in new_schema.model_fields
    assert query.get_declaration() is declaration
    assert query.model_dump()["title__icontains"] == "book"
    assert isinstance(query, book_query)
    assert schema not in RequestQuery.get_concrete_subclasses()


async def test_query_string_reads_aliases_repeated_values_and_explicit_overrides(book_query):
    class Aliased(book_query):
        token: Annotated[int, NoFilter()] = Field(alias="request-token")
        tags: Annotated[list[int], NoFilter()] = Field(
            validation_alias=AliasChoices("item", "items"),
            default_factory=list,
        )
        first: Annotated[int, NoFilter()] = Field(validation_alias=AliasPath("positions", 0), default=0)

    text = "request-token=7&item=1&item=2&positions=9&positions=10"
    query = Aliased.from_query_string(text)
    assert (query.token, query.tags, query.first) == (7, [1, 2], 9)
    override = Aliased.from_query_string(text, token=11, items=[3])
    assert (override.token, override.tags, override.first) == (11, [3], 9)

    class ByName(Aliased):
        model_config = {"validate_by_alias": False, "validate_by_name": True}

    query = ByName.from_query_string("token=8&tags=4&tags=5&first=6")
    assert (query.token, query.tags, query.first) == (8, [4, 5], 6)


@pytest.mark.parametrize("reverse", [False, True])
async def test_shared_query_alias_has_one_shape_regardless_of_field_order(book_query, reverse):
    declarations = [
        ("many", (Annotated[list[int], NoFilter()], Field(alias="item"))),
        ("one", (Annotated[int, NoFilter()], Field(alias="item"))),
    ]
    query_class = create_model(
        "SharedInput", __base__=book_query, **dict(reversed(declarations) if reverse else declarations)
    )
    with pytest.raises(InvalidRequestQuery) as direct:
        query_class(item=["1", "2"])
    with pytest.raises(InvalidRequestQuery) as from_text:
        query_class.from_query_string("item=1&item=2")
    assert from_text.value.errors == direct.value.errors


async def test_shared_indexed_aliases_read_repeated_values_and_explicit_root(book_query):
    class Indexed(book_query):
        first: Annotated[int, NoFilter()] = Field(validation_alias=AliasPath("positions", 0))
        second: Annotated[int, NoFilter()] = Field(validation_alias=AliasPath("positions", 1))

    query = Indexed.from_query_string("positions=7&positions=8")
    assert (query.first, query.second) == (7, 8)
    query = Indexed.from_query_string("positions=7&positions=8", positions=[9, 10])
    assert (query.first, query.second) == (9, 10)


@pytest.mark.parametrize("second_input,expected", [("", 0), ("&second=8", 8)])
async def test_explicit_field_value_suppresses_a_shared_url_alias(book_query, second_input, expected):
    class Shared(book_query):
        first: Annotated[int, NoFilter()] = Field(alias="item")
        second: Annotated[int, NoFilter()] = Field(alias="item", default=0)

    query = Shared.from_query_string("item=7" + second_input, first=11)
    assert (query.first, query.second) == (11, expected)


@pytest.mark.parametrize("invalidate", [False, True])
async def test_concurrent_preparation_publishes_one_completed_generation(book_query, monkeypatch, invalidate):
    # Loading dialects invalidates schema candidates; test that separately below.
    DialectRegistry.get_connected_dialects()
    entered, release, reader_started = Event(), Event(), Event()
    original = DeclarationBuilder.build
    original_fields = book_query.model_fields
    calls = []

    def build(builder):
        calls.append(builder.request_query_class)
        entered.set()
        assert release.wait(5)
        if invalidate and len(calls) == 1:
            RequestQueryRegistry.forget_parameters(book_query)
        return original(builder)

    def read():
        reader_started.set()
        return book_query.prepare_parameters()

    monkeypatch.setattr(DeclarationBuilder, "build", build)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(copy_context().run, book_query.prepare_parameters)
        try:
            assert entered.wait(5)
            second = executor.submit(copy_context().run, read)
            assert reader_started.wait(5)
            assert book_query.model_fields is original_fields
            assert book_query.get_cache_bucket().get(GENERATION_BUCKET_KEY) is None
        finally:
            release.set()
        assert first.result(5) is second.result(5)
    assert len(calls) == (2 if invalidate else 1)
    assert first.result() is book_query.prepare_parameters()


async def test_declaration_uses_the_current_annotation_and_immutable_mappings(book_query):
    declaration = book_query.get_declaration()
    queryset = declaration.queryset
    number = declaration.get_lookup_info(queryset.annotate(score=F("id")), "score")
    text = declaration.get_lookup_info(queryset.annotate(score=F("title")), "score")
    assert number.value_type is int
    assert text.value_type is str
    with pytest.raises(TypeError):
        declaration.include_to_many["other"] = True
    with pytest.raises(TypeError):
        declaration.api_target_fields["other"] = ("title",)
