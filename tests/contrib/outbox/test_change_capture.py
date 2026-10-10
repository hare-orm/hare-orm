"""Meta.change_capture: every ORM write of a captured model writes an outbox event per changed row in
its own transaction - saves, deletes, update(), bulk writes, cascades (the database's own included),
soft deletes, many-to-many through rows - with the payload its mode asks for."""

from decimal import Decimal

import pytest

from hare.contrib.outbox import ChangeCapture, ChangePayload
from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import ConfigurationError
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.observers.observers import Observers
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.capture_models import (
    AuditedOrder,
    CapturedArticle,
    CapturedChild,
    CapturedCustomer,
    CapturedOrder,
    CapturedOrderLine,
    CapturedOrderNote,
    CapturedTag,
    CaptureOutboxEvent,
    CompositeKeyRow,
    ExtendedRow,
    SoftOrder,
    TenantCaptureOutboxEvent,
    TenantRow,
    UncapturedParent,
    UncapturedRow,
)


async def get_events(topic: str | None = None) -> list[CaptureOutboxEvent]:
    queryset = CaptureOutboxEvent.objects.order_by("sequence")
    if topic is not None:
        queryset = queryset.filter(topic=topic)
    return await queryset


def strip_time(payload: dict) -> dict:
    return {key: value for key, value in payload.items() if key != "occurred_at"}


@pytest.mark.asyncio
async def test_create_writes_an_inserted_event(db_capture):
    customer = await CapturedCustomer.objects.create(id=1, name="Ann")
    await CapturedOrder.objects.create(id=7, total=Decimal("12.50"), secret="hidden", customer=customer)

    (event,) = await get_events("models.capturedorder.inserted")
    assert event.ordering_key == "models.CapturedOrder:7"
    assert event.headers == {}
    assert strip_time(event.payload) == {
        "model": "models.CapturedOrder",
        "operation": "inserted",
        "pk": 7,
        "changed": None,
        "before": None,
        "after": {"id": 7, "status": "new", "total": "12.50", "customer_id": 1},
    }
    assert event.payload["occurred_at"]
    assert [event.topic for event in await get_events()] == [
        "models.capturedcustomer.inserted",
        "models.capturedorder.inserted",
    ]


@pytest.mark.asyncio
async def test_save_writes_an_updated_event_with_the_fields_set(db_capture):
    order = await CapturedOrder.objects.create(id=1)
    order.status = "paid"
    await order.save(update_fields=["status"])

    (event,) = await get_events("models.capturedorder.updated")
    assert event.payload["changed"] == ["status"]
    assert event.payload["after"]["status"] == "paid"
    assert event.payload["before"] is None


@pytest.mark.asyncio
async def test_a_partial_instance_reads_the_fields_it_lacks(db_capture):
    await CapturedOrder.objects.create(id=1, total=Decimal("3.00"))
    order = await CapturedOrder.objects.only("id", "status").get(id=1)
    order.status = "paid"
    await order.save(update_fields=["status"])

    (event,) = await get_events("models.capturedorder.updated")
    assert event.payload["after"] == {"id": 1, "status": "paid", "total": "3.00", "customer_id": None}


@pytest.mark.asyncio
async def test_queryset_update_captures_each_row(db_capture):
    await CapturedOrder.objects.bulk_create(
        [CapturedOrder(id=1), CapturedOrder(id=2), CapturedOrder(id=3, status="x")]
    )
    observed: list[RowsChanged] = []
    with Observers.observing(RowsChanged, observed.append, models=[CapturedOrder]):
        updated = await CapturedOrder.objects.filter(status="new").update(status="sent")

    assert updated == 2
    events = await get_events("models.capturedorder.updated")
    assert sorted(event.payload["pk"] for event in events) == [1, 2]
    assert all(event.payload["changed"] == ["status"] for event in events)
    assert all(event.payload["after"]["status"] == "sent" for event in events)
    # The keys returned for the capture reach the observers too.
    assert sorted(observed[0].pks) == [1, 2]


@pytest.mark.asyncio
async def test_delete_captures_the_row_as_it_was(db_capture):
    order = await CapturedOrder.objects.create(id=1, status="done")
    await order.delete()

    (event,) = await get_events("models.capturedorder.deleted")
    assert event.payload["before"]["status"] == "done"
    assert event.payload["after"] is None


