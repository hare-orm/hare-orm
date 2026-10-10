# Transactional outbox

Writing a row and announcing it are two operations: if the process stops (or a broker is briefly
unreachable) between them, either the announcement is lost, or it goes out for a write that then
rolled back. The transactional outbox writes the announcement as one more row — an **outbox
event** — in the *same* transaction as the write it reports, so both commit or roll back together.
An `OutboxRelay` then delivers the events to where they go: Kafka, RabbitMQ, Redis streams, an HTTP
endpoint, taskiq, or a function of your own.

Delivery is **at least once**: an event can arrive twice (a relay stopped between delivering and
recording it), so a receiver deduplicates on the event's `id`, which every delivery sends.

There are two ways to write events:

- `OutboxEvent.enqueue()` — an event of your own, written where you call it;
- `Meta.change_capture = ChangeCapture(...)` — an event for every row the ORM's writes of a model
  change, written by the write itself.

## <a id="outboxevent"></a>The outbox model

`OutboxEvent` is abstract; a project declares its concrete model, combined with its own base model
through multiple inheritance:

```python
from hare.contrib.outbox import ListenNotifyWakeup, OutboxEvent


class MyOutboxEvent(YourBaseModel, OutboxEvent):
    class Meta(YourBaseModel.Meta, OutboxEvent.Meta):
        outbox_wakeup = ListenNotifyWakeup()
```

| Column | Meaning |
|---|---|
| `sequence` | The primary key, a `BIGINT` the database generates — the order the relay delivers in |
| `id` | The event's `UUID` — what a receiver deduplicates on |
| `topic` | Up to 255 characters — deliveries route on it |
| `payload` | A JSON value |
| `headers` | A JSON object — metadata besides the payload (a trace context, the author of a change), carried into the headers of a Kafka, RabbitMQ or HTTP message |
| `ordering_key` | Events of one key are delivered strictly in order; `NULL` for none |
| `idempotency_key` | Unique — at most one event per key |
| `created_at` | When the event was written |
| `attempts`, `last_error` | Failed deliveries and the error of the last one |
| `next_attempt_at` | When a failed event is tried again; `NULL` for at once |
| `lease_until`, `leased_by` | The relay delivering the event and until when |
| `published_at` | When it was delivered |
| `dead_lettered_at` | When it ran out of attempts — it is no longer delivered |

`OutboxEvent.Meta` declares only `abstract = True` and the relay's own indexes: partial indexes of
the events still to deliver (by `sequence`, and by `ordering_key, sequence`), of the delivered and of
the dead-lettered events, and `(topic, sequence)`.

> [!WARNING]
> **Keep `OutboxEvent.Meta` minimal**
>
> `ModelMeta` merges every abstract ancestor's `Meta` attributes into a concrete subclass through a
> reversed-MRO walk, and on a key **collision** between two ancestors' `Meta` (`YourBaseModel.Meta`
> and `OutboxEvent.Meta` both setting an attribute) whichever comes later wins — no error. Put
> project options on `YourBaseModel.Meta`, never a `table` name or `indexes` on `OutboxEvent.Meta`.

The events' `sequence` is taken when they are written. The events of one row are written while the
write holds that row's lock, so their order is the order of their commits whatever the clocks of the
hosts — the relay orders by `sequence` alone.

### <a id="encoding"></a>Payload and headers: `OutboxJsonEncoding`

The `payload` and `headers` columns are written with `OutboxJsonEncoding` — the same for `enqueue()`
and a captured change. Besides JSON's own values it writes an explicit table:

| Value | JSON |
|---|---|
| `Decimal` | its exact text, `"12.50"` |
| `UUID` | its text |
| `datetime`, `date`, `time` | ISO 8601 |
| `timedelta` | its seconds, a number |
| an IP address or network | its text |
| `bytes` | base64 |
| an enum member | its value |

Any other value raises `ValidationError` (its cause a `TypeError`) when the event is written — it
never quietly becomes its `str()`.

## <a id="enqueue"></a>`enqueue()`

```python
OutboxEvent.enqueue(
    topic, payload, *, using=None, idempotency_key=None, ordering_key=None, headers=None,
    extra_field_values=None, wakeup=None,
)
```

