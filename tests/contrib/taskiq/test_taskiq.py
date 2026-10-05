"""hare.contrib.taskiq on taskiq's InMemoryBroker: the worker's Hare context, query tags and the
tenant of a task, a transaction per task - committed, rolled back, sent again after a conflict -
and tasks sent through the outbox right after commit, again when the broker didn't take them."""

from __future__ import annotations

import asyncio
import contextvars
from typing import Any

import pytest
from taskiq import InMemoryBroker

from hare import Connections
from hare.contrib.taskiq import HareTaskiq, HareTaskiqMiddleware, TaskiqDelivery
from hare.contrib.taskiq.constants import MAX_TRANSACTION_RETRIES, TRANSACTION_RETRIES_LABEL
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError, TransactionRetryError
from hare.instrumentation.queries.query_tags import QueryTags
from hare.models.tenancy.tenancy import Tenancy
from hare.transactions.transactions import Transactions
from tests.contrib.taskiq.models import TaskiqOutboxEvent, TaskNote


def make_broker(**middleware_options: Any) -> InMemoryBroker:
    broker = InMemoryBroker(await_inplace=True)
    broker.add_middlewares(HareTaskiqMiddleware(**middleware_options))
    return broker


@pytest.mark.asyncio
async def test_a_task_runs_tagged_with_its_name_and_id_in_its_tenant(db_taskiq):
    broker = make_broker()
    seen: dict[str, Any] = {}

    @broker.task(task_name="remember_context")
    async def remember_context() -> None:
        seen["tags"] = dict(QueryTags.current.get() or {})
        seen["tenant"] = Tenancy.current.get()
        await Connections.get("models").execute_dicts("SELECT 1 AS one")

    with Tenancy.scope(7), QueryTags.scope(application="billing"):
        handle = await remember_context.kiq()
    result = await handle.wait_result(timeout=5)
    assert not result.is_err, result.error
    assert seen["tags"] == {"application": "billing", "task_name": "remember_context", "task_id": handle.task_id}
    assert seen["tenant"] == 7
    # Nothing leaks out of the task.
    assert QueryTags.current.get() is None
    assert Tenancy.current.get() is None


@pytest.mark.asyncio
async def test_a_label_set_by_hand_wins_and_none_leaves_tenants_alone(db_taskiq):
    broker = make_broker()
    seen: list[Any] = []

    @broker.task(task_name="tenant_of_task")
    async def tenant_of_task() -> None:
        seen.append(Tenancy.current.get())

    with Tenancy.scope(7):
        await (await tenant_of_task.kicker().with_labels(tenant="acme").kiq()).wait_result(timeout=5)
    await (await tenant_of_task.kiq()).wait_result(timeout=5)
    assert seen == ["acme", None]

    untouched_broker = make_broker(tenant_label=None)

    @untouched_broker.task(task_name="tenant_of_untouched_task")
    async def tenant_of_untouched_task() -> None:
        seen.append(Tenancy.current.get())

    with Tenancy.scope(7):
        await (await tenant_of_untouched_task.kiq()).wait_result(timeout=5)
    assert seen[-1] == 7  # the sender's own scope, run in place - the middleware set nothing


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_an_atomic_task_commits_when_it_returns_and_rolls_back_when_it_raises(db_taskiq):
    broker = make_broker(atomic_tasks=True)

    @broker.task(task_name="write_note")
    async def write_note(note_id: int, fail: bool) -> None:
        await TaskNote.objects.create(id=note_id, text="written")
        if fail:
            raise ValueError("the task failed")

    succeeded = await (await write_note.kiq(1, False)).wait_result(timeout=5)
    failed = await (await write_note.kiq(2, True)).wait_result(timeout=5)
    assert not succeeded.is_err
    assert failed.is_err
    assert [note.id for note in await TaskNote.objects.all().order_by("id")] == [1]


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transaction_retries", "conflicts", "succeeds"), [(2, 2, True), (2, 3, False), (0, 1, False)]
)
async def test_a_task_hitting_a_transaction_conflict_is_sent_again(
    db_taskiq, transaction_retries, conflicts, succeeds
):
    broker = make_broker(atomic_tasks=True, transaction_retries=transaction_retries)
    attempts: list[int] = []

    @broker.task(task_name="conflicting_task")
    async def conflicting_task() -> str:
        attempts.append(len(attempts))
        await TaskNote.objects.create(id=len(attempts), text="attempt")
        if len(attempts) <= conflicts:
            raise TransactionRetryError("could not serialize access")
        return "done"

    handle = await conflicting_task.kiq()
    result = await handle.wait_result(timeout=5)
    expected_runs = min(conflicts, transaction_retries) + 1 if succeeds else transaction_retries + 1
    assert len(attempts) == expected_runs
    if succeeds:
        assert not result.is_err
        assert result.return_value == "done"
        assert result.labels[TRANSACTION_RETRIES_LABEL] == conflicts
        # Only the run that returned committed its note.
        assert [note.id for note in await TaskNote.objects.all()] == [expected_runs]
    else:
        assert result.is_err
        assert isinstance(result.error, TransactionRetryError)
        assert await TaskNote.objects.all().count() == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"tenant_label": ""}, "tenant_label must be None or a non-empty label name"),
        ({"tenant_label": 5}, "tenant_label must be None or a non-empty label name"),
        ({"transaction_retries": -1}, "transaction_retries must be an int"),
        ({"transaction_retries": MAX_TRANSACTION_RETRIES + 1}, "transaction_retries must be an int"),
        ({"transaction_retries": True}, "transaction_retries must be an int"),
        ({"atomic_tasks": "models"}, "atomic_tasks must be a bool or a sequence"),
        ({"atomic_tasks": ["models", "models"]}, "atomic_tasks must name each connection once"),
    ],
)
def test_the_middleware_options_are_checked(options, message):
    with pytest.raises(ConfigurationError, match=message):
        HareTaskiqMiddleware(**options)


