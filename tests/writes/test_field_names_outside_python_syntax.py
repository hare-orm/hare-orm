"""bulk_create of a model built with type() whose field name is no Python attribute name ("f 1") raised
a bare SyntaxError from the compiled row serializer, which wrote the name as an attribute access."""

import pytest

from hare import fields
from hare.contrib.test.isolated_contexts import hare_test_context
from hare.models import Model
from hare.query.rows.native.hydrate_accelerator import HydrateAccelerator

pytestmark = pytest.mark.sqlite_only

OddNames = type(
    "OddNames",
    (Model,),
    {
        "__module__": __name__,
        "id": fields.IntField(primary_key=True, generated=False),
        "f 1": fields.IntField(default=0),
        "class": fields.IntField(default=0),
        "Meta": type("Meta", (), {"app": "odd_names", "table": "odd_names"}),
    },
)


@pytest.mark.parametrize("native", [True, False], ids=["native", "python"])
@pytest.mark.asyncio
async def test_rows_of_fields_named_outside_python_syntax_are_written(monkeypatch, native):
    if not native:
        monkeypatch.setattr(HydrateAccelerator, "module", None)
    async with hare_test_context(modules=[__name__], app_label="odd_names", connection_label="models"):
        await OddNames.objects.bulk_create([OddNames(**{"id": 1, "f 1": 5, "class": 7})])
        await OddNames.objects.create(**{"id": 2, "f 1": 6, "class": 8})
        rows = [(getattr(obj, "f 1"), getattr(obj, "class")) for obj in await OddNames.objects.order_by("id")]
        assert rows == [(5, 7), (6, 8)]