```python
async with Transactions.atomic():
    order = await Order.objects.create(customer=customer, total=total)
    await MyOutboxEvent.enqueue(
        "shop.order.placed",
        {"order_id": order.id, "total": order.total},
        ordering_key=f"order:{order.id}",
        headers={"author": user.id},
    )
```

Inside `Transactions.atomic()` every ORM call resolves to the transaction's connection, so the event
and the write it reports share one `COMMIT`; a rollback drops both. Outside a transaction the event
commits at once, like any `create()`. Once the transaction commits (at once outside one) the wakeup
signals the relays — the model's `Meta.outbox_wakeup`, or `wakeup=`.

> [!WARNING]
> **The outbox model lives on the connection of the write**
>
> Two connections never share a transaction. When `using=` isn't given, the model's connection has
> no open transaction and another connection has one, `enqueue()` raises `ConfigurationError`
> instead of committing the event on its own. Pass `using=` if that is intended.

The arguments are checked before anything is written (`ValidationError`): `topic`,
`idempotency_key` and `ordering_key` strings of 1 to 255 characters, `headers` a mapping of
non-empty text names, `extra_field_values` setting none of `enqueue()`'s own fields nor those the
ORM and the relay keep (`sequence`, `created_at`, `next_attempt_at`, `published_at`,
`dead_lettered_at`, `attempts`, `last_error`, `lease_until`, `leased_by`).

### <a id="idempotency-key"></a>`idempotency_key=`

At most one event ever exists per key. When one already exists, `enqueue()` returns **it** unchanged
(its own topic and payload) — no error, no duplicate, no wakeup signal. Retrying a handler or
re-running a job is safe.

- It is `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` and a `SELECT` of the event under the
  key, on one connection. A conflict never raises, so it never aborts an enclosing PostgreSQL
  transaction. A concurrent transaction enqueuing the same key waits for the first one and then
  gets its event, or writes its own if the first rolled back.
- Only a conflict on the key is deduplicated. A conflict on any other unique constraint raises
  `IntegrityError`.
- A key freed by a rolled-back transaction can be used again. A delivered, dead-lettered or
  soft-deleted event still holds its key.
- On a tenant-scoped model (`Meta.tenant_field`) keys are unique **across** tenants: a key another
  tenant's event holds raises `IntegrityError` (as does one committed after a
  `REPEATABLE READ`/`SERIALIZABLE` snapshot). For keys unique **per** tenant, redeclare
  `idempotency_key` without `unique=True` and add `UniqueConstraint(fields=("tenant_id",
  "idempotency_key"))`; with no unique constraint covering the key a keyed `enqueue()` raises
  `ConfigurationError`.
- The keyed event is written with `bulk_create()`, so a `save()` overridden on the outbox model isn't
  called for it.

### <a id="extra-field-values"></a>Subclass columns: `extra_field_values=`

A subclass with columns of its own (a relation to what the event is about, a tenant) fills them
through `extra_field_values=`. The tenant comes from the active `Tenancy.scope()` or from these
values — an explicit tenant is accepted without an active scope, as `create()` accepts it.

## <a id="change-capture"></a>Capturing a model's changes: `ChangeCapture`

```python
from hare.contrib.outbox import ChangeCapture, ChangePayload


class Order(Model):
    status = fields.CharField(max_length=20)
    total = fields.DecimalField(max_digits=10, decimal_places=2)
    customer = fields.ForeignKeyField("shop.Customer")

    class Meta:
        change_capture = ChangeCapture(MyOutboxEvent, payload=ChangePayload.AFTER)
```

Every ORM write of the model writes an outbox event per changed row, **in the write's own
transaction**:

| Write | Captured as |
|---|---|
| `save()`, `create()`, `get_or_create()`, `update_or_create()` | the inserted or updated row |
| `bulk_create()`, `bulk_update()` | each row — an upsert's updated rows as updates |
| `QuerySet.update()`, `QuerySet.restore()` | each matched row |
| `delete()`, `QuerySet.delete()` | each deleted row; a soft delete as an update of the soft-delete field |
| what `on_delete` reaches | the rows a `CASCADE` deletes and those `SET_NULL`/`SET_DEFAULT` updates — the database's own `ON DELETE CASCADE`/`SET NULL` included |
| `insert_from()`, `merge()` | each row written |
| the links of a many-to-many relation | the rows of a `through=Model` model declaring `change_capture`, inserted and deleted |

Raw SQL isn't captured. An auto-generated through table has no model, so declare a through model
to capture the links of a relation. A write outside a transaction opens one around itself and its
events — only for a captured model; on a database without transactions the events are written right
after the write.

### <a id="change-capture-declaration"></a>Declaration

```python
ChangeCapture(
    outbox, *, operations=(INSERT, UPDATE, DELETE), payload=ChangePayload.AFTER, fields=None,
    exclude=(), topic="{app}.{model}.{operation}", ordering_key="{label}:{pk}", extend=None,
)
```