@pytest.mark.parametrize("through_queryset", [False, True])
@pytest.mark.asyncio
async def test_cascades_are_captured(db_capture, through_queryset):
    customer = await CapturedCustomer.objects.create(id=1, name="Ann")
    order = await CapturedOrder.objects.create(id=10, customer=customer)
    await CapturedOrderLine.objects.create(id=100, order=order)
    await CapturedOrderNote.objects.create(id=200, order=order, text="hi")
    await CaptureOutboxEvent.objects.all().delete()

    if through_queryset:
        await CapturedCustomer.objects.filter(id=1).delete()
    else:
        await customer.delete()

    events = await get_events()
    assert sorted(event.topic for event in events) == [
        "models.capturedcustomer.deleted",
        "models.capturedorder.deleted",
        "models.capturedorderline.deleted",
        "models.capturedordernote.updated",
    ]
    (line_event,) = [event for event in events if event.topic == "models.capturedorderline.deleted"]
    assert strip_time(line_event.payload) == {
        "model": "models.CapturedOrderLine",
        "operation": "deleted",
        "pk": 100,
        "changed": None,
    }
    (note_event,) = [event for event in events if event.topic == "models.capturedordernote.updated"]
    assert note_event.payload["after"] == {"id": 200, "text": "hi", "order_id": None}
    assert await CapturedOrderNote.objects.filter(id=200, order_id=None).count() == 1


@pytest.mark.asyncio
async def test_a_cascade_from_an_uncaptured_model_is_captured(db_capture):
    parent = await UncapturedParent.objects.create(id=1)
    await CapturedChild.objects.create(id=2, parent=parent)
    await CaptureOutboxEvent.objects.all().delete()

    await UncapturedParent.objects.filter(id=1).delete()

    (event,) = await get_events()
    assert event.topic == "models.capturedchild.deleted"
    assert event.payload["before"] == {"id": 2, "parent_id": 1}


@pytest.mark.asyncio
async def test_a_walk_reaching_captured_rows_locks_each_row_it_reads(db_capture):
    """The walk locks the root rows, then each row as it reads it - a row pointing at one can't
    be inserted before the delete ends, so the database's cascade removes no row without an event."""
    parent = await UncapturedParent.objects.create(id=1)
    await CapturedChild.objects.create(id=2, parent=parent)
    db = UncapturedParent.get_connection()
    async with capture_queries(db) as counter:
        await UncapturedParent.objects.filter(id=1).delete()
    reads = [sql.upper() for sql in counter.queries if sql.upper().startswith("SELECT")]
    parent_table = UncapturedParent._meta.db_table.upper()
    child_table = CapturedChild._meta.db_table.upper()
    parent_reads = [sql for sql in reads if parent_table in sql]
    child_reads = [sql for sql in reads if child_table in sql]
    locks = db.features.supports_select_for_update and db.features.supports_transactions
    assert child_reads
    assert all(("FOR UPDATE" in sql) == locks for sql in child_reads)
    assert any("FOR UPDATE" in sql for sql in parent_reads) == locks
    assert len(await get_events("models.capturedchild.deleted")) == 1


@pytest.mark.asyncio
async def test_bulk_create_and_bulk_update_are_captured(db_capture):
    orders = [CapturedOrder(id=1), CapturedOrder(id=2)]
    await CapturedOrder.objects.bulk_create(orders)
    assert sorted(event.payload["pk"] for event in await get_events("models.capturedorder.inserted")) == [1, 2]

    for order in orders:
        order.status = "packed"
    await CapturedOrder.objects.bulk_update(orders, fields=["status"])
    events = await get_events("models.capturedorder.updated")
    assert sorted(event.payload["pk"] for event in events) == [1, 2]
    assert all(event.payload["after"]["status"] == "packed" for event in events)


@pytest.mark.asyncio
async def test_before_and_after(db_capture):
    await AuditedOrder.objects.create(id=1, note="first")
    order = await AuditedOrder.objects.get(id=1)
    order.status = "paid"
    await order.save()
    await AuditedOrder.objects.filter(id=1).update(note="second")
    await (await AuditedOrder.objects.get(id=1)).delete()

    inserted, saved, updated, deleted = await get_events()
    assert inserted.payload["before"] is None
    assert inserted.payload["after"]["note"] == "first"
    assert saved.payload["changed"] == ["status"]
    assert saved.payload["before"]["status"] == "new"
    assert saved.payload["after"]["status"] == "paid"
    assert updated.payload["before"] == {"id": 1, "status": "paid", "note": "first"}
    assert updated.payload["after"] == {"id": 1, "status": "paid", "note": "second"}
    assert deleted.payload["before"]["note"] == "second"
    assert deleted.payload["after"] is None


