"""pydantic_model_creator's extra fields and related values: a query's annotations and attributes set on a row,
values of related rows - on the schema of the model and, by a path, on the schemas of related rows."""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from hare import fields
from hare.contrib.pydantic import pydantic_model_creator
from hare.contrib.test import hare_test_context
from hare.exceptions import ConfigurationError
from hare.models import Model
from hare.query.functions import Count
from tests.contrib.frameworks.models import Chapter, Writer

REQUIRES = ("pydantic",)

pytestmark = pytest.mark.sqlite_only

MODELS = ["tests.contrib.frameworks.models"]
DB_URL = "sqlite+aiosqlite://:memory:"


class Profile(BaseModel):
    city: str


class Vault(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=20)
    token = fields.CharField(max_length=20, sensitive=True)


class Locker(Model):
    id = fields.IntField(primary_key=True)
    vault = fields.ForeignKeyField("models.Vault", related_name="lockers")


async def create_rows() -> None:
    anna = await Writer.objects.create(name="anna")
    await Writer.objects.create(name="boris")
    await Chapter.objects.create(volume=1, number=1, writer=anna, title="Opening")
    await Chapter.objects.create(volume=1, number=2, writer=anna, title="Middle")


@pytest.mark.asyncio
async def test_an_annotation_and_an_attribute_are_read_and_a_missing_one_is_none():
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(
            Writer, exclude=("chapters", "tags"), extra_fields={"chapter_count": int, "profile": Profile}
        )
        await create_rows()
        anna, boris = await Writer.objects.annotate(chapter_count=Count("chapters")).order_by("id")
        anna.profile = Profile(city="Kazan")
        plain = await Writer.objects.get(name="anna")

        assert schema.model_validate(anna).model_dump() == {
            "id": 1,
            "name": "anna",
            "chapter_count": 2,
            "profile": {"city": "Kazan"},
        }
        assert schema.model_validate(boris).model_dump()["chapter_count"] == 0
        assert schema.model_validate(plain).model_dump() == {
            "id": 1,
            "name": "anna",
            "chapter_count": None,
            "profile": None,
        }


@pytest.mark.asyncio
async def test_a_related_value_is_flattened_and_none_for_a_relation_not_loaded():
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(Chapter, exclude=("writer",), related_values={"writer_name": "writer.name"})
        await create_rows()
        loaded = await Chapter.objects.select_related("writer").get(number=1)
        not_loaded = await Chapter.objects.get(number=1)

        assert schema.model_validate(loaded).model_dump()["writer_name"] == "anna"
        assert schema.model_validate(not_loaded).model_dump()["writer_name"] is None


@pytest.mark.asyncio
async def test_a_related_value_is_taken_by_its_own_name_from_a_mapping():
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(Chapter, exclude=("writer",), related_values={"writer_name": "writer.name"})

    validated = schema.model_validate(
        {"volume": 1, "number": 1, "title": "Opening", "writer_id": 1, "writer_name": "anna"}
    )

    assert validated.writer_name == "anna"


@pytest.mark.asyncio
async def test_a_path_declares_them_on_the_schema_of_related_rows():
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(
            Chapter,
            exclude=("writer.chapters", "writer.tags"),
            extra_fields={"writer.chapter_count": int},
            computed=("writer.initial",),
            related_values={"writer_name": "writer.name"},
        )
        await create_rows()
        chapter = await Chapter.objects.select_related("writer").get(number=1)
        chapter.writer.chapter_count = 2

        dumped = schema.model_validate(chapter).model_dump()

    assert dumped["writer"] == {"id": 1, "name": "anna", "chapter_count": 2, "initial": "A"}
    assert dumped["writer_name"] == "anna"


@pytest.mark.parametrize("declaration", ["call", "meta"])
@pytest.mark.parametrize(
    ("selection", "selected"),
    [
        ({"exclude": ("position", "writer_name")}, set()),
        ({"include": ("title",)}, set()),
        ({"include": ("title", "position")}, {"position"}),
        ({"include": ("title", "writer_name")}, {"writer_name"}),
    ],
    ids=["exclude-extra-values", "include-model-field", "include-extra-field", "include-related-value"],
)
@pytest.mark.asyncio
async def test_extra_values_follow_the_schema_field_selection(declaration, selection, selected):
    options = {"extra_fields": {"position": int}, "related_values": {"writer_name": "writer.name"}, **selection}
    if declaration == "meta":
        options = {"meta_override": type("SelectedFields", (), options)}
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(Chapter, **options)
    assert {"position", "writer_name"} & schema.model_fields.keys() == selected
    assert schema.related_value_paths == (("writer.name",) if "writer_name" in selected else ())


@pytest.mark.parametrize("include_extra", [False, True])
@pytest.mark.asyncio
async def test_extra_value_selection_follows_relation_paths(include_extra):
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(
            Chapter,
            include=("title", "writer.name", *(["writer.chapter_count"] if include_extra else [])),
            extra_fields={"writer.chapter_count": int},
        )
        dumped = schema.model_validate(
            SimpleNamespace(title="Opening", writer=SimpleNamespace(name="anna", chapter_count=2))
        ).model_dump()
    expected_writer = {"name": "anna", **({"chapter_count": 2} if include_extra else {})}
    assert dumped == {"title": "Opening", "writer": expected_writer}