| Argument | Meaning |
|---|---|
| `outbox` | The concrete `OutboxEvent` model — on the captured model's connection, checked by `Hare.init()` |
| `operations` | The `RowOperation`s captured |
| `payload` | What an event holds of its row — below |
| `fields` | The fields an event holds; a relation is held by its key (`customer_id`). By default every field written in the table but the `sensitive` and encrypted ones — those only when named here |
| `exclude` | Fields left out of the default ones — not with `fields` |
| `topic` | A template of `{app}`, `{model}`, `{table}` and `{operation}` (`inserted`, `updated`, `deleted`), or a function of the change. By default `"{app}.{model}.{operation}"` in lower case — `shop.order.updated` |
| `ordering_key` | A template of `{label}` (`shop.Order`), `{pk}` (a composite key's parts joined by `:`), `{app}`, `{model}` and `{table}`, or a function of the change; the row's own by default, so one row's events are delivered in order. `None` for no ordering |
| `extend` | A function of the change — sync or async — giving a `ChangeExtension` or `None`; it runs at the write, in its transaction |

The declaration is checked when the model is finalised (`ConfigurationError`): a field name that
isn't written in the table, a template naming something else, `BEFORE_AND_AFTER` without
`Meta.track_dirty_fields`, an abstract outbox, an outbox capturing changes itself. Declared on an
abstract base model, it captures every model built on it — each under its own name — so one
declaration covers a service.

A topic is the model's label, not its table: it is unique across apps and connections, the same in
migrations and generic relations, and tells nothing of the storage (a tenant's schema, partitions).
`topic="{table}.{operation}"` names the table instead. The dots match the patterns of
[`TopicRouter`](#topicrouter) (`shop.order.*`) and RabbitMQ's topic exchanges.

### <a id="change-payload"></a>Payload modes and the envelope

The event's payload is the envelope:

```json
{"model": "shop.Order", "operation": "updated", "pk": 7, "changed": ["status"],
 "before": {"status": "new", "total": "10.00", "customer_id": 3},
 "after": {"status": "paid", "total": "10.00", "customer_id": 3},
 "occurred_at": "2026-10-06T12:00:00+00:00"}
```

A composite key is an object of its fields: `"pk": {"id": 7, "version": 2}`. `changed` names the
fields an update set — from `update_fields`, the dirty fields of a tracked instance, the assignments
of `update()`; `null` when not known and for an insert or delete.

| `payload` | `before` / `after` |
|---|---|
| `KEYS` | absent — the key only |
| `AFTER` | `after` — the row as the write left it; for a delete, `before` — the row as it was |
| `BEFORE_AND_AFTER` | both; `before` is `null` for an insert, `after` for a delete |

What each mode adds to a write:

| Write | `KEYS` | `AFTER` | `BEFORE_AND_AFTER` |
|---|---|---|---|
| `save()` | — | a captured field the instance hasn't loaded is read after the write | "before" from the instance's dirty-field snapshot |
| `create()` | — | the instance's values; a field waiting for its database default read after the write | — |
| `bulk_create()` | `RETURNING` of the keys | the rows read after the write | an upsert's rows read before it, locked |
| `QuerySet.update()` | `RETURNING` of the keys | `RETURNING` of the fields | `RETURNING OLD` where the server has it (PostgreSQL 18), else the rows read first with `SELECT ... FOR UPDATE` |
| `delete()`, `QuerySet.delete()`, a cascade | the keys — an instance's row read first, locked; a query's `RETURNING` | the deleted rows' fields, the same way | the same |
| `bulk_update()` | the rows read after | the rows read after | the snapshots, else the rows read first, locked |

A `QuerySet.delete()` of a captured model — or of a model whose `on_delete` reaches one — is carried
out by the ORM's cascade, so the rows the database's own cascade would delete are read first and
captured. A model without `change_capture` pays one attribute read per write; its statements don't
change.

### <a id="change-extension"></a>`ChangeExtension`

`extend` adds what a service knows of a change and the ORM doesn't — its recipients, its scope, the
author:

```python
from hare.contrib.outbox import ChangeCapture, ChangeExtension


async def add_author(change):
    return ChangeExtension(
        payload={"recipients": await get_recipients(change)},
        headers={"author": current_user_id.get()},
        extra_field_values={"tenant_id": change.tenant},
    )


class CapturedModel(Model):
    class Meta:
        abstract = True
        change_capture = ChangeCapture(MyOutboxEvent, extend=add_author)
```

| Attribute | Meaning |
|---|---|
| `payload` | Keys added to the envelope — none of its own (`ValidationError`) |
| `headers` | The event's headers |
| `extra_field_values` | Columns of the outbox model besides `OutboxEvent`'s own |

The change — `CapturedChange` (`hare.contrib.outbox`) — has `model`, `operation`, `pk`, `changed`, `before`, `after`,
`occurred_at` and `tenant` (the row's `Meta.tenant_field`). When the outbox model has a tenant field
too, it is the row's tenant unless `extra_field_values` sets it.

## <a id="outboxrelay"></a>`OutboxRelay`

```python
relay = OutboxRelay(
    MyOutboxEvent,
    KafkaDelivery("localhost:9092"),   # or an async def deliver(event)
    poll_interval_seconds=5.0,
    batch_size=100,
    max_delivery_attempts=5,
    retry_base_seconds=1.0,
    retry_max_seconds=300.0,
    lease_seconds=60.0,
    delivery_timeout_seconds=30.0,
    concurrency=10,
    topics=None,
    wakeup=None,                       # the model's Meta.outbox_wakeup by default
    name=None,                         # host:pid:random by default
)
async with relay:
    ...
```

The options are checked when the relay is made, type and range (`ConfigurationError`): the
durations finite numbers up to 86400 (`poll_interval_seconds`, `lease_seconds` and
`delivery_timeout_seconds` above 0, the retry pauses from 0, `retry_max_seconds` at least
`retry_base_seconds`, `delivery_timeout_seconds` below `lease_seconds`), `batch_size` and
`max_delivery_attempts` ints in `1..10000`, `concurrency` in `1..1000`, `topics` `None` or a
non-empty list of topics, `name` 1 to 255 characters.

`start()`/`stop()` are idempotent; `stop()` waits for the relay and closes the delivery's
connections. `Hare.close_connections()` doesn't stop a relay — stop it yourself, or use `async with`
for the application's lifetime.

### <a id="relay-poll"></a>One poll

1. **Claim.** One short statement leases the next batch to the relay — the events still to deliver,
   due (`next_attempt_at` passed or `NULL`), not leased by a live relay, in `sequence` order:
   `UPDATE ... SET lease_until, leased_by WHERE sequence IN (...) RETURNING`. Where the database locks
   rows (PostgreSQL) the candidates are read `FOR UPDATE SKIP LOCKED`, so several relays split the
   queue; on SQLite one writer at a time does it.
2. **Order.** An event of an ordering key isn't claimed while an earlier event of the key is
   undelivered — a failed or a dead-lettered one included. A key's events are delivered strictly in
   order, and a failure holds back only its own key; events without a key wait for nothing.
3. **Deliver** — outside any transaction, so a slow broker holds no connection or lock. A batch holds
   at most one event per key, so it is delivered in any order: a delivery sending batches gets it at
   once, another up to `concurrency` events at a time, each within `delivery_timeout_seconds`.
4. **Record** — one `UPDATE` marks the delivered events published; a failed event gets `attempts + 1`,
   `last_error` and `next_attempt_at = now + min(retry_base_seconds * 2 ** (attempts - 1),
   retry_max_seconds)`, spread by up to 10% either way. Out of attempts, it is dead-lettered
   (`dead_lettered_at`). The record is written even when the relay is stopped meanwhile.

A relay being stopped finishes its batch first; one whose process dies between delivering and
recording leaves its lease to expire, and the batch is delivered again — "at least once". While a poll claims events the relay polls again at once, else
it waits for a wakeup signal or `poll_interval_seconds`. The lease and retry times are the relays'
clocks.

`topics` limits the relay to those topics' events — claimed, delivered, counted and cleaned up — so
relays of different topics share one table.

### <a id="dead-letters"></a>Dead letters, backlog and cleanup

A dead-lettered event isn't delivered again, and it holds back the later events of its ordering
key until it is retried or removed:

```python
await relay.retry_dead_lettered()                       # every one
await relay.retry_dead_lettered(topics=["shop.order.updated"], ids=[event_id])
await relay.cleanup_dead_lettered(older_than=timedelta(days=30))
await relay.cleanup_published(older_than=timedelta(days=7))
```

`retry_dead_lettered()` gives the events their attempts back. The cleanups delete batch by batch
(`batch_size`, 1000 by default), never holding a long lock, and refuse a model with
`Meta.soft_delete_field` (`ConfigurationError`) — its delete wouldn't free the rows. The relay runs
neither on its own.

`get_backlog()` gives an `OutboxBacklog` for a health check: `pending` events, the
`oldest_pending_age_seconds`, the `dead_lettered` events.

### <a id="relay-observers"></a>Observing the relay

The relay reports through [`Observers`](../observability/observers.md):

| Event | Attributes |
|---|---|
| `OutboxDelivered` | `model`, `event_id`, `topic`, `attempts`, `delay_seconds` — since the event was written |
| `OutboxDeliveryFailed` | `model`, `event_id`, `topic`, `attempts`, `error`, `next_attempt_at` |
| `OutboxDeadLettered` | `model`, `event_id`, `topic`, `attempts`, `error` |

```python
Observers.observe(OutboxDeadLettered, alert_on_call)
```

A failure is also logged — a WARNING, an ERROR for a dead letter — with the delivery's exception
chained to a `DeliveryError`.

## <a id="wakeups"></a>Wakeups

A wakeup starts a poll as soon as events are written, instead of the relay waiting out
`poll_interval_seconds`. One object is given to the outbox model (`Meta.outbox_wakeup`, or
`enqueue(wakeup=)`) and to the relays — nothing to keep in step by hand. The signals of one
transaction are joined, one per topic, and sent once it commits — never for a rolled-back one. A lost
signal is harmless: the relays poll anyway. A subscription that fails is retried with backoff and,
after five failures in a row, given up with an ERROR — the relay keeps polling.

| Wakeup | Extra | How |
|---|---|---|
| `InProcessWakeup()` | — | the relays of the same process, right after the commit |
| `ListenNotifyWakeup(channel="hare_outbox")` | — | a `NOTIFY` per topic in the events' transaction — PostgreSQL delivers it on commit; the relays `LISTEN` through a [`NotificationListener`](../dialects/postgresql/listen-notify.md). No signal on a connection without `LISTEN`/`NOTIFY` |
| `RedisWakeup(url, channel=...)` or `RedisWakeup(client=...)` | `redis` | pub/sub |
| `KafkaWakeup(bootstrap_servers, topic=..., create_topic=True, replication_factor=1)` | `kafka` | a topic of signals each relay reads from the end, outside any consumer group; created keeping signals for a minute |
| `RabbitMQWakeup(url, exchange=...)` | `rabbitmq` | a fanout exchange, an exclusive auto-deleted queue per relay |

A wakeup of your own subclasses `OutboxWakeup`: `signal(topics)` sends a signal, `listen(connection_alias)`
listens until cancelled and gives each signal to `dispatch(topics)`, `close()` releases what they opened.

## <a id="deliveries"></a>Deliveries

A delivery only sends: the relay retries, counts attempts and dead-letters. Every delivery sends the
payload's JSON as the body, and the event's `headers`, its `id` (`hare-outbox-event-id`) and topic
(`hare-outbox-topic`) as headers. An `async def deliver(event)` works as a delivery too (it is wrapped in
`CallableDelivery`). A delivery of your own subclasses `OutboxDelivery`: `deliver(event)` sends one event;
`deliver_batch(events, *, concurrency, timeout_seconds)` sends a batch (each through `deliver()` by
default) and returns per event `None` or what failed it; `close()` releases its connections. `get_body()`,
`get_headers()` and `get_key()` give an event's body, headers and message key as the built-in deliveries
send them.

| Delivery | Extra | Sends |
|---|---|---|
| `KafkaDelivery(bootstrap_servers, topic=None)` or `KafkaDelivery(producer=...)` | `kafka` | to `topic` or the event's topic, keyed by the ordering key — one key's events stay in one partition; a batch sent at once, `acks="all"` |
| `RabbitMQDelivery(url, exchange="", routing_key=None)` | `rabbitmq` | persistent messages through an existing exchange (the default one when empty), the routing key the event's topic by default; each confirmed by the broker (publisher confirms) |
| `RedisStreamsDelivery(url, stream=None, max_length=None)` or `(client=...)` | `redis` | an `XADD` to `stream` or the event's topic — the event's id, topic, ordering key, headers and payload as JSON; a batch through one pipeline, streams trimmed to `max_length` |
| `WebhookDelivery(url, secret=None, timeout_seconds=10, headers=None)` | `http` | a `POST`; with `secret`, `hare-outbox-signature: sha256=<HMAC-SHA256 of the body>`; any status other than 2xx fails the event |
| [`TaskiqDelivery`](taskiq.md#kiq-on-commit) | `taskiq` | a taskiq task |

### <a id="topicrouter"></a>`TopicRouter`

```python
delivery = TopicRouter(
    {
        "shop.order.*": KafkaDelivery("localhost:9092"),
        "notify.*": WebhookDelivery("https://hooks.example.com/notify", secret=secret),
    },
    default=log_event,
)
```

A topic named exactly takes its delivery, else the first pattern (`fnmatch`) it matches in the order
given, else `default`. An event of a topic no route takes **fails** — it is retried and
dead-lettered, never marked delivered.

## <a id="example"></a>Example: one declaration for a service

```python
from hare.contrib.outbox import (
    ChangeCapture, ChangeExtension, ChangePayload, KafkaDelivery, OutboxEvent, OutboxRelay, RedisWakeup,
    TopicRouter, WebhookDelivery,
)

wakeup = RedisWakeup("redis://localhost:6379/0")


class ServiceOutboxEvent(OutboxEvent):
    class Meta(OutboxEvent.Meta):
        outbox_wakeup = wakeup


def get_extension(change):
    return ChangeExtension(headers={"author": current_user_id.get()})


class ServiceModel(Model):
    class Meta:
        abstract = True
        track_dirty_fields = True
        change_capture = ChangeCapture(
            ServiceOutboxEvent, payload=ChangePayload.BEFORE_AND_AFTER, extend=get_extension
        )


relay = OutboxRelay(
    ServiceOutboxEvent,
    TopicRouter(
        {"billing.*": WebhookDelivery("https://billing.example.com/events", secret=secret)},
        default=KafkaDelivery("localhost:9092"),
    ),
)
```

Every model built on `ServiceModel` writes its changes as events of `{app}.{model}.{operation}`,
delivered in order per row.

## <a id="exceptions"></a>Exceptions

`hare.contrib.outbox.DeliveryError` — an event wasn't delivered: a delivery's own refusal (a webhook
answering an error, no route taking a topic), or the failure the relay logs, the delivery's
exception chained as `__cause__`. It is never raised to a caller of the relay; a failed event never
stops the rest of the batch.
