"""OutboxEvent.enqueue_many(): the events of one call written in one transaction, an INSERT per run of
events alike, in the order given; idempotency keys as enqueue() keeps them; one wakeup signal."""

import pytest

from hare.contrib.outbox import OutboxMessage, OutboxWakeup
from hare.contrib.test import capture_queries, requires_features
from hare.exceptions import ValidationError
from hare.transactions.transactions import Transactions
from tests.contrib.outbox.models import DemoOutboxEvent, TenantScopedOutboxEvent


class RecordingWakeup(OutboxWakeup):
    """Records the topics of every signal_written() call."""

    def __init__(self) -> None:
        super().__init__()
        self.written: list[set[str]] = []

    async def signal_written(self, connection, topics) -> None:
        self.written.append(set(topics))


def count_inserts(captured) -> int:
    return sum(1 for sql in captured.queries if sql.lstrip().upper().startswith("INSERT"))


@pytest.mark.asyncio
async def test_events_without_keys_are_one_insert_in_the_order_given(db_outbox):
    wakeup = RecordingWakeup()
    messages = [OutboxMessage(f"widget.{index}", {"n": index}, ordering_key="widget:1") for index in range(5)]

    async with capture_queries() as captured:
        events = await DemoOutboxEvent.enqueue_many(messages, wakeup=wakeup)

    assert count_inserts(captured) == 1
    assert [event.payload["n"] for event in events] == list(range(5))
    stored = await DemoOutboxEvent.objects.order_by("sequence").values_list("topic", flat=True)
    assert stored == [f"widget.{index}" for index in range(5)]
    assert wakeup.written == [{f"widget.{index}" for index in range(5)}]


@pytest.mark.asyncio
async def test_the_events_given_back_are_the_rows_written(db_outbox):
    events = await DemoOutboxEvent.enqueue_many(
        [OutboxMessage("widget.a", {}), OutboxMessage("widget.b", {}, idempotency_key="widget-b")]
        + [OutboxMessage("widget.c", {})]
    )

    stored = await DemoOutboxEvent.objects.order_by("sequence").values_list("sequence", "id")
    assert [(event.sequence, event.id) for event in events] == stored
    await events[0].refresh_from_db()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [{"topic": ""}, {"idempotency_key": ""}, {"ordering_key": ""}],
    ids=["topic", "idempotency", "ordering"],
)
async def test_an_empty_topic_or_key_is_refused(db_outbox, arguments):
    message = OutboxMessage(**{"topic": "widget.a", "payload": {}, **arguments})

    with pytest.raises(ValidationError, match="1 to 255 characters"):
        await DemoOutboxEvent.enqueue(
            message.topic, {}, idempotency_key=message.idempotency_key, ordering_key=message.ordering_key
        )
    with pytest.raises(ValidationError, match="1 to 255 characters"):
        await DemoOutboxEvent.enqueue_many([message])
    assert await DemoOutboxEvent.objects.count() == 0


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_a_stored_or_repeated_key_gives_the_event_stored_under_it(db_outbox):
    stored = await DemoOutboxEvent.enqueue("widget.created", {"n": 0}, idempotency_key="widget-0")
    wakeup = RecordingWakeup()

    events = await DemoOutboxEvent.enqueue_many(
        [
            OutboxMessage("widget.renamed", {"n": 1}, idempotency_key="widget-0"),
            OutboxMessage("widget.created", {"n": 2}, idempotency_key="widget-2"),
            OutboxMessage("widget.renamed", {"n": 3}, idempotency_key="widget-2"),
        ],
        wakeup=wakeup,
    )

    assert [event.id for event in events] == [stored.id, events[1].id, events[1].id]
    assert [event.payload for event in events] == [{"n": 0}, {"n": 2}, {"n": 2}]
    assert await DemoOutboxEvent.objects.count() == 2
    assert wakeup.written == [{"widget.created"}]


@requires_features(supports_unique_constraints=True)
@pytest.mark.asyncio
async def test_runs_with_and_without_keys_keep_the_order_given(db_outbox):
    events = await DemoOutboxEvent.enqueue_many(
        [
            OutboxMessage("a", {}, ordering_key="k"),
            OutboxMessage("b", {}, ordering_key="k", idempotency_key="b"),
            OutboxMessage("c", {}, ordering_key="k"),
        ]
    )

    assert [event.topic for event in events] == ["a", "b", "c"]
    assert await DemoOutboxEvent.objects.order_by("sequence").values_list("topic", flat=True) == ["a", "b", "c"]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_refused_message_writes_no_event(db_outbox):
    with pytest.raises(ValidationError, match="topic"):
        await DemoOutboxEvent.enqueue_many(
            [OutboxMessage("a", {}), OutboxMessage("b", {}, extra_field_values={"topic": "c"})]
        )
    with pytest.raises(ValidationError):
        async with Transactions.atomic():
            await DemoOutboxEvent.enqueue_many([OutboxMessage("a", {}), OutboxMessage("b", {"nan": float("nan")})])

    assert await DemoOutboxEvent.objects.count() == 0


@pytest.mark.asyncio
async def test_events_of_tenants_named_without_a_scope(db_outbox):
    events = await TenantScopedOutboxEvent.enqueue_many(
        [
            OutboxMessage("a", {}, extra_field_values={"tenant_id": 1}),
            OutboxMessage("b", {}, extra_field_values={"tenant_id": 2}),
        ]
    )

    assert [(event.topic, event.tenant_id) for event in events] == [("a", 1), ("b", 2)]


@pytest.mark.asyncio
async def test_no_messages_write_nothing(db_outbox):
    async with capture_queries() as captured:
        assert await DemoOutboxEvent.enqueue_many([]) == []
    assert captured.count == 0
