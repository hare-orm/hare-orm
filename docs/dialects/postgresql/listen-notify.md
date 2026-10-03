# LISTEN/NOTIFY

Available on the client for either Postgres driver (`client = Connections.get(alias)`). Opens a
**dedicated, non-pooled** connection, `LISTEN`s on `channel`, and invokes `callback(connection, pid,
channel, payload)` — a plain **synchronous** callable, not a coroutine — for every `NOTIFY` received
on it. You own the returned object — it's never drawn from or returned to the connection pool.

The two drivers return genuinely different types, though:

- **`rust_pg` client** (`hare.dialects.postgresql.drivers.rust_pg`) — `client.listen(...)` returns a `pg.Listener`.
  Its `.is_closed()` reports `True` both after an explicit `await listener.close()` **and** if the
  underlying connection dies on its own (a dropped network, a killed backend, ...) — it doesn't
  just track whether you called `.close()`. If you drop the last reference without calling
  `.close()`, the dedicated connection still closes once Python garbage-collects the `Listener` —
  a safety net against a leaked connection, not a substitute for calling `.close()` explicitly
  (collection timing isn't guaranteed).
- **`asyncpg` client** (`hare.dialects.postgresql.drivers.asyncpg`) — `client.listen(...)` returns a plain
  `asyncpg.Connection`, with asyncpg's own `.is_closed()`. There is no separate `Listener` wrapper
  on this driver. Pool-only connection parameters (`min_size`, `max_size`, `max_queries`,
  `max_inactive_connection_lifetime`, ...) are left out of that dedicated connection; an unknown
  parameter raises `ConfigurationError`.

Either way, you must close what's returned when you're done listening:

```python
def on_notify(connection, pid, channel, payload):
    print(f"got {payload!r} on {channel}")

client = Connections.get("default")
listener = await client.listen("widget_ready", on_notify)
...
await listener.close()
```

## Sending: `client.notify(channel, payload="")` {: #notify }

```python
await client.notify("widget_ready", "abc123")
```

Runs a parameterized `SELECT pg_notify($1, $2)` — the channel and payload are bound values, so no
quoting or escaping is needed. It is available on the transaction client too
(`async with Transactions.atomic() as connection: await connection.notify(...)`); there
Postgres queues the notification and delivers it **only when the transaction commits** — on
rollback it is never delivered. Outside a transaction it is delivered immediately.

`client.features.supports_listen_notify` is `True` on both PostgreSQL drivers and `False` on
SQLite (which has neither `listen()` nor `notify()`).

## A self-reconnecting subscription: `NotificationListener` {: #notification-listener }

A raw `client.listen()` connection that dies (killed backend, network drop, idle timeout) just
stays dead. `hare.contrib.notify.NotificationListener` wraps it with a watchdog and reconnects:

```python
from hare.contrib.notify import NotificationListener

async def on_widget_ready(payload: str) -> None:  # a plain `def` works too
    ...

listener = NotificationListener(
    "default",                 # connection alias
    "widget_ready",            # channel
    on_widget_ready,           # callback(payload: str), sync or async
    reconnect_attempts=None,   # None = retry forever; N = give up after N failed retries
    backoff=0.5,               # base of backoff * 2**attempt, or an explicit sequence of delays
    max_backoff_seconds=30.0,  # cap for a single delay; None = uncapped
)
await listener.start()
...
await listener.stop()
```

or `async with NotificationListener(...) as listener: ...`.

- **`start()`** starts a background task and returns once the first `LISTEN` attempt has finished
  (connected, or failed and now retrying in the background). Idempotent. The task always uses the
  alias's shared client, even when `start()` is called inside a transaction.
- **`stop()`** cancels that task, closes the dedicated connection and cancels still-running async
  callbacks, waiting for all of it. Idempotent.
- **`run()`** is the loop itself — await it directly to run it in a task you own (`stop()` still
  works); it returns once the reconnect budget is exhausted.
- **`is_listening`** — whether a live `LISTEN` connection is open right now.
- **Callback** receives only the payload string. It runs on the event loop's thread with the
  contextvars of the code that started listening. If it returns an awaitable (an `async def`), that
  runs as a tracked background task. Exceptions from either type are logged, never propagated — a
  failing callback doesn't kill the subscription.
- **Watchdog/backoff:** the connection is checked once a second; when it dies, the listener
  reconnects. Consecutive failures (a failed connect, or a connection that dies before surviving
  one check) wait `backoff * 2**attempt` seconds (or the next entry of a `backoff` sequence, the
  last one repeating), clamped to `max_backoff_seconds`. After `reconnect_attempts` failed retries
  it logs at ERROR and stops. The counter resets once a connection survives a health check.
- **No support (SQLite):** `start()` logs at ERROR and does nothing — keep polling.
- Invalid `reconnect_attempts`/`backoff`/`max_backoff_seconds` (negative, non-finite, empty
  sequence, wrong type) raise `ConfigurationError` from the constructor.

!!! warning "NOTIFY is not persistent"
    A notification sent while the listener is reconnecting is lost. Use LISTEN/NOTIFY to cut
    latency on top of a durable source of truth (polling a table), never instead of it — exactly
    how [`OutboxRelay`](../../integrations/outbox.md) uses it.
