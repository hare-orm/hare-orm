"""Queries against partitioned tables: writes land in their partitions, conflicts, updates and
deletes by the composite key, row locks and foreign keys to and from a partitioned table."""

import datetime
import os

import pytest
import pytest_asyncio

from hare.contrib.test.isolated_contexts import hare_test_context
from hare.exceptions import IntegrityError
from hare.transactions.transactions import Transactions
from tests.dialects.postgresql.conftest import skip_if_not_postgres
from tests.dialects.postgresql.models_partitioned import (
    DailyEvent,
    Reader,
    ReaderVisit,
    RegionSale,
    ReportNote,
    UnreadReport,
)

PARTITION_ROW_COUNTS_SQL = (
    "SELECT child.relname AS name, "
    "(xpath('/row/count/text()', query_to_xml('SELECT COUNT(*) FROM ' || quote_ident(child.relname), "
    "false, true, '')))[1]::text::int AS row_count "
    "FROM pg_inherits inh JOIN pg_class child ON child.oid = inh.inhrelid "
    "WHERE inh.inhparent = $1::regclass ORDER BY child.relname"
)


@pytest_asyncio.fixture
async def db_partitioned():
    skip_if_not_postgres()
    async with hare_test_context(
        modules=["tests.dialects.postgresql.models_partitioned"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


async def get_partition_row_counts(context, table: str) -> dict[str, int]:
    rows = await context.get_connection().execute_dicts(PARTITION_ROW_COUNTS_SQL, [table])
    return {row["name"]: row["row_count"] for row in rows}


@pytest.mark.asyncio
async def test_rows_land_in_the_partition_of_their_key(db_partitioned):
    await UnreadReport.objects.bulk_create([UnreadReport(user_id=user, report_id=1) for user in range(40)])
    await RegionSale.objects.bulk_create([RegionSale(region=region, number=1) for region in ("us", "ca", "jp", "de")])
    await DailyEvent.objects.create(day=datetime.date(2020, 5, 1), number=1)
    await DailyEvent.objects.create(day=datetime.date(2026, 5, 1), number=1)

    unread_counts = await get_partition_row_counts(db_partitioned, "unreadreport")
    assert sorted(unread_counts) == [f"unreadreport_p{remainder}" for remainder in range(4)]
    assert sum(unread_counts.values()) == 40 and all(unread_counts.values())
    assert await get_partition_row_counts(db_partitioned, "regionsale") == {
        "regionsale_east": 1,
        "regionsale_other": 1,
        "regionsale_west": 2,
    }
    assert await get_partition_row_counts(db_partitioned, "dailyevent") == {
        "dailyevent_before_2026": 1,
        "dailyevent_y2026": 1,
    }
    assert await UnreadReport.objects.count() == 40
    assert await RegionSale.objects.filter(region__in=["us", "de"]).order_by("region").values_list(
        "region", flat=True
    ) == ["de", "us"]


@pytest.mark.asyncio
async def test_a_row_no_partition_holds_is_refused(db_partitioned):
    with pytest.raises(IntegrityError):
        await DailyEvent.objects.create(day=datetime.date(2030, 1, 1), number=1)


@pytest.mark.asyncio
async def test_storage_parameters_are_set_on_every_partition(db_partitioned):
    rows = await db_partitioned.get_connection().execute_dicts(
        "SELECT child.relname AS name, child.reloptions AS options FROM pg_inherits inh "
        "JOIN pg_class child ON child.oid = inh.inhrelid WHERE inh.inhparent = 'unreadreport'::regclass"
    )
    (parent,) = await db_partitioned.get_connection().execute_dicts(
        "SELECT reloptions AS options FROM pg_class WHERE oid = 'unreadreport'::regclass"
    )

    assert len(rows) == 4
    assert all(row["options"] == ["autovacuum_vacuum_scale_factor=0.02"] for row in rows)
    assert parent["options"] is None


@pytest.mark.asyncio
async def test_bulk_create_resolves_conflicts_on_the_composite_key(db_partitioned):
    await UnreadReport.objects.bulk_create(
        [UnreadReport(user_id=1, report_id=1, title="first"), UnreadReport(user_id=2, report_id=1, title="first")]
    )

    upserted = [UnreadReport(user_id=1, report_id=1, title="again"), UnreadReport(user_id=3, report_id=1, title="new")]
    await UnreadReport.objects.bulk_create(
        upserted, on_conflict=["user_id", "report_id"], update_fields=["title"], returning=True
    )

    assert sorted((report.user_id, report.title) for report in upserted) == [(1, "again"), (3, "new")]
    assert await UnreadReport.objects.order_by("user_id").values_list("user_id", "title") == [
        (1, "again"),
        (2, "first"),
        (3, "new"),
    ]

    await UnreadReport.objects.bulk_create(
        [UnreadReport(user_id=2, report_id=1, title="ignored"), UnreadReport(user_id=4, report_id=1, title="kept")],
        ignore_conflicts=True,
    )

    assert await UnreadReport.objects.order_by("user_id").values_list("user_id", "title") == [
        (1, "again"),
        (2, "first"),
        (3, "new"),
        (4, "kept"),
    ]


@pytest.mark.asyncio
async def test_update_and_delete_by_the_composite_key(db_partitioned):
    await UnreadReport.objects.bulk_create([UnreadReport(user_id=user, report_id=7) for user in range(6)])
    report = await UnreadReport.objects.get(user_id=3, report_id=7)

    report.title = "saved"
    await report.save(update_fields=["title"])
    assert await UnreadReport.objects.filter(user_id=2, report_id=7).update(seen_count=5) == 1
    assert await UnreadReport.objects.filter(pk=(4, 7)).delete() == 1
    await (await UnreadReport.objects.get(user_id=5, report_id=7)).delete()

    assert await UnreadReport.objects.order_by("user_id").values_list("user_id", "title", "seen_count") == [
        (0, "", 0),
        (1, "", 0),
        (2, "", 5),
        (3, "saved", 0),
    ]
    assert await UnreadReport.objects.get(does_not_exist_exception=None, pk=(4, 7)) is None


@pytest.mark.asyncio
async def test_rows_are_locked_skipping_the_locked_ones(db_partitioned):
    await UnreadReport.objects.bulk_create([UnreadReport(user_id=user, report_id=1) for user in range(4)])

    async with Transactions.atomic():
        locked = await UnreadReport.objects.filter(user_id__lt=2).order_by("user_id").select_for_update()
        async with Transactions.autonomous() as other_connection, other_connection._in_transaction() as other:
            free = await UnreadReport.objects.using(other).order_by("user_id").select_for_update(skip_locked=True)

    assert [report.user_id for report in locked] == [0, 1]
    assert [report.user_id for report in free] == [2, 3]


@pytest.mark.asyncio
async def test_foreign_keys_to_and_from_a_partitioned_table(db_partitioned):
    reader = await Reader.objects.create(name="ann")
    report = await UnreadReport.objects.create(user_id=1, report_id=1, reader=reader)
    note = await ReportNote.objects.create(report=report, text="look")

    fetched = await ReportNote.objects.select_related("report__reader").get(id=note.id)
    assert (fetched.report.pk, fetched.report.reader.name) == ((1, 1), "ann")
    assert [unread.pk for unread in await reader.unread_reports.all()] == [(1, 1)]
    assert await UnreadReport.objects.filter(notes__text="look").count() == 1
    prefetched = await UnreadReport.objects.prefetch_related("notes").get(user_id=1, report_id=1)
    assert [prefetched_note.text for prefetched_note in prefetched.notes] == ["look"]

    with pytest.raises(IntegrityError):
        await db_partitioned.get_connection().execute_script(
            "INSERT INTO reportnote (report_user_id, report_report_id) VALUES (9, 9)"
        )
    with pytest.raises(IntegrityError):
        await db_partitioned.get_connection().execute_script("UPDATE unreadreport SET reader_id = 999")

    # The cascade of the foreign key reaches the partitioned table's rows and those referencing them.
    await reader.delete()
    assert await UnreadReport.objects.count() == 0
    assert await ReportNote.objects.count() == 0


@pytest.mark.asyncio
async def test_a_table_without_a_primary_key_is_partitioned_by_a_foreign_key(db_partitioned):
    first = await Reader.objects.create(name="ann")
    second = await Reader.objects.create(name="bob")
    await ReaderVisit.objects.bulk_create(
        [ReaderVisit(reader=reader, page=f"page{number}") for reader in (first, second) for number in range(3)]
    )

    counts = await get_partition_row_counts(db_partitioned, "readervisit")

    assert sorted(counts) == ["readervisit_p0", "readervisit_p1"]
    assert sum(counts.values()) == 6
    assert await ReaderVisit.objects.filter(reader=first).count() == 3
    assert await first.visits.all().count() == 3
