"""The uniqueness and the relations a model declares, checked by hare before writes on ClickHouse, which
keeps neither: a key, a ``unique=True`` field, a ``UniqueConstraint`` with its condition, NULLs left
unique by themselves, a relation of ``db_constraint=True`` - by ``create()``, ``save()``,
``bulk_create()``, ``update()`` and ``bulk_update()``, the database left unwritten."""

import asyncio

import pytest
import pytest_asyncio

from hare.contrib.test import capture_queries
from hare.exceptions import IntegrityError, QueryError
from hare.query.expressions import F, Value
from hare.query.functions import Concat
from tests.dialects.clickhouse.models import Desk, Folder, Member, Office, Reading, Revision, RevisionComment, Team


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
async def test_a_saved_row_created_again_is_refused(clubs):
    member = await Member.objects.get(id=1)
    with pytest.raises(IntegrityError, match="primary key"):
        await member.save(force_create=True)
    with pytest.raises(IntegrityError, match="primary key"):
        await Member.objects.bulk_create([member])
    assert await get_member_ids() == [1, 2, 3]


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
async def test_an_update_by_an_expression_or_of_a_condition_field_breaking_a_uniqueness_is_refused(clubs):
    with pytest.raises(IntegrityError, match="member_number"):
        # Number 1 + 1 is member 2's number in the same club.
        await Member.objects.filter(id=1).update(number=F("number") + 1)
    with pytest.raises(IntegrityError, match="email"):
        await Member.objects.filter(id=2).update(email=Concat(Value("ann"), Value("@x")))
    await Member.objects.create(id=4, club=clubs["first"], number=1, active=False)
    inactive = await Member.objects.get(id=4)
    inactive.active = True
    with pytest.raises(IntegrityError, match="member_number"):
        # The condition of the uniqueness becomes true of a row repeating an active one's number.
        await Member.objects.bulk_update([inactive], ["active"])
    with pytest.raises(IntegrityError, match="member_number"):
        await Member.objects.filter(id=4).update(active=True)
    await Member.objects.filter(id=4).update(number=F("number") + 2)
    rows = await Member.objects.order_by("id").values_list("id", "email", "number", "active")
    assert rows == [(1, "ann@x", 1, True), (2, "bob@x", 2, True), (3, None, 1, True), (4, None, 3, False)]


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
    # An object skipped for its email takes no key: a later one of its key is inserted.
    await Member.objects.bulk_create(
        [Member(id=41, email="ann@x", club=first, number=41), Member(id=41, email="zed@x", club=first, number=42)],
        ignore_conflicts=True,
    )
    assert await Member.objects.filter(id=41).values_list("email", flat=True) == ["zed@x"]
    # A conflict target is a set of fields declared unique for every row.
    with pytest.raises(QueryError, match="declares unique"):
        await Member.objects.bulk_create(
            [Member(id=50, club=first, number=1)], update_fields=["email"], on_conflict=["number"]
        )


@pytest.mark.asyncio
async def test_concurrent_writes_of_one_value_in_one_process_pass_one_by_one(clubs):
    first = clubs["first"]

    async def write(call):
        try:
            await call
        except IntegrityError:
            return "refused"
        return "written"

    # create(), bulk_create() with ignore_conflicts and save() of one key, one email: the process holds
    # each value from its check until its row is written.
    outcomes = await asyncio.gather(
        *(write(Member.objects.create(id=60, club=first, number=60 + position)) for position in range(5))
    )
    assert sorted(outcomes) == ["refused"] * 4 + ["written"]
    outcomes = await asyncio.gather(
        *(
            write(Member.objects.create(id=70 + position, email="same@x", club=first, number=70 + position))
            for position in range(5)
        )
    )
    assert sorted(outcomes) == ["refused"] * 4 + ["written"]
    await asyncio.gather(
        *(
            Member.objects.bulk_create([Member(id=80, club=first, number=80 + position)], ignore_conflicts=True)
            for position in range(5)
        )
    )
    assert await Member.objects.filter(id=80).count() == 1
    members = [await Member.objects.get(id=id_) for id_ in (1, 2)]
    for member in members:
        member.email = "taken@x"
    outcomes = await asyncio.gather(*(write(member.save()) for member in members))
    assert sorted(outcomes) == ["refused", "written"]
    assert await Member.objects.filter(email="taken@x").count() == 1


@pytest.mark.asyncio
async def test_rows_naming_rows_of_their_own_batch_are_written(clickhouse_db):
    # As one INSERT of the rows: a row written beside the one it names is there.
    await Folder.objects.bulk_create([Folder(id=1), Folder(id=2, parent_id=1), Folder(id=3, parent_id=2)])
    with pytest.raises(IntegrityError, match="no Folder row of 99"):
        await Folder.objects.bulk_create([Folder(id=4, parent_id=99)])
    assert await Folder.objects.order_by("id").values_list("id", "parent_id") == [(1, None), (2, 1), (3, 2)]


@pytest.mark.asyncio
async def test_a_relation_to_a_key_of_two_columns_is_checked_in_one_query(clickhouse_db):
    revisions = [Revision(document_id=1, number=number, title="t") for number in range(1, 6)]
    await Revision.objects.bulk_create(revisions)
    comments = [RevisionComment(id=revision.number, revision=revision, text="c") for revision in revisions]
    async with capture_queries() as counter:
        await RevisionComment.objects.bulk_create(comments)
    # The keys, the revisions named, the insert.
    assert len(counter.queries) == 3
    with pytest.raises(IntegrityError, match="no Revision row"):
        await RevisionComment.objects.create(id=9, revision_document_id=1, revision_number=77, text="c")


@pytest.mark.asyncio
async def test_a_tree_of_rows_of_one_model_is_deleted_down_its_branches(clickhouse_db):
    await Folder.objects.bulk_create(
        [Folder(id=1), Folder(id=2, parent_id=1), Folder(id=3, parent_id=2), Folder(id=4)]
    )
    root = await Folder.objects.get(id=1)
    assert (await root.delete_preview()).deleted == {Folder: 3}
    await root.delete()
    assert await Folder.objects.values_list("id", flat=True) == [4]


@pytest.mark.asyncio
async def test_a_key_other_rows_name_is_kept(clickhouse_db):
    """The code of an office was changed with desks still naming the old one - they named no office."""
    named = await Office.objects.create(id=1, code="beta")
    free = await Office.objects.create(id=2, code="gamma")
    await Desk.objects.create(id=1, office=named)
    named.code = "beta2"
    with pytest.raises(IntegrityError, match="Desk.office"):
        await named.save()
    with pytest.raises(IntegrityError, match="Desk.office"):
        await Office.objects.filter(id=1).update(code="beta3")
    with pytest.raises(IntegrityError, match="Desk.office"):
        await Office.objects.filter(id=1).update(code=Concat(F("code"), Value("!")))
    with pytest.raises(IntegrityError, match="Desk.office"):
        await Office.objects.bulk_update([named], ["code"])
    assert await Office.objects.order_by("id").values_list("code", flat=True) == ["beta", "gamma"]
    free.code = "gamma2"
    await free.save()
    await Office.objects.filter(id=2).update(code="gamma3")
    assert await Office.objects.order_by("id").values_list("code", flat=True) == ["beta", "gamma3"]
    await Desk.objects.filter(id=1).delete()
    await Office.objects.filter(id=1).update(code="beta2")
    assert await Office.objects.get(id=1) is not None
