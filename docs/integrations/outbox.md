# Transactional Outbox (`hare.contrib.outbox`)

Writing a business row and publishing an event about it are two separate operations - if the
process crashes (or a message broker is briefly unreachable) between them, either the event never
goes out, or it goes out for a write that then rolled back. The transactional outbox pattern fixes
this by writing the event as an ordinary row in the *same* database transaction as the business
write - it's atomic by construction, since it's just one more `INSERT` in a transaction that either
commits or rolls back as a whole. A separate `OutboxRelay` process then reads unpublished rows and
delivers them to wherever they actually need to go (a message broker, a webhook, ...).

## `OutboxEvent` {: #outboxevent }

```python
class OutboxEvent(Model):
    id: UUIDField          # primary key, default uuid4
    topic: CharField        # max_length=255, indexed
    payload: JSONField[dict]
    created_at: DatetimeField   # auto_now_add=True
    published_at: DatetimeField | None  # null=True; set once delivered
    attempts: IntField      # default 0
    last_error: TextField | None
    idempotency_key: CharField | None   # max_length=255, null=True, unique=True

    class Meta:
        abstract = True
        indexes = (
            Index(fields=("topic", "created_at")),
            PartialIndex(fields=("published_at",), condition=Q(published_at__isnull=True)),
        )

    @classmethod
    async def publish(
        cls,
        topic: str,
        payload: dict,
        *,
        using=None,
        idempotency_key: str | None = None,
        notify_channel: str | None = None,
        extra_field_values: Mapping[str, Any] | None = None,
    ) -> Self: ...
```

Combine with your own project's base model through multiple inheritance, the same recipe
[`VersionedModel`](../soft-delete-versions-tenants/versioned-models.md) uses:

```python
class MyOutboxEvent(YourBaseModel, OutboxEvent):
    class Meta(YourBaseModel.Meta, OutboxEvent.Meta):
        pass
```

!!! warning "Keep `OutboxEvent.Meta` minimal"
    `OutboxEvent.Meta` deliberately sets just `abstract = True` plus the two `indexes` its own
    delivery query pattern needs (above) - nothing else. `ModelMeta`
    merges every abstract ancestor's `Meta` attributes into a concrete subclass via a
    reversed-MRO walk, and on a key **collision** between two abstract ancestors' `Meta`
    (`YourBaseModel.Meta` and `OutboxEvent.Meta` both setting the same attribute), whichever
    ancestor comes later in that walk silently wins - no error. Don't add a `table` name or extra
    `indexes` to `OutboxEvent.Meta` for this reason; put project-specific Meta options on
    `YourBaseModel.Meta` instead, where they can't collide with anything this package declares.

### Publishing {: #publishing }

```python
async with Transactions.atomic():
    widget = await Widget.objects.create(name="Left Handle")
    await MyOutboxEvent.publish(topic="widget.created", payload={"widget_id": str(widget.id)})
```

Without `idempotency_key`, `publish()` is just `await cls.create(topic=topic, payload=payload,
using=using)` - no special plumbing. Any ORM call made inside
`Transactions.atomic()` automatically resolves to that transaction's own connection (see
[Transactions](../connections/transactions.md)), so the outbox row and the business write it describes land in the same `INSERT`/`COMMIT` with zero
extra wiring. If the business write raises after `publish()` runs, the whole transaction - outbox
row included - rolls back with it; nothing is ever published for a write that didn't happen.

Calling `publish()` with no transaction open still works - it just commits immediately, same as
any other `create()` call.

!!! warning "Requires `MyOutboxEvent` on the SAME connection as the business write"
    This atomicity only holds when `MyOutboxEvent`'s own connection (the `default_connection` of
    its app in the config, or a
    `ConnectionRouter`) resolves to the exact same connection the enclosing transaction is open
    on - two different connections can never share one SQL transaction, no matter what. If
    `publish()` is called with no `using=` while its own connection has no active transaction
    but a DIFFERENT one does, it raises `ConfigurationError` rather than silently committing the
    event standalone (which would defeat the whole point of this pattern with no error at all).
    Pass `using=` explicitly, pointing at the connection you actually want, if this is
    intentional. The check applies to `idempotency_key=`/`notify_channel=` calls too.