@pytest.mark.asyncio
async def test_the_json_schema_lists_them_nullable_with_their_types():
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(
            Chapter,
            exclude=("writer",),
            extra_fields={"position": int},
            related_values={"writer_name": "writer.name"},
        )

    properties = schema.model_json_schema()["properties"]

    assert properties["position"]["anyOf"] == [{"type": "integer"}, {"type": "null"}]
    assert {"type": "string"} in properties["writer_name"]["anyOf"]
    assert {"type": "null"} in properties["writer_name"]["anyOf"]


@pytest.mark.asyncio
async def test_other_declarations_build_other_schemas():
    """The schemas are kept by a hash of what shapes them - a related value and an extra field of one name
    and type would otherwise share one schema."""
    async with hare_test_context(MODELS, db_url=DB_URL):
        related = pydantic_model_creator(Chapter, exclude=("writer",), related_values={"label": "writer.name"})
        extra = pydantic_model_creator(Chapter, exclude=("writer",), extra_fields={"label": str})

        assert related is not extra
        assert pydantic_model_creator(Chapter, exclude=("writer",), related_values={"label": "writer.name"}) is related
        assert pydantic_model_creator(Chapter, exclude=("writer",), extra_fields={"label": str}) is extra


@pytest.mark.asyncio
async def test_pydantic_meta_declares_them_too():
    class ExtraMeta:
        extra_fields = {"chapter_count": int}
        exclude = ("chapters", "tags")

    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(Writer, meta_override=ExtraMeta)

    assert "chapter_count" in schema.model_fields


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"extra_fields": {"editor.count": int}}, "'editor' is no relation of the model"),
        ({"extra_fields": {"title": str}}, "'title' is a field of the schema or declared twice"),
        (
            {"extra_fields": {"label": str}, "related_values": {"label": "writer.name"}},
            "'label' is a field of the schema or declared twice",
        ),
        ({"related_values": {"label": "writer.chapters.title"}}, "'chapters' is no relation of Writer to one row"),
        ({"related_values": {"label": "writer.tags"}}, "names no value field of a related row"),
        ({"related_values": {"label": "title"}}, "names no value field of a related row"),
    ],
    ids=["unknown relation", "field name", "declared twice", "to-many path", "path to a relation", "own field"],
)
@pytest.mark.asyncio
async def test_a_wrong_declaration_is_refused(options, message):
    async with hare_test_context(MODELS, db_url=DB_URL):
        with pytest.raises(ConfigurationError, match=message):
            pydantic_model_creator(Chapter, exclude=("writer",), **options)


@pytest.mark.asyncio
async def test_a_write_schema_gets_none_of_them():
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(
            Chapter,
            exclude=("writer",),
            exclude_readonly=True,
            extra_fields={"position": int},
            related_values={"writer_name": "writer.name"},
        )

    assert "position" not in schema.model_fields
    assert "writer_name" not in schema.model_fields


@pytest.mark.asyncio
async def test_they_are_sorted_with_the_other_fields():
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(
            Chapter,
            exclude=("writer",),
            sort_alphabetically=True,
            extra_fields={"position": int},
            related_values={"author_name": "writer.name"},
        )

    assert list(schema.model_fields) == sorted(schema.model_fields)


@pytest.mark.parametrize("relations_as_ids", [False, True], ids=["relation excluded", "relations as ids"])
@pytest.mark.asyncio
async def test_from_hare_orm_and_from_queryset_fetch_the_relations_of_a_related_value(relations_as_ids):
    async with hare_test_context(MODELS, db_url=DB_URL):
        schema = pydantic_model_creator(
            Chapter,
            exclude=() if relations_as_ids else ("writer",),
            relations_as_ids=relations_as_ids,
            related_values={"writer_name": "writer.name"},
        )
        await create_rows()

        from_row = await schema.from_hare_orm(await Chapter.objects.get(number=1))
        from_queryset = await schema.from_queryset(Chapter.objects.order_by("number"))

    assert from_row.writer_name == "anna"
    assert [chapter.writer_name for chapter in from_queryset] == ["anna", "anna"]


@pytest.mark.asyncio
async def test_exclude_sensitive_drops_a_related_value_of_a_sensitive_field():
    async with hare_test_context([__name__], db_url=DB_URL):
        options = {
            "exclude": ("vault",),
            "related_values": {"vault_label": "vault.label", "vault_token": "vault.token"},
        }
        public = pydantic_model_creator(Locker, exclude_sensitive=True, **options)
        private = pydantic_model_creator(Locker, **options)
        vault = await Vault.objects.create(label="front", token="s3cret")
        await Locker.objects.create(vault=vault)

        public_row = (await public.from_queryset(Locker.objects.all()))[0]
        private_row = (await private.from_queryset(Locker.objects.all()))[0]

    assert public_row.model_dump() == {"id": 1, "vault_id": 1, "vault_label": "front"}
    assert private_row.vault_token == "s3cret"
