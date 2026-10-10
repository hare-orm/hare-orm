# taskiq

`hare.contrib.taskiq` runs hare in [taskiq](https://taskiq-python.github.io/) workers: the worker's
Hare context, every task's statements tagged with the task, the task's tenant, a transaction per
task sent again after a conflict, and tasks sent only once the transaction asking for them commits.
Install the extra:

```bash
pip install "hare-orm[taskiq]"
```

## <a id="hare-taskiq"></a>Setting up the broker

```python
from taskiq_redis import ListQueueBroker

from hare.contrib.taskiq import HareTaskiq

broker = ListQueueBroker("redis://localhost:6379")
hare_taskiq = HareTaskiq(broker, HARE_ORM, atomic_tasks=True, transaction_retries=3)


@broker.task
async def charge_order(order_id: int) -> None:
    order = await Order.objects.get(id=order_id)
    ...
```

`HareTaskiq(broker, config, *, atomic_tasks=False, tenant_label="tenant", transaction_retries=0)`:

- opens the Hare context of `config` (as for `Hare.init(config=...)`) when a worker starts
  (`WORKER_STARTUP`) and closes its connections when it stops (`WORKER_SHUTDOWN`) — the context is
  the global fallback, seen by every task the worker runs;
- adds `HareTaskiqMiddleware` to the broker (`hare_taskiq.middleware`) — create the `HareTaskiq` in
  the module both the application and the worker import the broker from: the sending side needs the
  middleware too, to write the tenant into the tasks it sends.

The application sending tasks keeps its own Hare context (`Hare.init()`, a web framework's plugin);
`HareTaskiq` only opens the worker's.

## <a id="middleware"></a>What every task gets

`HareTaskiqMiddleware(*, atomic_tasks=False, tenant_label="tenant", transaction_retries=0)`:

- **Query tags** — every statement of the task carries `task_name` and `task_id` in its trailing
  comment ([query tags](../observability/query-tags.md)), next to the tags already active.
- **Tenant** — sending a task, the middleware writes the tenant scope active then
  (`Tenancy.scope(...)`) into the `tenant_label` label — a single tenant value: a string, an integer
  or a UUID (sent as its text); a label set by hand (`task.kicker().with_labels(tenant=...)`) is
  kept. The worker runs the task inside `Tenancy.scope(<label value>)`. `tenant_label=None` leaves
  tenants alone.
- **Transaction** — with `atomic_tasks=True` the task runs in a transaction on the default
  connection (connection names — one on each), committed when the task returns and rolled back
  when it raises. A failing commit fails the task. Callbacks of `Transactions.on_commit()` run after
  the commit — `TaskiqDelivery.kiq_on_commit()` from a task wakes the relay then.
- **Retry after a conflict** — a task whose transaction hits `TransactionRetryError` (a
  serialization failure, a deadlock — in the task or at the commit) is rolled back and sent again
  under its own id, up to `transaction_retries` times; the label `hare_transaction_retries` counts
  the retries. The failed attempt's result isn't saved, so `wait_result()` returns the attempt that
  succeeded — or the last failure. With taskiq's own `SimpleRetryMiddleware`, leave
  `TransactionRetryError` out of its exceptions, or a conflict is retried twice.

Everything the middleware set up is undone when the task ends — the tags, the tenant scope, the
running task. A synchronous task runs in a thread, where hare's asynchronous API isn't available.

The options are checked when it's created, raising `ConfigurationError`: `atomic_tasks` a bool or a
sequence of distinct connection names, `tenant_label` `None` or a non-empty string,
`transaction_retries` an `int` in `0..100` (`hare.contrib.taskiq.constants.MAX_TRANSACTION_RETRIES`).
A connection name the configuration lacks is refused when the worker starts.

## <a id="kiq-on-commit"></a>Sending tasks after commit

A task sent from inside a transaction may run before the transaction commits — and see none of its
writes — or run for a transaction that then rolls back. `TaskiqDelivery` sends it through the
[transactional outbox](outbox.md) instead — only once the transaction commits:

```python
from hare.contrib.outbox import OutboxEvent
from hare.contrib.taskiq import TaskiqDelivery


class TaskOutboxEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        table = "task_outbox"


tasks = TaskiqDelivery(broker, model=TaskOutboxEvent)

async with tasks.get_relay(poll_interval_seconds=1.0):
    async with Transactions.atomic():
        order = await Order.objects.create(total=100)
        await tasks.kiq_on_commit(charge_order, order.id)
    # charge_order is sent here, right after the commit.
```

`TaskiqDelivery(broker, *, model, topic="taskiq", using=None, wakeup=None)` is the outbox delivery
sending tasks, and the sender writing them. `await kiq_on_commit(task, *args, labels=None,
**kwargs)` takes the task or its name, its arguments — JSON values — and its labels, and writes the
task as an outbox event of `topic` (`OutboxEvent.enqueue()`) — in the current transaction, so a
rolled-back transaction leaves neither the event nor the task; outside a transaction it commits at
once. It returns the event.

The relay of `get_relay(**relay_options)` sends the tasks — the events of the delivery's `topic`
only, so the table can hold other topics too. The delivery's wakeup — an `InProcessWakeup` of its
own by default — starts the relay right after the commit, so run the relay in the process sending
the tasks; give a broker's wakeup (`RedisWakeup`, ...) to send from another one at once. A task the
broker didn't take (it was down) is retried by the relay like any outbox event.

Delivery is at least once: a task can arrive twice — make tasks safe to repeat. `using` names the
connection the tasks are written on — the model's by default. An empty `topic` raises
`ConfigurationError`.

## <a id="testing"></a>Testing

taskiq's `InMemoryBroker(await_inplace=True)` runs each task as it is sent, in the sending task —
with `HareTaskiqMiddleware` added to it the tests see the tags, tenants and transactions the workers
get:

```python
broker = InMemoryBroker(await_inplace=True)
broker.add_middlewares(HareTaskiqMiddleware(atomic_tasks=True))
```