### Idempotent publishing: `idempotency_key=` {: #idempotency-key }

```python
async with Transactions.atomic():
    await MyOutboxEvent.publish(
        topic="order.paid",
        payload={"order_id": order_id},
        idempotency_key=f"order-paid-{order_id}",
    )
```

At most one row ever exists per key. If a row with that key already exists, `publish()` returns
**that** row unchanged (its own `topic`/`payload`, not the ones just passed) - no error, no
duplicate. Retrying a handler, redelivering a webhook or re-running a job is safe, with no
`bulk_create(ignore_conflicts=True)` of your own (which would skip `publish()`'s wrong-transaction
check).

- It is atomic: `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING`, then a `SELECT` of the row
  under the key, on the same connection. A key conflict never raises, so it never aborts an
  enclosing Postgres transaction. Publishing the same key twice in one transaction, or in separate ones, both return
  the first row. A concurrent transaction publishing the same key waits for the first one to
  commit (or roll back) and then gets its row (or inserts its own).
- Only a conflict on the key is deduplicated. A conflict on any other unique constraint - the
  primary key, a unique column of a subclass set through `extra_field_values` - raises
  `IntegrityError` exactly as it does without a key (and on Postgres aborts the enclosing
  transaction like any other failed statement); it is never mistaken for "key already used".
- A key freed by a rolled-back transaction can be used again. A row that was already delivered, or
  soft-deleted (`Meta.soft_delete_field`), still holds its key.
- Without a key, behavior is unchanged: duplicates are allowed, and any number of rows with a
  `NULL` key coexist under the unique constraint on both Postgres and SQLite.
- The key must be a string of 1..255 characters, otherwise `ValidationError`.
- On a tenant-scoped model (`Meta.tenant_field`) keys are unique **across** tenants. If the key is
  held by another tenant's row, `publish()` raises `IntegrityError` instead of returning that row.
  The same happens under `REPEATABLE READ`/`SERIALIZABLE` if the holder committed after your
  snapshot (Postgres usually reports a serialization failure there first).
- For keys unique **per** tenant, redeclare `idempotency_key` on the subclass without `unique=True`
  and add an unconditional `UniqueConstraint(fields=("tenant_id", "idempotency_key"))` to
  `Meta.constraints`: `publish()` then targets that constraint in its
  `ON CONFLICT`. With no unique constraint covering the key at all, a keyed `publish()` raises
  `ConfigurationError`.
