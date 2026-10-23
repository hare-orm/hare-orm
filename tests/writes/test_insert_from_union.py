"""insert_from() of a union() refused to run when the target model had an auto_now/auto_now_add field -
it annotated the source with the field's moment, and a combined queryset takes no annotation."""

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.models import Model

pytestmark = pytest.mark.sqlite_only


class Shelf(Model):
    id = fields.IntField(primary_key=True, generated=False)
    label = fields.CharField(max_length=20)

    class Meta:
        app = "insert_from_union"
        table = "insert_union_shelf"


class Archive(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=20)
    created = fields.DatetimeField(auto_now_add=True)

    class Meta:
        app = "insert_from_union"
        table = "insert_union_archive"


@pytest.mark.asyncio
async def test_a_union_is_inserted_with_the_moment_of_an_auto_now_field():
    async with hare_test_context(
        modules=["tests.writes.test_insert_from_union"], app_label="insert_from_union", connection_label="models"
    ):
        await Shelf.objects.bulk_create([Shelf(id=1, label="a"), Shelf(id=2, label="b"), Shelf(id=3, label="c")])
        source = Shelf.objects.filter(id__lte=2).values("label").union(Shelf.objects.filter(id=3).values("label"))
        assert await Archive.objects.insert_from(source, fields=["label"]) == 3
        rows = await Archive.objects.order_by("label").values_list("label", "created")
        assert [label for label, _created in rows] == ["a", "b", "c"]
        assert all(created is not None for _label, created in rows)
