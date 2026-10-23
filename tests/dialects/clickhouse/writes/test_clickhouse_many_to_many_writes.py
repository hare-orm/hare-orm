"""A change of a many-to-many relation on ClickHouse, which has no transaction to undo half of it: what
can fail is checked, or written, before anything is removed."""

import pytest

from hare.exceptions import IntegrityError, QueryError
from tests.dialects.clickhouse.models import Player, Skill


async def get_linked_skill_ids(player: Player) -> list[int]:
    return sorted(await player.skills.all().values_list("id", flat=True))


@pytest.mark.asyncio
async def test_a_set_with_a_member_of_no_row_leaves_the_links_as_they_were(clickhouse_db):
    """set() removed the links that stayed out before the member of no row failed to be added."""
    player = await Player.objects.create(id=1, name="p")
    first = await Skill.objects.create(id=1, name="first")
    second = await Skill.objects.create(id=2, name="second")
    await player.skills.add(second)
    with pytest.raises(IntegrityError):
        await player.skills.set([first, -1])
    assert await get_linked_skill_ids(player) == [2]
    with pytest.raises(IntegrityError):
        await player.skills.set([-1], clear=True)
    assert await get_linked_skill_ids(player) == [2]
    await player.skills.set([first], clear=True)
    assert await get_linked_skill_ids(player) == [1]


@pytest.mark.asyncio
async def test_a_create_with_through_defaults_the_relation_cant_take_writes_nothing(clickhouse_db):
    """The object was written before through_defaults failed - its row stayed."""
    player = await Player.objects.create(id=1, name="p")
    with pytest.raises(QueryError, match="through_defaults isn't supported"):
        await player.skills.create(id=10, name="x", through_defaults={"level": 1})
    assert not await Skill.objects.filter(id=10).exists()


@pytest.mark.asyncio
async def test_a_hundred_thousand_links_are_added_and_set_at_once(clickhouse_db):
    """The links were written as INSERTs built one row a call - each call copied the rows before it, so
    200 000 links took minutes; they are loaded in one binary insert."""
    player = await Player.objects.create(id=1, name="p")
    await Skill.objects.bulk_create([Skill(id=number, name="s") for number in range(1, 100_001)])
    await player.skills.add(*range(1, 100_001))
    assert await player.skills.all().count() == 100_000
    await player.skills.set(list(range(50_001, 100_001)))
    assert await player.skills.all().count() == 50_000
    assert await player.skills.filter(id__lte=50_000).count() == 0