- The tenant comes from the active `Tenancy.scope()`, or from `extra_field_values` - an explicit
  tenant is accepted with no active scope, with or without a key, exactly as `create()` does.
  Under a scope of [several values](../soft-delete-versions-tenants/multi-tenancy.md#tenancy-scope) `extra_field_values` has to
  name it, and a keyed `publish()` returns the row stored under the key for that tenant.
- On the keyed path the row is written with `bulk_create()`, so a `save()` overridden on the
  outbox subclass is not called for it.

### Subclass columns: `extra_field_values=` {: #extra-field-values }

A subclass with columns of its own (a relation to what the event is about, a source label) fills
them through `extra_field_values=`, keyed or not:

```python
await DeliveryEvent.publish(
    topic=channel.key,
    payload={"message": message},
    idempotency_key=f"automation-{automation.id}-{event_id}",
    extra_field_values={"automation": automation, "event_key": event_key},
)
```

It can't set `topic`, `payload` or `idempotency_key` - pass those as `publish()`'s own arguments -
nor the fields the ORM and the relay maintain: `created_at`, `published_at`, `attempts`,
`last_error` (`ValidationError` for all of them, before anything is written). It may set the
primary key explicitly (e.g. a deterministic id); publishing the same key with the same explicit id
again returns the existing row and sends no second `NOTIFY`.

### Waking the relay: `notify_channel=` {: #notify-channel }

```python
async with Transactions.atomic():
    await MyOutboxEvent.publish(topic="widget.created", payload={...}, notify_channel="my_outbox_channel")
```

When a new row is written, `publish()` also sends `NOTIFY` on `notify_channel` (payload: the row's
id) through the same connection. Inside a transaction Postgres delivers it only on commit - never on
rollback - so an `OutboxRelay` with `listen_channel="my_outbox_channel"` wakes up exactly when the
row becomes visible. No `NOTIFY` is sent when an existing row is returned for an `idempotency_key` -
only a row this call really inserted is announced.
On SQLite (no LISTEN/NOTIFY; relays there only poll) `notify_channel` is silently ignored.

## `OutboxRelay` {: #outboxrelay }

```python
relay = OutboxRelay(
    MyOutboxEvent,
    deliver=send_to_message_bus,   # async def deliver(event: MyOutboxEvent) -> None
    poll_interval_seconds=5.0,
    batch_size=100,
    listen_channel=None,           # optional: a Postgres LISTEN/NOTIFY channel name
    max_delivery_attempts=5,
    backoff_base_seconds=0.5,
)
await relay.start()
...
await relay.stop()
```

or as an async context manager:

```python
async with OutboxRelay(MyOutboxEvent, deliver=send_to_message_bus) as relay:
    ...
```

The options are validated on construction, type and range, raising `ConfigurationError`:
`poll_interval_seconds` a finite number in `(0, 86400]`, `batch_size` an `int` in `1..10000`,
`max_delivery_attempts` an `int` in `1..10000`, `backoff_base_seconds` a finite number in
`[0, 3600]`, `listen_channel` `None` or a non-empty string (the limits are
`hare.contrib.outbox.constants.MAX_POLL_INTERVAL_SECONDS`/`MAX_BATCH_SIZE`/
`MAX_DELIVERY_ATTEMPTS_LIMIT`/`MAX_BACKOFF_BASE_SECONDS`).

`start()`/`stop()` are both idempotent. `stop()` cancels the relay's background task(s) and awaits
them, so it always returns with everything cleanly shut down - no leaked `asyncio.CancelledError`.
The relay always polls through the alias's shared client, even when `start()` is called inside a
transaction.

!!! warning "Not wired into `Hare.close_connections()`"
    Unlike connection pools, `Hare.close_connections()` has no idea an `OutboxRelay` exists.
    Your application must call `.stop()` itself before shutdown (or use the relay as an `async
    with` block scoped to your app's own lifetime) - the same situation
    `OpenTelemetryInstrumentor.uninstrument()` is in.

### Delivery, retry, and dead-lettering {: #delivery-retry-dead-lettering }

Each polling cycle claims up to `batch_size` rows where `published_at IS NULL` and `attempts <
max_delivery_attempts`, oldest first, then calls `deliver(event)` on each:

- **Success** (`deliver` returns normally): `published_at` is set to now and saved.
- **Failure** (`deliver` raises): `attempts` is incremented, `last_error` is set to `str(exc)` and
  saved, and the exception is wrapped in `DeliveryError` for logging. Once `attempts` reaches
  `max_delivery_attempts`, the row stops being claimed by any future poll - it's **dead-lettered**,
  not deleted: `published_at` stays `NULL` forever, so it's easy to find later:

```python
dead_lettered = await MyOutboxEvent.objects.filter(
    published_at=None, attempts__gte=relay.max_delivery_attempts
)
```

A transient failure that later succeeds (before hitting the cap) is delivered normally on a later
poll - there's no permanent penalty for a retry that eventually works.

On Postgres, each row (up to `batch_size` per polling cycle) is claimed with `SELECT ... FOR UPDATE
SKIP LOCKED` and delivered inside its **own** transaction, committed independently as soon as that
row's delivery succeeds and is saved. Consequences:

- Multiple relay instances (multiple app replicas, say) safely split one queue instead of
  double-delivering - a row already locked by one relay's in-flight transaction is simply skipped
  by another's concurrent poll, never blocked on.
- A row that fails to save after a successful `deliver()` (a transient DB error, or the polling
  task being cancelled mid-save) only rolls back that one row's own claim - it never undoes an
  earlier row's already-committed delivery from the same polling cycle. Rows are never claimed as
  one all-or-nothing batch transaction for exactly this reason.
- `deliver()` itself runs inside a savepoint of that transaction. A database error it raises
  (e.g. an `IntegrityError` from a write it makes) rolls back only its own writes, and the row's
  `attempts`/`last_error` are still recorded - the row is retried and eventually dead-lettered
  like any other failure instead of staying first in the queue.

**SQLite has no `FOR UPDATE`/`SKIP LOCKED` equivalent at all** - claiming there assumes a single
`OutboxRelay` instance (running more than one against the same SQLite database risks double
delivery), and rows are claimed as a single query with no wrapping transaction at all - each
`event.save()` commits immediately on its own.

### Polling is the backstop; LISTEN/NOTIFY is only latency {: #polling-and-listen-notify }

Polling (`poll_interval_seconds`) is always running and is the actual source of truth - it alone
guarantees every row eventually gets delivered. `listen_channel`, when set, additionally opens a
Postgres `LISTEN` subscription and triggers an extra poll cycle the moment a matching `NOTIFY`
arrives, for lower latency than waiting out the poll interval. A burst of `NOTIFY`s is coalesced:
at most one such poll runs at a time, and it makes one more pass if another `NOTIFY` arrived
meanwhile or it claimed a full `batch_size`. It is **not** a replacement for
polling: Postgres `NOTIFY` is not persistent - a notification sent while nothing is listening (relay
not started yet, listener reconnecting, ...) is simply lost forever, with no queuing or replay.
`NOTIFY` is only sent when you ask for it - pass the same channel as `publish(...,
notify_channel=...)` (see [above](#notify-channel)), or send it yourself (or from a
database trigger):

```python
async with Transactions.atomic() as connection:
    await MyOutboxEvent.publish(topic="widget.created", payload={...})
    await connection.notify("my_outbox_channel")
```

The subscription is a [`NotificationListener`](../dialects/postgresql/listen-notify.md) opened on the
same (router-chosen) connection `publish()` writes through. If it can't be established, or dies
and can't reconnect, the relay retries with exponential backoff (`backoff_base_seconds *
2**attempt`, uncapped) up to 5 consecutive failed retries, then logs at ERROR and keeps running
on polling alone - it never crashes the relay over a lost LISTEN connection. Both Postgres drivers (`asyncpg` and `rust_pg`) implement `.listen()`;
`listen_channel` is ignored (with one ERROR log line) only on a backend with no
LISTEN/NOTIFY support at all - i.e. SQLite.

### Cleanup {: #cleanup }

`OutboxRelay` does not delete old published rows on its own. Call `cleanup_published()` yourself,
at whatever cadence fits your own periodic-task setup:

```python
from datetime import timedelta

deleted = await relay.cleanup_published(older_than=timedelta(days=7))
```

## Exceptions {: #exceptions }

`hare.contrib.outbox.DeliveryError` wraps whatever `deliver` raised - it's what gets logged (via
`exc_info=`) alongside the WARNING/ERROR line for a failed/dead-lettered delivery. It is not
raised back at any caller; a failing row never stops the rest of the batch from being processed.

!!! note "Out of scope"
    Exactly-once delivery semantics (this is at-least-once - `idempotency_key` deduplicates
    *publishing*, but your `deliver` callable and whatever it hands off to should still be
    idempotent) and schema/message versioning aren't built in - layer those on top if you need
    them.