@pytest.mark.asyncio
async def test_update_reads_the_rows_as_they_were_by_returning_old_or_a_read_before(db_capture):
    """``BEFORE_AND_AFTER`` of ``update()``: one ``UPDATE ... RETURNING`` reading ``OLD`` where the
    database has it; otherwise a read of the rows first - locked where it can lock them."""
    await AuditedOrder.objects.create(id=1, note="first")
    db = AuditedOrder.get_connection()
    table = AuditedOrder._meta.db_table
    async with capture_queries(db) as counter:
        await AuditedOrder.objects.filter(id=1).update(note="second")
    statements = [sql.upper() for sql in counter.queries if table.upper() in sql.upper()]
    updates = [sql for sql in statements if sql.startswith("UPDATE")]
    reads = [sql for sql in statements if sql.startswith("SELECT")]
    assert len(updates) == 1
    if db.features.supports_returning_old_new:
        assert reads == []
        assert "OLD" in updates[0].partition("RETURNING")[2]
    else:
        assert len(reads) == 1
        assert ("FOR UPDATE" in reads[0]) == db.features.supports_select_for_update
    event = (await get_events())[-1]
    assert event.payload["before"]["note"] == "first"
    assert event.payload["after"]["note"] == "second"


@pytest.mark.asyncio
async def test_update_writes_only_the_rows_it_read_as_they_were(db_capture, monkeypatch):
    """A row matching the update's filter that appears after the rows were read - committed by
    another transaction, say - is left alone: the update has no "before" of it."""
    if AuditedOrder.get_connection().features.supports_returning_old_new:
        pytest.skip("RETURNING OLD reads the rows in the update itself")
    await AuditedOrder.objects.create(id=1, note="first")
    read_values_of = ChangeCapturing.read_values_of
    inserted = []

    async def read_then_insert(model, db, keys, needs, *, lock=False):
        values = await read_values_of(model, db, keys, needs, lock=lock)
        if not inserted:
            # The insert's own capture reads rows too - one row appears, once.
            inserted.append(True)
            await AuditedOrder.objects.using(db).bulk_create([AuditedOrder(id=2, note="first")])
        return values

    monkeypatch.setattr(ChangeCapturing, "read_values_of", staticmethod(read_then_insert))
    assert await AuditedOrder.objects.filter(note="first").update(note="second") == 1
    assert {row.id: row.note for row in await AuditedOrder.objects.order_by("id")} == {1: "second", 2: "first"}
    updated = [event for event in await get_events() if event.payload["operation"] == "updated"]
    assert [event.payload["pk"] for event in updated] == [1]


@pytest.mark.asyncio
async def test_soft_delete_and_restore_are_updates(db_capture):
    order = await SoftOrder.objects.create(id=1)
    await order.delete()
    await order.restore()
    await SoftOrder.objects.filter(id=1).delete()

    topics_and_changes = [(event.topic, event.payload["changed"]) for event in await get_events()]
    assert topics_and_changes == [
        ("models.softorder.inserted", None),
        ("models.softorder.updated", ["deleted_at"]),
        ("models.softorder.updated", ["deleted_at"]),
        ("models.softorder.updated", ["deleted_at"]),
    ]
    events = await get_events()
    assert events[1].payload["after"]["deleted_at"] is not None
    assert events[2].payload["after"]["deleted_at"] is None


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_rolled_back_write_leaves_no_event(db_capture):
    with pytest.raises(RuntimeError):
        async with Transactions.atomic("models"):
            await CapturedOrder.objects.create(id=1)
            raise RuntimeError("rolled back")
    async with Transactions.atomic("models"):
        await CapturedOrder.objects.create(id=2)
        with pytest.raises(RuntimeError):
            async with Transactions.atomic("models"):
                await CapturedOrder.objects.filter(id=2).update(status="x")
                raise RuntimeError("savepoint rolled back")

    assert [event.topic for event in await get_events()] == ["models.capturedorder.inserted"]
    assert (await get_events())[0].payload["pk"] == 2


@pytest.mark.asyncio
async def test_a_composite_key_is_an_object_of_its_fields(db_capture):
    await CompositeKeyRow.objects.create(id=1, version=2, name="a")

    (event,) = await get_events()
    assert event.payload["pk"] == {"id": 1, "version": 2}
    assert event.ordering_key == "models.CompositeKeyRow:1:2"


@pytest.mark.asyncio
async def test_the_row_tenant_is_the_event_tenant(db_capture):
    with Tenancy.scope(5):
        await TenantRow.objects.create(id=1, name="a")
    await TenantRow.objects.all_tenants().filter(id=1).update(name="b")

    events = await TenantCaptureOutboxEvent.objects.all_tenants().order_by("sequence")
    assert [(event.topic, event.tenant_id) for event in events] == [
        ("models.tenantrow.inserted", 5),
        ("models.tenantrow.updated", 5),
    ]


