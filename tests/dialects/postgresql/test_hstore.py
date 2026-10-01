"""HStoreField: its value, lookups and key/keys/values transforms."""

import os
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio

from hare.contrib.test.helpers import hare_test_context, truncate_all_models
from hare.dialects.postgresql.fields.hstore import HStoreField, HStoreText
from hare.dialects.registry import DialectRegistry
from hare.exceptions import UnSupportedError, ValidationError
from hare.query.expressions import F, Q
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_hstore import Item, Shelf


def test_text_form_round_trips():
    mapping = {"a": "1", "b": None, 'q"\\': 'x"y\\', "": "", "é": "=>,"}
    assert HStoreText.parse(HStoreText.encode(mapping)) == mapping
    assert HStoreText.parse('"a"=>"1", "b"=>NULL') == {"a": "1", "b": None}
    assert HStoreText.parse("") == {}
    for invalid in ('"a"=>"1', '"a" "b"', "=>x"):
        with pytest.raises(ValueError):
            HStoreText.parse(invalid)


def test_sql_type_is_postgres_only():
    field = HStoreField()
    field.model_field_name = "attributes"
    assert field.get_column_type(DialectRegistry.get_dialect("postgresql")) == "hstore"
    with pytest.raises(UnSupportedError, match="HStoreField.*attributes.*sqlite"):
        field.get_column_type(DialectRegistry.get_dialect("sqlite"))


@pytest_asyncio.fixture(scope="module")
async def hstore_context() -> AsyncGenerator[Any]:
    skip_if_not_postgres()
    db_url = os.environ["HARE_TEST_DB"].replace("\\{", "{").replace("\\}", "}")
    db_url = db_url.format(uuid.uuid4().hex) if "{}" in db_url else db_url
    async with hare_test_context(
        ["tests.dialects.postgresql.models_hstore"], db_url=db_url, app_label="models"
    ) as ctx:
        yield ctx


@pytest_asyncio.fixture
async def items(hstore_context: Any) -> AsyncGenerator[None]:
    shelf = await Shelf.objects.create(id=1)
    await Item.objects.create(id=1, shelf=shelf, attributes={"color": "red", "size": "L", "note": None})
    await Item.objects.create(id=2, attributes={"color": "blue", 'we"ird\\': "x=>y, z"})
    await Item.objects.create(id=3, attributes={})
    await Item.objects.create(id=4, attributes=None)
    await Item.objects.create(id=5, attributes={"n": 5, 7: True})
    yield
    await truncate_all_models()


async def matching(*conditions, **lookups):
    return sorted(await Item.objects.filter(*conditions, **lookups).values_list("id", flat=True))


@pytest.mark.asyncio
async def test_value_round_trips(items):
    assert await Item.objects.all().order_by("id").values_list("attributes", flat=True) == [
        {"color": "red", "size": "L", "note": None},
        {"color": "blue", 'we"ird\\': "x=>y, z"},
        {},
        None,
        {"n": "5", "7": "True"},
    ]
    item = await Item.objects.get(id=1)
    item.attributes["size"] = "M"
    await item.save()
    await item.refresh_from_db()
    assert item.attributes == {"color": "red", "size": "M", "note": None}
    await Item.objects.bulk_create([Item(id=6, attributes={"b": "1"})])
    assert await Item.objects.get(id=6).values_list("attributes", flat=True) == {"b": "1"}
    with pytest.raises(ValidationError, match="expected a dict"):
        await Item.objects.create(id=8, attributes=["x"])
    with pytest.raises(ValidationError, match="null byte"):
        await Item.objects.create(id=9, attributes={"a": "b\x00"})


@pytest.mark.asyncio
async def test_lookups(items):
    assert await matching(attributes={"color": "blue", 'we"ird\\': "x=>y, z"}) == [2]
    assert await matching(attributes={}) == [3]
    assert await matching(attributes__isnull=True) == [4]
    assert await matching(attributes__in=[{"color": "red", "size": "L", "note": None}, {}]) == [1, 3]
    assert await matching(attributes__contains={"color": "red"}) == [1]
    assert await matching(attributes__contains={"note": None}) == [1]
    assert await matching(attributes__contained_by={"color": "blue", 'we"ird\\': "x=>y, z", "a": "b"}) == [2, 3]
    assert await matching(attributes__has_key="size") == [1]
    assert await matching(attributes__has_keys=["color", "size"]) == [1]
    assert await matching(attributes__has_any_keys=["n", "size"]) == [1, 5]


@pytest.mark.asyncio
async def test_key_keys_and_values_transforms(items):
    assert await matching(attributes__color="red") == [1]
    assert await matching(attributes__color__in=["red", "blue"]) == [1, 2]
    assert await matching(attributes__color__startswith="bl") == [2]
    assert await matching(attributes__color__isnull=True) == [3, 4, 5]
    assert await matching(~Q(attributes__color="red")) == [2, 3, 4, 5]
    assert await matching(attributes__keys__contains=["size"]) == [1]
    assert await matching(attributes__keys__len=3) == [1]
    assert await matching(attributes__values__contains=["red"]) == [1]
    assert await Shelf.objects.filter(items__attributes__color="red").values_list("id", flat=True) == [1]
    colors = Item.objects.all().order_by("id").values_list("attributes__color", flat=True)
    assert await colors == ["red", "blue", None, None, None]
    by_color = Item.objects.filter(attributes__color__isnull=False).order_by("-attributes__color")
    assert await by_color.values_list("id", flat=True) == [1, 2]
    blue = Item.objects.annotate(color=F("attributes__color")).filter(color="blue").values_list("id", flat=True)
    assert await blue == [2]
