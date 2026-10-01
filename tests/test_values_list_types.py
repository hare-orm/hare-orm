"""The rows values_list() is typed with: a tuple per row by default, the value alone with
``flat=True`` and a namedtuple with ``named=True`` (typed as ``Any``). Checked by mypy
(``assert_type``) and run against the database for the values themselves."""

from typing import Any, assert_type

import pytest

from tests.testmodels import Tournament


@pytest.mark.asyncio
async def test_values_list_rows_are_typed_by_flat_and_named(db):
    await Tournament.objects.create(name="a")
    rows = await Tournament.objects.all().values_list("id", "name")
    assert_type(rows, list[tuple[Any, ...]])
    names = await Tournament.objects.all().values_list("name", flat=True)
    assert_type(names, list[Any])
    assert names == ["a"]
    named_rows = await Tournament.objects.all().values_list("name", named=True)
    assert_type(named_rows, list[Any])
    assert named_rows[0].name == "a"
    single_name = await Tournament.objects.all().values_list("name", flat=True).get(name="a")
    assert_type(single_name, Any)
    assert single_name == "a"
    first_row = await Tournament.objects.all().values_list("id", "name").first()
    assert_type(first_row, tuple[Any, ...])
    model_get_name = await Tournament.objects.get(name="a").values_list("name", flat=True)
    assert_type(model_get_name, Any)
    assert model_get_name == "a"
    sliced_names = await Tournament.objects.all().order_by("id").values_list("name", flat=True)[0:1]
    assert_type(sliced_names, list[Any])
    assert sliced_names == ["a"]
