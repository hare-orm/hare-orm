"""A field named like a parameter of the filter API - ``join_type``, ``connector``, ``negate``,
``exception`` - is filtered like any other: ``Q(...)`` takes nothing but conditions, and
``.filter()``/``.exclude()`` keep their own parameters apart from the filters."""

from __future__ import annotations

import os
import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.models import Model
from hare.query.enums import Connector
from hare.query.expressions import Q
from tests.utils.multi_database_context import MultiDatabaseTestContext

MODULE_NAME = "tests._reserved_filter_names_models"


@pytest_asyncio.fixture
async def link_model():
    class Link(Model):
        id = fields.IntField(primary_key=True)
        join_type = fields.CharField(max_length=10)
        connector = fields.CharField(max_length=10)
        negate = fields.BooleanField(default=False)
        exception = fields.CharField(max_length=20, default="")

        class Meta:
            app = "reserved_filter_names"

    module = types.ModuleType(MODULE_NAME)
    setattr(module, "Link", Link)  # noqa: B010
    sys.modules[MODULE_NAME] = module
    db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    try:
        async with MultiDatabaseTestContext.open(
            db_url,
            ["default"],
            apps={"reserved_filter_names": {"models": [MODULE_NAME], "default_connection": "default"}},
        ) as ctx:
            await ctx.generate_schemas()
            await Link.objects.create(id=1, join_type="inner", connector="usb", negate=False, exception="timeout")
            await Link.objects.create(id=2, join_type="left", connector="hdmi", negate=True, exception="")
            yield Link
    finally:
        sys.modules.pop(MODULE_NAME, None)


@pytest.mark.asyncio
async def test_q_filters_a_field_named_join_type_or_connector(link_model):
    Link = link_model
    assert await Link.objects.filter(Q(join_type="inner")).values_list("id", flat=True) == [1]
    assert await Link.objects.filter(Q(connector="hdmi")).values_list("id", flat=True) == [2]
    either = Q.with_connector(Connector.OR, join_type="inner", connector="hdmi")
    assert sorted(await Link.objects.filter(either).values_list("id", flat=True)) == [1, 2]


@pytest.mark.asyncio
async def test_filter_and_exclude_take_a_field_named_join_type_or_negate(link_model):
    Link = link_model
    assert await Link.objects.filter(join_type="left").values_list("id", flat=True) == [2]
    assert await Link.objects.filter(negate=True).values_list("id", flat=True) == [2]
    assert await Link.objects.exclude(negate=True).values_list("id", flat=True) == [1]
    assert await Link.objects.all().filter(connector="usb", negate=False).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_get_takes_a_field_named_like_its_own_parameter_through_q(link_model):
    Link = link_model
    assert (await Link.objects.get(Q(exception="timeout"))).id == 1
    assert (await Link.objects.get(join_type="left")).id == 2
