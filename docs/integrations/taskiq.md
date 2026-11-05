# taskiq

`hare.contrib.taskiq` runs hare in [taskiq](https://taskiq-python.github.io/) workers: the worker's
`HareContext`, every task's statements tagged with the task, the task's tenant, a transaction per task
sent again after a conflict, and tasks sent only once the transaction asking for them commits. Install
the extra:

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
async def send_due_date_reminder(loan_id: int) -> None:
    loan = await Loan.objects.get(id=loan_id)
    ...
```

```python
class HareTaskiq:
    def __init__(
        self,
        broker: AsyncBroker,
        config: Mapping[str, Any] | HareConfig | str,
        *,
        hare_context: HareContext | None = None,
        atomic_tasks: bool | Iterable[str] = False,
        tenant_label: str | None = "tenant",
        transaction_retries: int = 0,
    ) -> None: ...
```

`HareTaskiq` of a broker:

- opens the `HareContext` of `config` (as for `Hare.init(config=...)`) when a worker starts
  (`WORKER_STARTUP`) and closes its connections when it stops (`WORKER_SHUTDOWN`) — the context is the
  explicit context activated by middleware for each task the worker runs;
- adds `HareTaskiqMiddleware` to the broker (`hare_taskiq.middleware`) — create the `HareTaskiq` in the
  module both the application and the worker import the broker from: the sending side needs the
  middleware too, to write the tenant into the tasks it sends.

The application sending tasks keeps its own `HareContext` (`Hare.init()`, a web framework's plugin);
`HareTaskiq` only opens the worker's.

## <a id="middleware"></a>What every task gets

`HareTaskiqMiddleware(*, atomic_tasks=False, tenant_label="tenant", transaction_retries=0)`:

- **Query tags** — every statement of the task carries `task_name` and `task_id` in its trailing comment
  ([query tags](../observability/query-tags.md)), next to the tags already active.
- **Tenant** — sending a task, the middleware writes the tenant scope active then (`Tenancy.scope(...)`)
  into the `tenant_label` label: a tenant value — a string, an integer or a UUID (sent as its text) — as
  it is, `Tenancy.any_of(...)` as `{"any_of": [...]}`, `Tenancy.ALL` as `{"all": true}`, a scope given
  model by model as `{"models": {"app.Model": ...}}`. A tenant value of another type, or a scope given
  for an abstract model, raises `UnSupportedError` when the task is sent — the task never runs without
  the scope of its sender. A label set by hand (`task.kicker().with_labels(tenant=...)`) is kept. The
  worker runs the task inside the scope the label names. `tenant_label=None` leaves tenants alone.
- **Transaction** — `atomic_tasks=True` selects one connection automatically: the only configured
  connection, then the common connection of all registered models, then the alias `default`.
  An ambiguous configuration raises `QueryError`; an explicit list of aliases opens one transaction
  per named connection. It commits when the task returns and rolls back when it raises. A
  failing commit fails the task. Callbacks of `Transactions.on_commit()` run after the commit —
  `TaskiqDelivery.kiq_on_commit()` from a task wakes the relay then. If opening a later connection
  fails, already opened transactions roll back. Completion drains every acquired transaction,
  including when cancelled, before restoring task scopes. Connections commit in reverse acquisition
  order; a later failure cannot undo an earlier successful commit. Multiple completion failures
  are reported together with the original task error.
- **Retry after a conflict** — a task whose transaction hits `TransactionRetryError` (a serialization
  failure, a deadlock — in the task or at the commit) is rolled back and sent again under its own id, up
  to `transaction_retries` times; the label `hare_transaction_retries` counts the retries. The failed
  attempt's result isn't saved, so `wait_result()` returns the attempt that succeeded — or the last
  failure. With taskiq's own `SimpleRetryMiddleware`, leave `TransactionRetryError` out of its
  exceptions, or a conflict is retried twice.

Hare's `post_execute` hook completes transactions and restores the tags, tenant and running-task
scope. This includes a task error reported through taskiq's `on_error` hook. The hooks depend on
taskiq's receiver reaching them: an exception in another middleware, dependency cleanup or an
earlier broker acknowledgement can interrupt that sequence. Hare cannot finish a skipped hook.
Keep taskiq's default `when_saved` acknowledgement mode for atomic tasks: `when_received` and
`when_executed` acknowledge before Hare commits, so a later transaction failure cannot undo that
acknowledgement. Database commit and broker acknowledgement are separate operations.
A synchronous task runs in a thread, where hare's asynchronous API isn't available.

`atomic_tasks` accepts an iterable of connection aliases, including a one-shot iterator. The iteration
order is captured at construction; later changes to the source collection have no effect. A single string
is not a collection of aliases. Transactions commit in reverse order, as described above.
A connection alias of `atomic_tasks` the configuration lacks is refused when the worker starts.

## <a id="kiq-on-commit"></a>Sending tasks after commit

A task sent from inside a transaction may run before the transaction commits — and see none of its writes
— or run for a transaction that then rolls back. `TaskiqDelivery` sends it through the
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
        loan = await Loan.objects.create(book=book, reader=reader)
        await tasks.kiq_on_commit(send_due_date_reminder, loan.id)
    # The event is committed; the relay is woken to send the task.
```

`TaskiqDelivery(broker, *, model, topic="taskiq", using=None, wakeup=None)` is the outbox delivery sending
tasks, and the sender writing them; `model` is the outbox model's class or its `"app.Model"` label, looked up
in the current context when it is first needed. `await kiq_on_commit(task, *args, labels=None, **kwargs)`
takes the task or its name, its arguments — JSON values — and its labels, and writes the task as an outbox
event of `topic` (`OutboxEvent.enqueue()`) — in the current transaction, so a rolled-back transaction leaves
neither the event nor the task; outside a transaction it commits at once. The broker's `HareTaskiqMiddleware`
writes the tenant scope active then into the tenant label of the event, so the task runs in the sender's
tenant, not in the relay's. It returns the event.

The relay of `get_relay(**relay_options)` sends the tasks — the events of the delivery's `topic` only, so
the table can hold other topics too — a relay of the other events of the table takes
`exclude_topics=[delivery.topic]`. The delivery's wakeup — an `InProcessWakeup` of its own by default —
wakes the running relay after the commit, so run the relay in the process sending the tasks; give a
broker's wakeup (`RedisWakeup`, ...) to send from another one at once. A task the broker didn't take (it
was down) is retried by the relay like any outbox event.

Delivery is at least once: a task can arrive twice. Include a stable business idempotency key in its
arguments or labels and use it to handle duplicates; `event.id` is not automatically passed to Taskiq.
`using` names the connection the tasks are written on — the model's by default. `get_relay()` uses the same
connection
unless its `using` option explicitly selects another one. An empty `topic` raises
`ValidationError` when enqueuing a task.

## <a id="testing"></a>Testing

taskiq's `InMemoryBroker(await_inplace=True)` runs each task as it is sent, in the sending task — with
`HareTaskiqMiddleware` added to it the tests see the tags, tenants and transactions the workers get:

```python
broker = InMemoryBroker(await_inplace=True)
broker.add_middlewares(HareTaskiqMiddleware(atomic_tasks=True))
```

`HareTaskiq(..., hare_context=hare_app_db)` explicitly borrows the test's context instead
of opening its configuration's databases — the tasks' writes are rolled back with the test's; see
[Application tests](../testing/setup.md#application-tests).

An explicitly supplied `hare_context` is borrowed: the integration neither initializes nor closes it.
The caller controls its lifetime. Without it, the application creates and closes a separate context.
Application startup does not set a process-global fallback. Pass a test fixture explicitly as
`hare_context=hare_app_db` or `hare_context=hare_app_transactional_db`.
