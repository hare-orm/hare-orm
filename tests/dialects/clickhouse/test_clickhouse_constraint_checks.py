"""The uniqueness and the relations a model declares, checked by hare before writes on ClickHouse, which
keeps neither: a key, a ``unique=True`` field, a ``UniqueConstraint`` with its condition, NULLs left
unique by themselves, a relation of ``db_constraint=True`` - by ``create()``, ``save()``,
``bulk_create()``, ``update()`` and ``bulk_update()``, the database left unwritten."""

import pytest
import pytest_asyncio

from hare.exceptions import IntegrityError
from tests.dialects.clickhouse.models import Member, Reading, Team


@pytest_asyncio.fixture
async def clubs(clickhouse_db):
    first = await Team.objects.create(name="first")
    second = await Team.objects.create(name="second")
    await Member.objects.bulk_create(
        [
            Member(id=1, email="ann@x", club=first, number=1),
            Member(id=2, email="bob@x", club=first, number=2),
            Member(id=3, email=None, club=second, number=1),
        ]
    )
    return {"first": first, "second": second}


async def get_member_ids():
    return await Member.objects.order_by("id").values_list("id", flat=True)


@pytest.mark.asyncio
async def test_a_new_row_breaking_a_uniqueness_is_refused(clubs):
    first = clubs["first"]
    for member, message in (
        (Member(id=1, email="new@x", club=first, number=9), "primary key"),
        (Member(id=9, email="ann@x", club=first, number=9), "email"),
        (Member(id=9, email="new@x", club=first, number=2), "member_number"),
    ):
        with pytest.raises(IntegrityError, match=message):
            await member.save()
    # NULL is unique by itself; the uniqueness of a condition holds for the rows it is true of.
    await Member.objects.create(id=4, email=None, club=first, number=3)
    await Member.objects.create(id=5, email="eve@x", club=first, number=1, active=False)
    assert await get_member_ids() == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_a_batch_breaking_a_uniqueness_writes_nothing(clubs):
    first = clubs["first"]
    with pytest.raises(IntegrityError, match="email"):
        await Member.objects.bulk_create(
            [
                Member(id=10, email="same@x", club=first, number=10),
                Member(id=11, email="same@x", club=first, number=11),
            ]
        )
    with pytest.raises(IntegrityError, match="primary key"):
        await Member.objects.bulk_create([Member(id=12, club=first, number=12), Member(id=2, club=first, number=13)])
    assert await get_member_ids() == [1, 2, 3]


@pytest.mark.asyncio
async def test_an_update_breaking_a_uniqueness_is_refused(clubs):
    member = await Member.objects.get(id=2)
    member.email = "ann@x"
    with pytest.raises(IntegrityError, match="email"):
        await member.save()
    # Its own value again is no break.
    member.email = "bob@x"
    await member.save()
    with pytest.raises(IntegrityError, match="email"):
        await Member.objects.filter(id=2).update(email="ann@x")
    with pytest.raises(IntegrityError, match="email"):
        # One value written into two rows.
        await Member.objects.filter(id__in=[1, 2]).update(email="both@x")
    await Member.objects.filter(id=2).update(email="robert@x")
    members = await Member.objects.filter(id__in=[1, 2]).order_by("id")
    members[0].number, members[1].number = 2, 1
    members[1].email = "ann@x"
    with pytest.raises(IntegrityError, match="email"):
        await Member.objects.bulk_update(members, ["number", "email"])
    rows = await Member.objects.order_by("id").values_list("id", "email", "number")
    assert rows == [(1, "ann@x", 1), (2, "robert@x", 2), (3, None, 1)]


@pytest.mark.asyncio
async def test_a_row_naming_a_missing_related_row_is_refused(clubs):
    missing = Team(name="missing")
    with pytest.raises(IntegrityError, match="no Team row"):
        await Member.objects.create(id=20, club_id=missing.id, number=20)
    with pytest.raises(IntegrityError, match="no Team row"):
        await Member.objects.filter(id=1).update(club_id=missing.id)
    member = await Member.objects.get(id=1)
    member.club_id = missing.id
    with pytest.raises(IntegrityError, match="no Team row"):
        await member.save()
    # A relation the database isn't told of is left unchecked.
    await Member.objects.create(id=21, club=clubs["second"], sponsor_id=missing.id, number=21)
    assert await get_member_ids() == [1, 2, 3, 21]


@pytest.mark.asyncio
async def test_a_table_keeping_versions_of_a_row_repeats_its_key(clickhouse_db):
    await Reading.objects.create(id=1, sensor="a", value=1)
    await Reading.objects.create(id=1, sensor="a", value=2, version=1)
    assert await Reading.objects.filter(id=1).final().values_list("value", flat=True) == [2]


@pytest.mark.asyncio
async def test_conflicting_rows_are_skipped_or_updated(clubs):
    first = clubs["first"]
    # Skipped: a key or another value held already, or held twice in the batch - the first one written.
    await Member.objects.bulk_create(
        [
            Member(id=1, email="other@x", club=first, number=20),
            Member(id=30, email="ann@x", club=first, number=30),
            Member(id=31, email="new@x", club=first, number=31),
            Member(id=32, email="new@x", club=first, number=32),
        ],
        ignore_conflicts=True,
    )
    assert await get_member_ids() == [1, 2, 3, 31]
    # Updated: an object of a stored email updates that row, the others are inserted.
    await Member.objects.bulk_create(
        [Member(id=99, email="ann@x", club=first, number=7), Member(id=40, email="eve@x", club=first, number=40)],
        update_fields=["number"],
        on_conflict=["email"],
    )
    rows = await Member.objects.filter(id__in=[1, 40, 99]).order_by("id").values_list("id", "number")
    assert rows == [(1, 7), (40, 40)]
    # Every object conflicting - nothing is inserted.
    await Member.objects.bulk_create([Member(id=2, club=first, number=50)], ignore_conflicts=True)
    assert await get_member_ids() == [1, 2, 3, 31, 40]
