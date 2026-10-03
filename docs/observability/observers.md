# Observing the ORM (`hare.instrumentation`)

One registry, `Observers`, tells you what the ORM does - every statement it sends, every
transaction it opens and closes, every row its writes change - so metrics, tracing, logging,
cache invalidation and automations hook in without hare-orm depending on any particular APM
library.

```python
from hare.instrumentation import Observers, QueryExecuted, RowsChanged, TransactionEvent

Observers.observe(event_type: type, callback: Callable, *, models: Iterable[type[Model]] | None = None) -> Callable
Observers.unobserve(event_type: type, callback: Callable, *, models: Iterable[type[Model]] | None = None) -> None
Observers.observing(event_type: type, callback: Callable, *, models: Iterable[type[Model]] | None = None)  # a `with` block
Observers.is_observed(event_type: type, model: type[Model] | None = None) -> bool
Observers.wrap_queries(wrapper: QueryWrapper) -> None
Observers.unwrap_queries(wrapper: QueryWrapper) -> None
await Observers.wait_for_pending(timeout_seconds: float | None = None) -> None
```

There are three events, each a frozen dataclass:

| Event | When | Attributes |
|---|---|---|
| `QueryExecuted` | Every query-executing call finished - `execute`, `execute_many`, `execute_script`, a bulk `COPY`, a stream - successful or not, on every connection, whatever the dialect and driver. | `sql` (with its [query tags](query-tags.md); `COPY <table> (<columns>) FROM STDIN` for a bulk load), `params` (`None` when there are none to show), `duration_ms` (for a stream - until it was read to the end or closed), `error` (`None` on success), `connection_name`. |
| `TransactionEvent` | A real, top-level transaction began, committed or rolled back. | `type`, `connection_name`, `duration_ms`, `error` - see [Transaction events](#transaction-events). |
| `RowsChanged` | A committed write changed rows of a model. | `model`, `operation`, `pks`, `fields`, `connection_name` - see [Row changes](#change-events). |

## Observing an event {: #observing }

```python
def count_query(event: QueryExecuted) -> None:
    metrics.histogram("db.query.duration_ms", event.duration_ms, tags={"failed": event.error is not None})


Observers.observe(QueryExecuted, count_query)
...
Observers.unobserve(QueryExecuted, count_query)
```

`observe()` returns the callback, so it works as a decorator-style one-liner too. Observing the
same callback again changes nothing for `QueryExecuted`/`TransactionEvent`; for `RowsChanged` it
adds the models (see [Which models an observer hears](#which-models)). `unobserve()` of a callback
that isn't registered does nothing.

How an observer runs depends on the event and on whether it is a plain function or an
`async def`:

- **`QueryExecuted` and `TransactionEvent`, a plain function** - called right away, inline in the
  task that ran the query, before the query call returns: by the time the query's `await` finishes
  the observer has already seen it. Keep it cheap - it adds to the query's own latency.
- **`QueryExecuted` and `TransactionEvent`, an `async def`** - runs **in the background**: a query
  call (`execute`, `Model.save()`, ...) never waits for it, however slow. One background task per
  event runs every `async def` observer of that event together (`asyncio.gather`), so one slow
  observer doesn't add to another's latency; on one event loop, an event's observers start only
  after the previous event's have finished, so they see events **in the order they happened**.
- **`RowsChanged`** - every observer, plain or `async def`, runs once the write's transaction
  commits, in the task that committed it; an `async def` is awaited there (see
  [When an event comes](#when-an-event-comes)).

For every event:

- An observer that raises is caught and logged (`"Observer %r raised on %r"`) - it never affects
  the query, the transaction, the write or any other observer.
- Without observers an event costs nothing: the event object isn't even built.

The background observers (`async def` observers of `QueryExecuted`/`TransactionEvent`) also:

- Run **outside every transaction** of the code that issued the query: an observer's own queries
  go through the alias's ordinary, non-transactional connection - they never join (and are never
  rolled back with, or race the `COMMIT` of) the observed transaction, so an audit record of a
  failed attempt survives its rollback. On SQLite such a query waits until the observed transaction
  has ended (one shared connection) - don't await `Observers.wait_for_pending()` inside a
  transaction whose observers write.
- **Fire no events themselves**: the queries and transactions a background observer runs reach no
  observer, so an audit observer that writes a row doesn't observe its own write and can't trigger
  itself forever.
- Are bounded: at most **1000** dispatches (one per event) wait or run at once. Past that -
  typically behind an observer that stopped finishing - new events aren't delivered to `async def`
  observers, with a warning when the backlog fills up and another (with the number dropped) once it
  drains.

## Where an observer is registered {: #where-observers-live }

| How | Gets the events of |
|---|---|
| `Observers.observe(event_type, callback)` | The whole process. |
| `ctx.observe(event_type, callback)` on a `HareContext` | Only while that context is the current one - the code running inside `async with ctx:` (or after `Hare.init()` made it the global one). `ctx.unobserve(...)` removes it; it goes with the context. |
| `with Observers.observing(event_type, callback):` | The block and the tasks started inside it - a `ContextVar`, so concurrent tasks each see only their own queries. |

```python
from hare.core.context import HareContext

async with HareContext() as ctx:
    await ctx.init(config)
    ctx.observe(QueryExecuted, count_query)  # sees only this context's work
    ...
```

```python
seen: list[str] = []
with Observers.observing(QueryExecuted, lambda event: seen.append(event.sql)):
    await Book.objects.filter(rating__gte=4).all()
assert len(seen) == 1
```

`observing()` nests: an inner block's observer is added to the outer ones, and both get the queries
of the inner block. Leaving the block in another task than the one that entered it (an
async-generator pytest fixture whose teardown runs in another task) doesn't raise - the observer is
dropped from the leaving task's context. `models=` narrows `RowsChanged` the same way for every
form.

## Waiting for background observers {: #waiting-for-pending }

```python
await Observers.wait_for_pending(timeout_seconds: float | None = None) -> None
```

Since `async def` observers run in the background, one can still be mid-flight (e.g. flushing a
metric to a remote collector) when your process wants to exit. `Hare.close_connections()` already
calls this for you, waiting at most **10 s** - observers still running then are cancelled, with a
warning, so a stuck observer can't block shutdown. Reach for it directly when you need every
event delivered so far to be fully handled at some other point (e.g. asserting on an observer's
effect in a test). `timeout_seconds` (a positive number; anything else raises `QueryError`) bounds
the wait the same way; `None` waits as long as the observers run.

```python
Observers.observe(QueryExecuted, ship_metric)  # an async def
...
await Observers.wait_for_pending()  # every event so far has now been handled
```

Every background observer's exception is logged when it happens. The exceptions raised since the
last call are also re-raised here - one on its own, several as an `ExceptionGroup`. Only the
**100** most recent are kept (the group's message counts the older ones), and they are kept without
the local variables of their traceback frames, so an observer that keeps failing can't grow memory
without bound.

## Wrapping every query: `QueryWrapper` {: #query-wrappers }

```python
from hare.instrumentation import Observers, QueryCall, QueryWrapper


class Timed(QueryWrapper):
    async def around(self, call: QueryCall, proceed):
        started = time.perf_counter()
        try:
            return await proceed()
        finally:
            metrics.timing(call.method_name, time.perf_counter() - started)


Observers.wrap_queries(Timed())
```

An observer sees a query once it has finished; a `QueryWrapper` runs **around** it, in the task
that issued it, around the driver call itself - the place for a tracing span that must be the
parent of whatever the driver does, a timer, or a guard that refuses a query. It is what
[OpenTelemetry instrumentation](opentelemetry.md) is built on.

- `around(call, proceed)` wraps one call: `await proceed()` runs it (through the next wrapper) and
  returns what it returned - return that.
- `around_stream(call, proceed)` wraps a stream (`QuerySet.stream()`) for as long as
  it is read: `proceed()` opens the stream - an async iterator of batches of rows, each a list - and
  returns it. The default passes it through.
- `QueryCall` carries `method_name` (`execute`, `execute_many`, `execute_script`,
  `execute_described`, `copy` or `stream`), `sql`, `params`, `connection_name` and `dialect`.
- Wrappers nest in order of installation, the first outermost. `wrap_queries()` of an already
  installed wrapper and `unwrap_queries()` of one that isn't installed do nothing.

## Slow-query logging {: #slow-query-logging }

Independent of any observer, every query slower than a configurable threshold (**500 ms** by
default) logs a `DEBUG`-level line (`"Slow query (%.1fms): %s: %s"`) through hare's database
client logger - make sure your logging setup surfaces `DEBUG` from hare's loggers if you want to
see it.

Override the threshold via `Hare.init()`:

```python
await Hare.init(config, slow_query_threshold_ms=200.0)
```

This sets `Observers.slow_query_threshold_ms` process-wide (it isn't a per-`HareContext` setting) -
read it directly if you need the current value. The value is a finite number of milliseconds from
`0` to `86400000` (`0` logs every query); anything else raises `ConfigurationError`, and the
threshold only changes once `init()` succeeds.

## Transaction events {: #transaction-events }

`QueryExecuted` only fires around individual statements. A transaction made of several
individually fast statements with real elapsed time between them (application-level delay, not DB
time) is invisible to it; `TransactionEvent` closes that gap.

```python
from hare.instrumentation import Observers, TransactionEvent
from hare.transactions.enums import TransactionEventType


def log_transaction(event: TransactionEvent) -> None:
    if event.type is not TransactionEventType.BEGIN:
        metrics.histogram("db.transaction.duration_ms", event.duration_ms, tags={"event": event.type.value})


Observers.observe(TransactionEvent, log_transaction)
```

- `type` is `TransactionEventType.BEGIN`, `.COMMIT` or `.ROLLBACK`.
- `duration_ms` is `0.0` for `BEGIN` (nothing to measure yet) and the transaction's own elapsed
  time for `COMMIT`/`ROLLBACK`.
- `error` is `None` for `BEGIN` and for an ordinary `COMMIT`/`ROLLBACK`. A transaction whose
  connection was lost is reported as a `ROLLBACK` carrying that connection error (when it broke
  while the `COMMIT` was in flight, the `COMMIT` may still have landed, and no
  `on_commit()`/`on_rollback()` callback runs). An observer that needs the cause of an ordinary
  rollback should wrap its own `async with Transactions.atomic():` block instead.
- **Never fired for a savepoint** (a nested transaction opened while already inside another one) -
  only a real, top-level transaction counts. Nesting several levels deep still fires exactly one
  `BEGIN`/`COMMIT` (or `BEGIN`/`ROLLBACK`) pair, for the outermost level only.

A plain-function observer runs inline (in the task that commits), an `async def` one in the
background, exactly as for `QueryExecuted`: `commit()`/`rollback()` never waits on it, and a
`BEGIN` observer's own queries never join the transaction that just began.

## Row changes {: #change-events }

After every write through the ORM, a `RowsChanged` event tells what changed: which model, which
rows (their primary keys), whether it was an insert, an update or a delete, and which fields were
written.

What it is for. An application often has to react to a change of its data, for example:

- drop a stale entry from a cache;
- send an update over WebSocket to the pages that are open;
- run an automation ("when an order changes, send an email").

`QueryExecuted` isn't enough for this: it only carries SQL text, which doesn't tell which rows
changed.

An example - an observer that drops the changed rows from a cache:

```python
from hare.instrumentation import Observers, RowsChanged


async def drop_cached(event: RowsChanged) -> None:
    await cache.drop(event.model, event.pks)


Observers.observe(RowsChanged, drop_cached)                                # the changes of every model
Observers.observe(RowsChanged, send_order_update, models=[Order, Invoice])  # only of Order and Invoice
```

A `RowsChanged`:

| Attribute | Meaning |
|---|---|
| `model` | The model whose rows changed. |
| `operation` | `RowOperation.INSERT`, `UPDATE` or `DELETE` (`from hare.instrumentation import RowOperation`). |
| `pks` | The primary keys of the changed rows. `None` when the ORM doesn't know them: a `QuerySet.update()`/`delete()` by a filter, the rows `on_delete` reached, a model without a primary key. |
| `fields` | The fields written. `None` when it isn't known: an insert, a `save()` without `update_fields`. |
| `connection_name` | The connection the write ran on. |

### Which models an observer hears {: #which-models }

- Without `models=` - the changes of every model.
- With `models=` - only the changes of those models and their subclasses. An observer of an
  abstract base model hears every model built on it.
- Observing with the same callback again adds the models to the ones it already hears. An observer
  of every model stays one; observing without models makes any observer one.
- `unobserve(RowsChanged, callback)` removes the observer entirely.
  `unobserve(RowsChanged, callback, models=[Order])` stops it hearing `Order` only; once it hears no
  model, it is removed. An observer of every model can't drop one model - `QueryError`: it is
  removed entirely.
- The models are model classes; a string (`"models.Order"`) or an instance is a `TypeError`.
  `models=` for `QueryExecuted`/`TransactionEvent`, which aren't about a model, is a `QueryError`.
- A model registered again while the application runs (`Hare.register_live_models()`) is a new
  class: an observer of that model is registered for the new class.
- `Observers.is_observed(RowsChanged, Order)` - whether any observer hears `Order`. When none
  does, no event of a change of `Order` is built or sent.

### Which writes report {: #which-writes-report }

- `save()` - an insert or an update (with `update_fields` when given), `delete()`, `hard_delete()`,
  `restore()`.
- `QuerySet.update()`, `QuerySet.delete()` and `QuerySet.hard_delete()` - when they changed at
  least one row.
- `bulk_create()` and `bulk_update()`. When the call fails, only the rows that stayed in the
  database are reported:
  - the batches of a `COPY`: `COPY` can't join a transaction and commits each batch itself;
  - the other rows of a `bulk_update()` that failed on a stale object;
  - the batches of a `bulk_update()` with an explicit `batch_size` written before the error.

  On any other failure every row is rolled back and nothing is reported.
- `bulk_create(ignore_conflicts=True)` reports only the rows it really inserted, and an upsert
  (`bulk_create(update_fields=..., on_conflict=...)`) reports each row as an insert or an update -
  whichever it was. On PostgreSQL the statement itself says which; on a backend without that, the
  ORM reads which keys already exist before the write, inside the same transaction, and only while
  an observer listens. Both need the rows back from the statement (`RETURNING`) and a primary key:
  on a backend without `RETURNING`, through `COPY`, or for a model without a primary key every row
  comes as an insert - plus, for an upsert, one update event of the `update_fields`.
- The rows `on_delete` reached (whether the ORM or the database does it):
  - `CASCADE` - as deletes;
  - `SET_NULL` / `SET_DEFAULT` - as updates of the foreign key's field.

  An observer of `Event` hears the delete of its rows when the tournament they belonged to was
  deleted, too.
- A soft delete comes as an update of the soft-delete field - for the model itself and for the
  related models `CASCADE` reached, when they have such a field. Related models without it are
  really deleted and come as deletes.
- One delete gives one event for each model it touched. The intermediate writes the ORM makes on
  the way through a cascade report nothing of their own.
- Many-to-many links: `add()`, `remove()`, `clear()`, `set()` - from both sides of the relation and
  for a `through=` model.

Raw SQL (`execute_sql()` and the like) doesn't report.

### When an event comes {: #when-an-event-comes }

- A write inside a transaction - once the transaction commits. A rolled-back transaction or
  savepoint reports nothing of its writes.
- A write outside a transaction - right after the write.
- A write through `Transactions.autonomous()` - once the autonomous transaction commits.

Observers are called in the order they were registered, in the task that committed the
transaction - the process's first, then the current `HareContext`'s, then the ones `observing()`
installed. A plain function is called, an `async def` awaited. An observer that raises is logged
and affects neither the write nor the other observers. An observer may run queries itself; its own
writes report too. While there are no observers, a write costs almost nothing: it only checks a
counter.
