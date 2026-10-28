"""HareDTO's extra fields: a query's annotations and attributes set on a row, properties and methods of the
model, values of related rows - on the rows of the response and, by a path, on related rows."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from litestar import Litestar, get
from litestar.di import NamedDependency
from litestar.dto import DTOConfig
from litestar.testing import AsyncTestClient
from pydantic import BaseModel

from hare import Hare
from hare.contrib.frameworks.litestar import HareDTO, HarePlugin, RequestQueryDIPlugin
from hare.contrib.frameworks.litestar.hare_dto_extra_fields import HareDTOExtraFields
from hare.contrib.request_query import OrderingConfig, Page, RequestQuery
from hare.exceptions import ConfigurationError
from hare.query.functions import Count
from tests.contrib.frameworks.models import Chapter, Writer

REQUIRES = ("litestar",)

pytestmark = pytest.mark.sqlite_only

CONFIG = {
    "connections": {"default": "sqlite+aiosqlite://:memory:"},
    "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
}


class Profile(BaseModel):
    city: str


class CountedWriterDTO(HareDTO[Writer]):
    config = DTOConfig(exclude={"chapters", "tags"})
    extra_fields = {"chapter_count": int, "profile": Profile}
    computed = ("initial", "shouted_name")


class FlatChapterDTO(HareDTO[Chapter]):
    config = DTOConfig(exclude={"writer.chapters", "writer.tags"})
    extra_fields = {"writer.chapter_count": int}
    computed = ("writer.initial",)
    related_values = {"writer_name": "writer.name"}


class WriterPageQuery(RequestQuery[Writer]):
    class Meta:
        queryset = Writer.objects.all().annotate(chapter_count=Count("chapters"))
        ordering = OrderingConfig(default=("id",))


@asynccontextmanager
async def create_rows(app: Litestar) -> AsyncGenerator[None]:
    await Hare.generate_schemas()
    anna = await Writer.objects.create(name="anna")
    await Writer.objects.create(name="boris")
    await Chapter.objects.create(volume=1, number=1, writer=anna, title="Opening")
    await Chapter.objects.create(volume=1, number=2, writer=anna, title="Middle")
    yield


@get("/counted-writers", return_dto=CountedWriterDTO)
async def counted_writers() -> list[Writer]:
    writers = await Writer.objects.annotate(chapter_count=Count("chapters")).order_by("id")
    writers[0].profile = Profile(city="Kazan")
    return writers


@get("/plain-writers", return_dto=CountedWriterDTO)
async def plain_writers() -> list[Writer]:
    return await Writer.objects.order_by("id")


@get("/partial-writers", return_dto=CountedWriterDTO)
async def partial_writers() -> list[Writer]:
    return await Writer.objects.only("id").order_by("id")


@get("/flat-chapters", return_dto=FlatChapterDTO)
async def flat_chapters() -> list[Chapter]:
    chapters = await Chapter.objects.select_related("writer").order_by("number")
    chapters[0].writer.chapter_count = 2
    return chapters


@get("/unloaded-chapters", return_dto=FlatChapterDTO)
async def unloaded_chapters() -> list[Chapter]:
    return await Chapter.objects.order_by("number")


@get(
    "/writer-pages",
    return_dto=CountedWriterDTO,
    dependencies={"writers": RequestQueryDIPlugin.provide(WriterPageQuery)},
)
async def writer_pages(writers: NamedDependency[WriterPageQuery]) -> Page[Writer]:
    return await writers.page()


def build_app() -> Litestar:
    return Litestar(
        [counted_writers, plain_writers, partial_writers, flat_chapters, unloaded_chapters, writer_pages],
        plugins=[HarePlugin(CONFIG)],
        lifespan=[create_rows],
    )


@pytest.mark.asyncio
async def test_an_annotation_an_attribute_a_property_and_a_method_are_answered():
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/counted-writers")
    assert response.json() == [
        {
            "id": 1,
            "name": "anna",
            "chapter_count": 2,
            "profile": {"city": "Kazan"},
            "initial": "A",
            "shouted_name": "ANNA",
        },
        {"id": 2, "name": "boris", "chapter_count": 0, "profile": None, "initial": "B", "shouted_name": "BORIS"},
    ]


@pytest.mark.asyncio
async def test_a_value_the_query_did_not_make_is_null():
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/plain-writers")
    assert [writer["chapter_count"] for writer in response.json()] == [None, None]
    assert [writer["profile"] for writer in response.json()] == [None, None]


@pytest.mark.asyncio
async def test_related_rows_get_their_extra_fields_and_a_related_value_is_flattened():
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/flat-chapters")
    first, second = response.json()
    assert first["writer_name"] == "anna"
    assert first["writer"] == {"id": 1, "name": "anna", "chapter_count": 2, "initial": "A"}
    # Each chapter's writer is a row of its own: the attribute was set on the first one only.
    assert second["writer"] == {"id": 1, "name": "anna", "chapter_count": None, "initial": "A"}
    assert second["writer_name"] == "anna"


@pytest.mark.asyncio
async def test_a_related_value_of_a_relation_not_loaded_is_null():
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/unloaded-chapters")
    assert [(chapter["writer"], chapter["writer_name"]) for chapter in response.json()] == [(None, None), (None, None)]


@pytest.mark.asyncio
async def test_a_page_of_rows_carries_their_extra_fields():
    async with AsyncTestClient(build_app()) as client:
        page = (await client.get("/writer-pages")).json()
    assert [(writer["name"], writer["chapter_count"], writer["initial"]) for writer in page["result"]] == [
        ("anna", 2, "A"),
        ("boris", 0, "B"),
    ]
    assert page["count"] == 2


@pytest.mark.asyncio
async def test_the_openapi_schema_lists_the_extra_fields_with_their_types():
    async with AsyncTestClient(build_app()) as client:
        schema = (await client.get("/schema/openapi.json")).json()
    # CountedWriterDTO's rows - of /counted-writers, /plain-writers, /partial-writers and the page - are the ones with
    # shouted_name.
    writer_schemas = [
        component["properties"]
        for component in schema["components"]["schemas"].values()
        if "shouted_name" in component.get("properties", {})
    ]
    assert len(writer_schemas) == 4
    for properties in writer_schemas:
        assert {"chapter_count", "profile", "initial", "shouted_name"} <= set(properties)
        assert {"type": "integer"} in properties["chapter_count"]["oneOf"]
        assert {"type": "string"} in properties["initial"]["oneOf"]


class UnknownRelationDTO(HareDTO[Chapter]):
    extra_fields = {"editor.count": int}


class NotComputedDTO(HareDTO[Writer]):
    computed = ("name_length",)


class UnannotatedComputedDTO(HareDTO[Writer]):
    computed = ("loose_name",)


class ToManyRelatedValueDTO(HareDTO[Writer]):
    related_values = {"chapter_title": "chapters.title"}


class RelationEndingRelatedValueDTO(HareDTO[Chapter]):
    related_values = {"writer_tags": "writer.tags"}


class FieldNamedExtraDTO(HareDTO[Writer]):
    extra_fields = {"name": str}


class TwiceDeclaredDTO(HareDTO[Writer]):
    extra_fields = {"initial": str}
    computed = ("initial",)


@pytest.mark.parametrize(
    ("dto", "message"),
    [
        (UnknownRelationDTO, "'editor' is no relation of Chapter"),
        (NotComputedDTO, "'name_length' is no property or method"),
        (UnannotatedComputedDTO, "'loose_name' has no return annotation"),
        (ToManyRelatedValueDTO, "'chapters' is no relation of Writer to one row"),
        (RelationEndingRelatedValueDTO, "names no value field of a related row"),
        (FieldNamedExtraDTO, "'name' is a field of the model or declared twice"),
        (TwiceDeclaredDTO, "'initial' is a field of the model or declared twice"),
    ],
    ids=[
        "unknown relation",
        "not computed",
        "no return annotation",
        "to-many path",
        "path to a relation",
        "field name",
        "declared twice",
    ],
)
def test_a_wrong_declaration_is_refused_when_the_handler_is_built(dto, message, monkeypatch):
    monkeypatch.setattr(Writer, "loose_name", lambda writer: writer.name, raising=False)

    async def wrong() -> list:
        return []

    wrong.__annotations__["return"] = list[dto.model_type]

    with pytest.raises(ConfigurationError, match=message):
        Litestar([get("/wrong", return_dto=dto)(wrong)], plugins=[HarePlugin(CONFIG)])


@pytest.mark.asyncio
async def test_a_property_reading_a_field_the_query_did_not_select_is_null():
    """Python takes an AttributeError of a property for a missing property and asks the model's __getattr__,
    which hid the field the query didn't select behind an error naming the property."""
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/partial-writers")
    assert response.status_code == 200
    assert [(writer["initial"], writer["shouted_name"]) for writer in response.json()] == [(None, None), (None, None)]


@pytest.mark.asyncio
async def test_an_attribute_error_inside_a_property_is_an_error_not_null(monkeypatch):
    def broken_initial(writer: Writer) -> str:
        return writer.nickname  # type: ignore[attr-defined]

    monkeypatch.setattr(Writer, "initial", property(broken_initial))
    async with AsyncTestClient(build_app()) as client:
        response = await client.get("/counted-writers")
    assert response.status_code == 500


def test_a_computed_value_of_a_row_reads_the_property_of_its_own_class():
    class LoudWriter(Writer):
        @property
        def initial(self) -> str:
            return "!"

    extra_fields = HareDTOExtraFields(Writer, {}, ("initial",), {}, HareDTO.read_attribute)
    # Only the class of the row matters - its property reads no field.
    rows = (Writer(name="anna"), LoudWriter.__new__(LoudWriter))
    readings = [extra_fields.find(row, "initial").read(row) for row in rows]
    assert readings == ["A", "!"]
