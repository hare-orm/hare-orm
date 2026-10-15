"""``bulk_update()`` and the upserts of ``bulk_create()`` on ClickHouse, which returns no rows of a
write: the rows written read by their keys, the objects of one conflict key written in order."""

import decimal

import pytest

from hare.exceptions import StaleObjectError
from hare.instrumentation import Observers, RowsChanged
from tests.dialects.clickhouse.models import Invoice, Member, Team, Ticket


@pytest.mark.asyncio
async def test_bulk_update_bumps_the_version_of_the_rows_it_writes(clickhouse_db):
    first, second = await Ticket.objects.bulk_create([Ticket(id=1, title="a"), Ticket(id=2, title="b")])
    stamped = (await Ticket.objects.get(id=1)).changed
    # Another write bumps the version of the second row.
    await Ticket.objects.filter(id=2).update(status="closed")
    first.title = "a2"
    second.title = "b2"
    with pytest.raises(StaleObjectError):
        await Ticket.objects.bulk_update([first, second], ["title"])
    assert (first.version, second.version) == (2, 1)
    assert first.get_dirty_fields() == {}
    assert set(second.get_dirty_fields()) == {"title"}
    rows = await Ticket.objects.order_by("id").values_list("id", "title", "version")
    assert rows == [(1, "a2", 2), (2, "b", 2)]
    assert (await Ticket.objects.get(id=1)).changed >= stamped


@pytest.mark.asyncio
async def test_bulk_update_reads_back_the_computed_columns(clickhouse_db):
    invoice = Invoice(id=1, price=decimal.Decimal("2.00"), quantity=1)
    await Invoice.objects.bulk_create([invoice])
    invoice.quantity = 3
    await Invoice.objects.bulk_update([invoice], ["quantity"], returning=True)
    assert (invoice.total, invoice.note) == (decimal.Decimal("6.00"), "many")


@pytest.mark.asyncio
async def test_an_upsert_updates_the_stamped_and_versioned_row(clickhouse_db):
    await Ticket.objects.bulk_create([Ticket(id=1, title="a")])
    objects = await Ticket.objects.bulk_create(
        [Ticket(id=1, title="a2"), Ticket(id=2, title="b")], update_fields=["title"], on_conflict=["id"]
    )
    assert [obj.pk for obj in objects] == [1, 2]
    rows = await Ticket.objects.order_by("id").values_list("id", "title", "version")
    assert rows == [(1, "a2", 2), (2, "b", 1)]


@pytest.mark.asyncio
async def test_an_upsert_bumps_the_version_without_comparing_it(clickhouse_db):
    await Ticket.objects.bulk_create([Ticket(id=1, title="a")])
    await Ticket.objects.filter(id=1).update(status="closed")
    kept, read_back = Ticket(id=1, title="a2"), Ticket(id=1, title="a3")
    await Ticket.objects.bulk_create([kept], update_fields=["title"], on_conflict=["id"])
    await Ticket.objects.bulk_create([read_back], update_fields=["title"], on_conflict=["id"], returning=True)
    assert (kept.version, read_back.version) == (1, 4)
    assert kept.get_dirty_fields() == read_back.get_dirty_fields() == {}
    assert await Ticket.objects.values_list("title", "version") == [("a3", 4)]


@pytest.mark.asyncio
async def test_the_last_object_of_a_conflict_key_wins(clickhouse_db):
    await Ticket.objects.bulk_create([Ticket(id=1, title="a")])
    objects = [
        Ticket(id=1, title="a2"),
        Ticket(id=2, title="b"),
        Ticket(id=1, title="a3"),
        Ticket(id=2, title="b2"),
        Ticket(id=1, title="a4"),
    ]
    assert await Ticket.objects.bulk_create(objects, update_fields=["title"], on_conflict=["id"]) == objects
    assert [(obj.pk, obj._saved_in_db) for obj in objects] == [(1, True), (2, True), (1, True), (2, True), (1, True)]
    assert await Ticket.objects.order_by("id").values_list("id", "title") == [(1, "a4"), (2, "b2")]


@pytest.mark.asyncio
async def test_the_last_object_of_a_unique_value_wins(clickhouse_db):
    club = await Team.objects.create(name="club")
    objects = [
        Member(id=1, email="ann@x", club=club, number=1),
        Member(id=2, email="ann@x", club=club, number=2),
    ]
    await Member.objects.bulk_create(objects, update_fields=["number"], on_conflict=["email"])
    assert await Member.objects.values_list("id", "number") == [(1, 2)]
    assert objects[1].pk == 1


@pytest.mark.asyncio
async def test_bulk_create_gives_the_objects_of_a_generator(clickhouse_db):
    objects = await Ticket.objects.bulk_create(Ticket(id=index, title=str(index)) for index in range(3))
    assert [(obj.pk, obj._saved_in_db) for obj in objects] == [(0, True), (1, True), (2, True)]


@pytest.mark.asyncio
async def test_a_repeated_key_is_reported_updated_after_its_insert(clickhouse_db):
    events = []
    with Observers.observing(RowsChanged, lambda event: events.append((event.operation.value, list(event.pks)))):
        await Ticket.objects.bulk_create(
            [Ticket(id=1, title="a"), Ticket(id=1, title="b")], update_fields=["title"], on_conflict=["id"]
        )
        await Observers.wait_for_pending()
    assert events == [("insert", [1]), ("update", [1])]
