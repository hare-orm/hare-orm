"""OutboxRelay's topics and exclude_topics - names and prefix patterns ending in "*": what a relay claims,
counts, cleans up, gives back from the dead letters and wakes up for."""

from datetime import timedelta

import pytest

from hare.contrib.outbox import OutboxRelay
from hare.contrib.outbox.relay.topic_selection import TopicSelection
from hare.exceptions import ConfigurationError
from hare.time import Timezone
from tests.contrib.outbox.models import DemoOutboxEvent

#: The topic of the task relay - what TaskiqDelivery.get_relay() takes by default.
TASK_TOPIC = "taskiq"


async def enqueue_topics(*topics: str) -> None:
    for topic in topics:
        await DemoOutboxEvent.enqueue(topic, {})


async def get_delivered_topics(relay_options: dict) -> list[str]:
    delivered: list[str] = []

    async def deliver(event: DemoOutboxEvent) -> None:
        delivered.append(event.topic)

    relay = OutboxRelay(DemoOutboxEvent, deliver, **relay_options)
    await relay.poll_once()
    return sorted(delivered)


@pytest.mark.asyncio
async def test_a_change_relay_and_a_task_relay_share_one_outbox(db_outbox):
    await enqueue_topics("shop.order.inserted", TASK_TOPIC, "shop.order.updated")

    assert await get_delivered_topics({"exclude_topics": [TASK_TOPIC]}) == [
        "shop.order.inserted",
        "shop.order.updated",
    ]
    assert await get_delivered_topics({"topics": [TASK_TOPIC]}) == [TASK_TOPIC]
    assert await DemoOutboxEvent.objects.filter(published_at__isnull=True).count() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relay_options", "expected_topics"),
    [
        ({"topics": ["shop.*"]}, ["shop.order.inserted", "shop.pay_ment.updated"]),
        ({"topics": ["shop.order.*", "billing.invoice"]}, ["billing.invoice", "shop.order.inserted"]),
        ({"topics": ["shop.*"], "exclude_topics": ["shop.order.*"]}, ["shop.pay_ment.updated"]),
        ({"exclude_topics": ["shop.*", "billing.invoice"]}, ["shop_x.y", "shopping.cart"]),
        (
            {"topics": ["*"]},
            sorted(["billing.invoice", "shop.order.inserted", "shop.pay_ment.updated", "shop_x.y", "shopping.cart"]),
        ),
        ({"topics": []}, []),
        # A pattern's "_" and "%" are the topic's characters, not LIKE's wildcards.
        ({"topics": ["shop.pay_*"]}, ["shop.pay_ment.updated"]),
        ({"topics": ["shop_*"]}, ["shop_x.y"]),
    ],
)
async def test_prefix_patterns_and_exclusions_select_the_events(db_outbox, relay_options, expected_topics):
    await enqueue_topics(
        "shop.order.inserted", "shop.pay_ment.updated", "shopping.cart", "shop_x.y", "billing.invoice"
    )

    assert await get_delivered_topics(relay_options) == expected_topics


@pytest.mark.asyncio
async def test_the_backlog_and_cleanup_of_a_relay_skip_its_excluded_topics(db_outbox):
    await enqueue_topics("shop.order.inserted", TASK_TOPIC)
    relay = OutboxRelay(DemoOutboxEvent, lambda event: None, exclude_topics=[TASK_TOPIC])

    assert (await relay.get_backlog()).pending == 1
    await DemoOutboxEvent.objects.all().update(published_at=Timezone.now() - timedelta(days=2))
    assert await relay.cleanup_published(timedelta(days=1)) == 1
    assert await DemoOutboxEvent.objects.values_list("topic", flat=True) == [TASK_TOPIC]


@pytest.mark.asyncio
async def test_retry_dead_lettered_takes_prefix_patterns(db_outbox):
    await enqueue_topics("shop.order.inserted", "shop.order.updated", "billing.invoice")
    await DemoOutboxEvent.objects.all().update(dead_lettered_at=Timezone.now(), attempts=5)
    relay = OutboxRelay(DemoOutboxEvent, lambda event: None)

    assert await relay.retry_dead_lettered(topics=["shop.order.*"]) == 2
    assert await DemoOutboxEvent.objects.filter(dead_lettered_at__isnull=False).values_list("topic", flat=True) == [
        "billing.invoice"
    ]


@pytest.mark.database_independent
@pytest.mark.parametrize(
    ("relay_options", "woken_by", "not_woken_by"),
    [
        ({}, [{"a"}, set()], []),
        ({"topics": ["shop.*"]}, [{"shop.order.inserted"}, {"x", "shop.y"}, set()], [{"shopping"}, {TASK_TOPIC}]),
        ({"exclude_topics": [TASK_TOPIC]}, [{"shop.order.inserted"}, {TASK_TOPIC, "a"}, set()], [{TASK_TOPIC}]),
        ({"topics": ["shop.*"], "exclude_topics": ["shop.secret.*"]}, [{"shop.order"}], [{"shop.secret.x"}]),
    ],
)
def test_a_wakeup_wakes_a_relay_naming_one_of_its_topics_or_none(relay_options, woken_by, not_woken_by):
    relay = OutboxRelay(DemoOutboxEvent, lambda event: None, **relay_options)
    for topics in woken_by:
        relay.woken.clear()
        relay.on_wakeup(frozenset(topics))
        assert relay.woken.is_set(), topics
    for topics in not_woken_by:
        relay.woken.clear()
        relay.on_wakeup(frozenset(topics))
        assert not relay.woken.is_set(), topics


@pytest.mark.database_independent
@pytest.mark.parametrize("pattern", ["shop.*.inserted", "*.inserted", "shop.?", "shop.**", "shop?*"])
@pytest.mark.parametrize("option_name", ["topics", "exclude_topics"])
def test_a_wildcard_inside_a_pattern_is_refused(option_name, pattern):
    with pytest.raises(ConfigurationError, match="wildcard inside"):
        OutboxRelay(DemoOutboxEvent, lambda event: None, **{option_name: [pattern]})


@pytest.mark.database_independent
def test_a_selection_of_every_topic_has_no_condition():
    assert TopicSelection().get_condition() is None
    assert TopicSelection().takes("anything")
