"""A CharEnumField's label holding a backslash, a new line, a tab or a quote reads back as its member: the
drivers read the labels off the text of the type, where the server escapes them - clickhouse-connect keeps
the escapes, clickhouse-driver drops the backslashes."""

import pytest

from tests.dialects.clickhouse.models import EscapedLabel, Labelled


@pytest.mark.asyncio
async def test_escaped_labels_read_back_as_their_members(clickhouse_db):
    await Labelled.objects.bulk_create([Labelled(id=number, label=label) for number, label in enumerate(EscapedLabel)])
    assert await Labelled.objects.order_by("id").values_list("label", flat=True) == list(EscapedLabel)
    assert [row.label for row in await Labelled.objects.order_by("id")] == list(EscapedLabel)
    assert await Labelled.objects.filter(label=EscapedLabel.NEW_LINE).values_list("id", flat=True) == [1]
