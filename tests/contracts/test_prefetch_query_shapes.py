"""Sliced prefetches preserve masks, duplicates and composite identities within and across databases."""

import sys
import types

import pytest
import pytest_asyncio

from hare import fields
from hare.models import Model
from hare.query.expressions import Window
from hare.query.functions.window import RowNumber
from hare.query.relation_loading import Prefetch
from tests.utils.database_under_test import DatabaseUnderTest
from tests.utils.multi_database_context import MultiDatabaseTestContext


@pytest_asyncio.fixture(params=[False, True], ids=["same_connection", "cross_connection"])
async def prefetch_contract(request, monkeypatch):
    class QueryTag(Model):
        code = fields.CharField(max_length=12)
        number = fields.IntField()
        rank = fields.IntField()
        deleted_at = fields.DatetimeField(null=True)
        pk = fields.CompositePrimaryKey("code", "number")

        class Meta:
            app = "contract_target"
            soft_delete_field = "deleted_at"

    class QueryParent(Model):
        a = fields.IntField()
        b = fields.IntField()
        tags = fields.ManyToManyField("contract_target.QueryTag", related_name="parents", db_constraint=False)
        pk = fields.CompositePrimaryKey("a", "b")

        class Meta:
            app = "contract_parent"

    target_connection = "target" if request.param else "parent"
    apps = {}
    for model, connection in ((QueryTag, target_connection), (QueryParent, "parent")):
        module_name = f"tests._{model.Meta.app}_query_shapes"
        module = types.ModuleType(module_name)
        setattr(module, model.__name__, model)
        monkeypatch.setitem(sys.modules, module_name, module)
        apps[model.Meta.app] = {"models": [module_name], "default_connection": connection}
    async with MultiDatabaseTestContext.open(
        DatabaseUnderTest.get_url(), sorted({"parent", target_connection}), apps=apps
    ) as context:
        await context.generate_schemas()
        tags = [await QueryTag.objects.create(code="same", number=number, rank=number) for number in (1, 2, 3, 4)]
        parents = [await QueryParent.objects.create(a=first, b=second) for first, second in ((1, 1), (1, 2), (2, 1))]
        links = [[1, 2, 3], [2, 3, 4], []]
        for parent, numbers in zip(parents, links, strict=True):
            await parent.tags.add(*(tags[number - 1] for number in numbers))
        await tags[1].delete()
        yield QueryParent, QueryTag, links, request.param


@pytest.mark.asyncio
async def test_sliced_prefetch_shapes_match_the_linked_rows(prefetch_contract):
    parent_model, tag_model, links, cross_connection = prefetch_contract
    cells = [
        ("plain", False),
        ("only", False),
        ("defer", True),
        ("window_only", True),
        ("distinct", True),
    ]
    if not cross_connection:
        cells.extend((("fanout_only", False), ("distinct_fanout", True)))
    for shape, include_deleted in cells:
        child = tag_model.objects.include_deleted() if include_deleted else tag_model.objects.all()
        child = child.order_by("-rank", "code", "number")
        if "fanout" in shape:
            child = child.filter(parents__b__gte=1)
        if "distinct" in shape:
            child = child.distinct()
        if "only" in shape:
            child = child.only("pk", "rank")
        if shape == "defer":
            child = child.defer("deleted_at")
        if "window" in shape:
            child = child.annotate(position=Window(RowNumber(), order_by=("-rank", "code", "number")))
        visible = {number for numbers in links for number in numbers if include_deleted or number != 2}
        positions = {number: position for position, number in enumerate(sorted(visible, reverse=True), 1)}
        for start, stop in ((0, 1), (1, 3), (1, None), (0, 0)):
            query = (
                parent_model.objects.only("a", "b")
                .order_by("a", "b")
                .prefetch_related(Prefetch("tags", queryset=child[start:stop], to_attribute="picked"))
            )
            expected = []
            for numbers in links:
                selected = []
                for number in numbers:
                    if number not in visible:
                        continue
                    repeats = sum(number in other for other in links) if shape == "fanout_only" else 1
                    selected.extend([number] * repeats)
                expected.append(sorted(selected, reverse=True)[start:stop])
            parents = await query
            assert [[tag.number for tag in parent.picked] for parent in parents] == expected, (shape, start, stop)
            for parent, numbers in zip(parents, expected, strict=True):
                for tag, number in zip(parent.picked, numbers, strict=True):
                    assert tag.pk == ("same", number)
                    assert tag.rank == number
                    if "window" in shape:
                        assert tag.position == positions[number]
                    partial = "only" in shape or shape == "defer"
                    assert tag._partial == partial
                    assert ("deleted_at" in vars(tag)) != partial
