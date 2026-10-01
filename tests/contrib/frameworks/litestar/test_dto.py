"""HareDTO: the hare models a handler returns or takes, shaped by Litestar's DTOs."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from litestar import Litestar, get, post
from litestar.dto import DTOConfig
from litestar.params import FromPath
from litestar.testing import AsyncTestClient

from hare import Hare
from hare.contrib.frameworks.litestar import HareDTO, HarePlugin
from tests.contrib.frameworks.models import Chapter, Tag, Writer

CONFIG = {
    "connections": {"default": "sqlite://:memory:"},
    "apps": {"models": {"models": ["tests.contrib.frameworks.models"], "default_connection": "default"}},
}


@asynccontextmanager
async def create_rows(app: Litestar) -> AsyncGenerator[None]:
    await Hare.generate_schemas()
    anna = await Writer.objects.create(name="anna")
    await Writer.objects.create(name="boris")
    await anna.tags.add(await Tag.objects.create(name="poet"), await Tag.objects.create(name="critic"))
    await Chapter.objects.create(volume=1, number=1, writer=anna, title="Opening")
    await Chapter.objects.create(volume=1, number=2, writer=anna, title="Middle")
    yield


class CamelWriterDTO(HareDTO[Writer]):
    config = DTOConfig(exclude={"chapters", "tags"}, rename_fields={"name": "fullName"})


class RenamedChapterDTO(HareDTO[Chapter]):
    config = DTOConfig(
        max_nested_depth=2,
        include={"title", "writer.name", "writer.tags.name"},
        rename_fields={"writer.name": "fullName"},
    )


class DeepChapterDTO(HareDTO[Chapter]):
    config = DTOConfig(max_nested_depth=2)


class TrimmedChapterDTO(HareDTO[Chapter]):
    config = DTOConfig(max_nested_depth=2, exclude={"writer.chapters", "writer.tags.id", "writer_id"})


@get("/writers/{writer_id:int}")
async def one_writer(writer_id: FromPath[int]) -> Writer:
    return await Writer.objects.get(id=writer_id)


@get("/maybe-writers/{writer_id:int}")
async def maybe_writer(writer_id: FromPath[int]) -> Writer | None:
    return await Writer.objects.get_or_none(id=writer_id)


@get("/writers")
async def all_writers() -> list[Writer]:
    return list(await Writer.objects.all().order_by("id"))


@get("/writers-with-chapters")
async def writers_with_chapters() -> list[Writer]:
    return list(await Writer.objects.all().order_by("id").prefetch_related("chapters"))


@get("/chapters")
async def all_chapters() -> list[Chapter]:
    return list(await Chapter.objects.all().order_by("number"))


@get("/chapters-with-writers")
async def chapters_with_writers() -> list[Chapter]:
    return list(await Chapter.objects.all().order_by("number").select_related("writer"))


@get("/writers-with-tags")
async def writers_with_tags() -> list[Writer]:
    return list(await Writer.objects.all().order_by("id").prefetch_related("tags"))


@get("/deep-chapters", return_dto=DeepChapterDTO)
async def deep_chapters() -> list[Chapter]:
    return list(
        await Chapter.objects.all().order_by("number").select_related("writer").prefetch_related("writer__tags")
    )


@get("/trimmed-chapters", return_dto=TrimmedChapterDTO)
async def trimmed_chapters() -> list[Chapter]:
    return list(
        await Chapter.objects.all().order_by("number").select_related("writer").prefetch_related("writer__tags")
    )


@get("/renamed-chapters", return_dto=RenamedChapterDTO)
async def renamed_chapters() -> list[Chapter]:
    return list(
        await Chapter.objects.all().order_by("number").select_related("writer").prefetch_related("writer__tags")
    )


@get("/renamed-writers", return_dto=CamelWriterDTO)
async def renamed_writers() -> list[Writer]:
    return list(await Writer.objects.all().order_by("id"))


@post("/writers")
async def create_writer(data: Writer) -> Writer:
    await data.save()
    return data


ROUTES = [
    one_writer,
    maybe_writer,
    all_writers,
    writers_with_chapters,
    writers_with_tags,
    deep_chapters,
    trimmed_chapters,
    renamed_chapters,
    all_chapters,
    chapters_with_writers,
    renamed_writers,
    create_writer,
]


def build_app() -> Litestar:
    return Litestar(ROUTES, plugins=[HarePlugin(CONFIG)], lifespan=[create_rows])


@pytest.mark.asyncio
async def test_rows_and_lists_of_rows():
    async with AsyncTestClient(build_app()) as client:
        one = (await client.get("/writers/1")).json()
        many = (await client.get("/writers")).json()
    assert one == {"id": 1, "name": "anna", "tags": None, "chapters": None}
    assert many == [one, {"id": 2, "name": "boris", "tags": None, "chapters": None}]


@pytest.mark.asyncio
async def test_a_relation_is_its_rows_once_loaded_and_null_otherwise():
    async with AsyncTestClient(build_app()) as client:
        chapters = (await client.get("/chapters")).json()
        with_writers = (await client.get("/chapters-with-writers")).json()
        with_chapters = (await client.get("/writers-with-chapters")).json()
    assert chapters[0] == {"volume": 1, "number": 1, "writer": None, "writer_id": 1, "title": "Opening"}
    assert with_writers[0]["writer"] == {"id": 1, "name": "anna"}
    assert [chapter["title"] for chapter in with_chapters[0]["chapters"]] == ["Opening", "Middle"]
    assert "writer" not in with_chapters[0]["chapters"][0]
    assert with_chapters[1]["chapters"] == []


@pytest.mark.asyncio
async def test_a_dto_of_its_own_shapes_the_response():
    async with AsyncTestClient(build_app()) as client:
        writers = (await client.get("/renamed-writers")).json()
    assert writers == [{"id": 1, "fullName": "anna"}, {"id": 2, "fullName": "boris"}]


@pytest.mark.asyncio
async def test_request_data_makes_a_row_without_its_relations():
    async with AsyncTestClient(build_app()) as client:
        created = await client.post("/writers", json={"name": "carl"})
        schema = (await client.get("/schema/openapi.json")).json()
    assert created.status_code == 201
    assert created.json() == {"id": 3, "name": "carl", "tags": None, "chapters": None}
    request_schema = schema["paths"]["/writers"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    request_component = schema["components"]["schemas"][request_schema["$ref"].rsplit("/", 1)[-1]]
    assert set(request_component["properties"]) == {"name"}
    response_schema = schema["paths"]["/writers/{writer_id}"]["get"]["responses"]["200"]["content"]["application/json"]
    response_component = schema["components"]["schemas"][response_schema["schema"]["$ref"].rsplit("/", 1)[-1]]
    assert set(response_component["properties"]) == {"id", "name", "tags", "chapters"}


@pytest.mark.asyncio
async def test_a_many_to_many_relation_is_a_list_of_its_rows():
    async with AsyncTestClient(build_app()) as client:
        writers = (await client.get("/writers-with-tags")).json()
    assert writers[0]["tags"] == [{"id": 1, "name": "poet"}, {"id": 2, "name": "critic"}]
    assert writers[1]["tags"] == []
    assert writers[0]["chapters"] is None


@pytest.mark.asyncio
async def test_the_related_rows_of_related_rows_go_as_deep_as_the_dto_says():
    async with AsyncTestClient(build_app()) as client:
        shallow = (await client.get("/chapters-with-writers")).json()
        deep = (await client.get("/deep-chapters")).json()
    assert shallow[0]["writer"] == {"id": 1, "name": "anna"}
    assert deep[0]["writer"]["tags"] == [{"id": 1, "name": "poet"}, {"id": 2, "name": "critic"}]
    assert deep[0]["writer"]["chapters"] is None


@pytest.mark.asyncio
async def test_a_nested_field_is_named_by_its_path_through_the_relations():
    async with AsyncTestClient(build_app()) as client:
        chapters = (await client.get("/trimmed-chapters")).json()
    assert chapters[0] == {
        "volume": 1,
        "number": 1,
        "title": "Opening",
        "writer": {"id": 1, "name": "anna", "tags": [{"name": "poet"}, {"name": "critic"}]},
    }
    async with AsyncTestClient(build_app()) as client:
        renamed = (await client.get("/renamed-chapters")).json()
    assert renamed[0] == {
        "title": "Opening",
        "writer": {"fullName": "anna", "tags": [{"name": "poet"}, {"name": "critic"}]},
    }


@pytest.mark.asyncio
async def test_a_path_through_the_relations_as_litestar_names_it():
    async with AsyncTestClient(build_app()):
        assert HareDTO.get_litestar_path(Chapter, "writer.name") == "writer.0.name"
        assert HareDTO.get_litestar_path(Chapter, "writer.tags.name") == "writer.0.tags.0.0.name"
        assert HareDTO.get_litestar_path(Writer, "chapters.writer.name") == "chapters.0.0.writer.0.name"
        assert HareDTO.get_litestar_path(Chapter, "writer.0.tags.0.0.name") == "writer.0.tags.0.0.name"
        assert HareDTO.get_litestar_path(Chapter, "writer") == "writer"
        assert HareDTO.get_litestar_path(Chapter, "title") == "title"


@pytest.mark.asyncio
async def test_a_handler_answering_a_row_or_none():
    async with AsyncTestClient(build_app()) as client:
        found = await client.get("/maybe-writers/1")
        missing = await client.get("/maybe-writers/99")
    assert found.json()["name"] == "anna"
    assert missing.status_code == 200
    assert missing.json() is None


@pytest.mark.asyncio
async def test_a_live_model_registered_again_gets_a_dto_of_its_own():
    from litestar.typing import FieldDefinition

    from hare import fields
    from hare.contrib.test.helpers import hare_test_context
    from hare.models import Model

    def build_note(field: fields.Field) -> type[Model]:
        return type(
            "LiveNote",
            (Model,),
            {
                "__module__": __name__,
                "id": fields.IntField(primary_key=True),
                "body": field,
                "Meta": type("Meta", (), {"table": "live_note"}),
            },
        )

    plugin = HarePlugin(CONFIG)
    async with hare_test_context(modules=["tests.contrib.frameworks.models"], app_label="models"):
        first_note = build_note(fields.IntField())
        Hare.register_live_models([first_note], "live")
        first_dto = plugin.create_dto_for_type(FieldDefinition.from_annotation(first_note))
        assert plugin.create_dto_for_type(FieldDefinition.from_annotation(first_note)) is first_dto
        Hare.unregister_live_models([first_note])
        assert first_note not in HarePlugin.model_dtos
        second_note = build_note(fields.CharField(max_length=20))
        Hare.register_live_models([second_note], "live")
        try:
            second_dto = plugin.create_dto_for_type(FieldDefinition.from_annotation(second_note))
            assert second_dto is not first_dto
            assert second_dto.model_type is second_note
            (body,) = [item for item in second_dto.generate_field_definitions(second_note) if item.name == "body"]
            assert body.annotation is str
        finally:
            Hare.unregister_live_models([second_note])
