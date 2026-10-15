"""with_recursive() on ClickHouse: its recursive query adds rows by UNION ALL alone, so a walk ends
at max_depth - through a cycle too - and a walk without one is refused before it runs."""

import pytest

from hare.exceptions import UnSupportedError
from tests.dialects.clickhouse.models import Folder


@pytest.mark.asyncio
async def test_a_walk_reaches_every_level_up_to_its_depth(clickhouse_db):
    await Folder.objects.create(id=1)
    await Folder.objects.create(id=2, parent_id=1)
    await Folder.objects.create(id=3, parent_id=2)
    await Folder.objects.create(id=4, parent_id=3)

    walked = await Folder.objects.filter(pk=1).with_recursive("folders", max_depth=2).values_list("id", flat=True)

    assert sorted(walked) == [1, 2, 3]


@pytest.mark.asyncio
async def test_a_walk_through_a_cycle_ends_at_its_depth_and_lists_each_row_once(clickhouse_db):
    await Folder.objects.create(id=1)
    await Folder.objects.create(id=2, parent_id=1)
    await Folder.objects.filter(id=1).update(parent_id=2)

    walked = await Folder.objects.filter(pk=1).with_recursive("folders", max_depth=5).values_list("id", flat=True)

    assert sorted(walked) == [1, 2]


@pytest.mark.asyncio
async def test_a_walk_without_a_depth_is_refused(clickhouse_db):
    await Folder.objects.create(id=1)

    with pytest.raises(UnSupportedError, match="needs max_depth"):
        await Folder.objects.filter(pk=1).with_recursive("folders").values_list("id", flat=True)