@pytest.mark.asyncio
async def test_hare_taskiq_opens_the_worker_context_and_closes_it(tmp_path):
    database_file = (tmp_path / "worker.sqlite3").as_posix()
    config = {
        "connections": {"default": f"sqlite+aiosqlite://{database_file}"},
        "apps": {"worker": {"models": ["tests.contrib.taskiq.worker_models"]}},
    }
    broker = InMemoryBroker(await_inplace=True)
    hare_taskiq = HareTaskiq(broker, config, atomic_tasks=True)
    assert hare_taskiq.middleware in broker.middlewares

    @broker.task(task_name="worker_query")
    async def worker_query() -> int:
        rows = await Connections.get("default").execute_dicts("SELECT 41 + 1 AS answer")
        return rows[0]["answer"]

    async def run_in_a_task_of_its_own() -> int:
        await broker.startup()
        try:
            assert hare_taskiq.lifecycle.context is not None
            result = await (await worker_query.kiq()).wait_result(timeout=5)
            assert not result.is_err, repr(result.error)
            return result.return_value
        finally:
            await broker.shutdown()

    # A worker process has no Hare context of its own - the one HareTaskiq opens is the fallback.
    assert await asyncio.create_task(run_in_a_task_of_its_own(), context=contextvars.Context()) == 42
    assert hare_taskiq.lifecycle.context is None

    wrong_broker = InMemoryBroker()
    wrong = HareTaskiq(wrong_broker, config, atomic_tasks=["missing"])
    with pytest.raises(ConfigurationError, match="atomic_tasks names the connection 'missing'"):
        await wrong.start(wrong_broker.state)
    assert wrong.lifecycle.context is None


async def wait_for(condition: Any) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition never held")


@requires_features(supports_transactions=True)
@pytest.mark.asyncio
async def test_a_task_is_sent_right_after_commit_and_never_after_rollback(db_taskiq):
    broker = InMemoryBroker(await_inplace=True)
    tasks = TaskiqDelivery(broker, model=TaskiqOutboxEvent)
    received: list[int] = []

    @broker.task(task_name="receive")
    async def receive(number: int) -> None:
        received.append(number)

    async with tasks.get_relay(poll_interval_seconds=60):
        async with Transactions.atomic():
            await tasks.kiq_on_commit(receive, 1)
            await asyncio.sleep(0.05)
            assert received == []
        # Woken by the commit, not the minute-long poll interval.
        await wait_for(lambda: received == [1])

        with pytest.raises(RuntimeError):
            async with Transactions.atomic():
                await tasks.kiq_on_commit("receive", 2)
                raise RuntimeError("rolled back")

        # Outside a transaction too, through the outbox.
        await tasks.kiq_on_commit(receive, 3, labels={"priority": 1})
        await wait_for(lambda: received == [1, 3])

    assert await TaskiqOutboxEvent.objects.filter(published_at=None).count() == 0
    assert await TaskiqOutboxEvent.objects.all().count() == 2


@pytest.mark.asyncio
async def test_a_task_the_broker_didnt_take_is_sent_again(db_taskiq):
    broker = InMemoryBroker(await_inplace=True)
    tasks = TaskiqDelivery(broker, model=TaskiqOutboxEvent, topic="tasks")
    received: list[Any] = []

    @broker.task(task_name="receive_later")
    async def receive_later(number: int, *, word: str) -> None:
        received.append((number, word))

    event = await tasks.kiq_on_commit(receive_later, 5, word="five")
    # An event of another topic of the same table isn't the relay's.
    await TaskiqOutboxEvent.enqueue("other", {"anything": True})
    relay = tasks.get_relay(retry_base_seconds=0)

    original_kick = broker.kick
    broker.kick = lambda message: (_ for _ in ()).throw(ConnectionError("broker down"))  # type: ignore[method-assign]
    await relay.poll_once()
    broker.kick = original_kick  # type: ignore[method-assign]
    await event.refresh_from_db()
    assert event.attempts == 1
    assert received == []

    assert await relay.poll_once() == 1
    assert received == [(5, "five")]
    assert await TaskiqOutboxEvent.objects.filter(topic="tasks", published_at=None).count() == 0
    assert await TaskiqOutboxEvent.objects.filter(topic="other", published_at=None).count() == 1


def test_the_delivery_options_are_checked():
    with pytest.raises(ConfigurationError, match="topic must be a non-empty string"):
        TaskiqDelivery(InMemoryBroker(), model=TaskiqOutboxEvent, topic="")