@pytest.mark.asyncio
async def test_many_to_many_through_rows_are_captured(db_capture):
    article = await CapturedArticle.objects.create(id=1, title="t")
    tag = await CapturedTag.objects.create(id=2, name="n")
    await article.tags.add(tag)
    await article.tags.remove(tag)

    assert [event.topic for event in await get_events()] == [
        "models.capturedarticletag.inserted",
        "models.capturedarticletag.deleted",
    ]


@pytest.mark.asyncio
async def test_extend_topic_ordering_key_and_fields(db_capture):
    row = await ExtendedRow.objects.create(id=1, name="a", hidden="x")
    await row.delete()

    (event,) = await get_events()
    assert event.topic == "rows.insert"
    assert event.ordering_key is None
    assert event.headers == {"author": "alice"}
    assert event.payload["source"] == "test"
    assert event.payload["after"] == {"id": 1, "name": "a"}


@pytest.mark.asyncio
async def test_an_uncaptured_model_writes_no_event_and_returns_nothing(db_capture):
    row = await UncapturedRow.objects.create(id=1, name="a")
    row.name = "b"
    await row.save()
    await UncapturedRow.objects.filter(id=1).update(name="c")
    await row.delete()

    assert await get_events() == []
    assert "RETURNING" not in UncapturedRow.objects.filter(id=1).update(name="d").sql()
    assert "RETURNING" in CapturedOrder.objects.filter(id=1).update(status="d").sql()


@pytest.mark.asyncio
async def test_insert_from_is_captured(db_capture):
    await UncapturedRow.objects.create(id=4, name="from row")

    await CapturedCustomer.objects.insert_from(UncapturedRow.objects.values("id", "name"), fields=["id", "name"])

    (event,) = await get_events()
    assert event.topic == "models.capturedcustomer.inserted"
    assert event.payload["after"] == {"id": 4, "name": "from row"}


@requires_features(supports_merge_returning=True)
@pytest.mark.asyncio
async def test_merge_is_captured_by_action(db_capture):
    await CapturedCustomer.objects.create(id=1, name="old")
    await CaptureOutboxEvent.objects.all().delete()

    await (
        CapturedCustomer.objects.merge([{"id": 1, "name": "new"}, {"id": 2, "name": "added"}], on="id")
        .when_matched(update={"name": "renamed"})
        .when_not_matched(insert={"id": 2, "name": "added"})
    )

    assert sorted(event.topic for event in await get_events()) == [
        "models.capturedcustomer.inserted",
        "models.capturedcustomer.updated",
    ]


def test_the_declaration_is_checked():
    with pytest.raises(ConfigurationError, match="OutboxEvent"):
        ChangeCapture(UncapturedRow)
    with pytest.raises(ConfigurationError):
        ChangeCapture(CaptureOutboxEvent, payload="everything")
    with pytest.raises(ConfigurationError, match="fields or exclude"):
        ChangeCapture(CaptureOutboxEvent, fields=["name"], exclude=["id"])
    with pytest.raises(ConfigurationError, match="nope"):
        ChangeCapture(CaptureOutboxEvent, fields=["nope"]).get_field_names(UncapturedRow)
    with pytest.raises(ConfigurationError, match="track_dirty_fields"):
        ChangeCapture(CaptureOutboxEvent, payload=ChangePayload.BEFORE_AND_AFTER).get_field_names(UncapturedRow)
    with pytest.raises(ConfigurationError, match="template"):
        ChangeCapture(CaptureOutboxEvent, topic="{nope}").get_field_names(UncapturedRow)
    with pytest.raises(ConfigurationError, match="capture changes itself"):
        ChangeCapture(CaptureOutboxEvent).get_field_names(CaptureOutboxEvent)


def test_sensitive_and_encrypted_fields_are_left_out_unless_named():
    from tests.fields.models_encrypted import EncryptedRecord

    default_names = ChangeCapture(CaptureOutboxEvent).get_field_names(EncryptedRecord)
    assert "title" in default_names
    assert not {"secret", "config", "api_token", "public_note"} & set(default_names)
    named = ChangeCapture(CaptureOutboxEvent, fields=["title", "api_token"]).get_field_names(EncryptedRecord)
    assert named == ("title", "api_token")
    assert "sensitive" not in str(CapturedOrder._meta.change_capture_needs.field_names)
    assert "secret" not in CapturedOrder._meta.change_capture_needs.field_names
