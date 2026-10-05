# Transactions

`Transactions` (`from hare import Transactions`) runs code in a database transaction:
`atomic()` as a block or a decorator, running a transaction again after a conflict, `autonomous()`
for a transaction of its own beside the current one, and callbacks after a commit or a rollback.

```python
class Transactions:
    @staticmethod
    def atomic(
        using: str | None = None,
        *,
        read_only: bool = False,
        statement_timeout: float | None = None,
        isolation: IsolationLevel | str | None = None,
        lock_timeout: float | None = None,
        retries: int = 0,
    ) -> Atomic   # async with Transactions.atomic(): ... / @Transactions.atomic() / async for attempt in ...

    @staticmethod
    @asynccontextmanager
    async def autonomous(
        using: str | None = None,
        *,
        read_only: bool = False,
        statement_timeout: float | None = None,
        isolation: IsolationLevel | str | None = None,
        lock_timeout: float | None = None,
    ) -> AsyncGenerator[DatabaseClient]

    @staticmethod
    def on_commit(callback: Callable[[], Any] | Callable[[], Awaitable[Any]], using: str | None = None) -> None

    @staticmethod
    def on_rollback(callback: Callable[[], Any] | Callable[[], Awaitable[Any]], using: str | None = None) -> None

    @staticmethod
    def distributed(coordinator: str, participants: Sequence[str]) -> AbstractAsyncContextManager[DistributedTransactions]
```

## <a id="in-transaction"></a>`atomic()` — context manager

```python
async with Transactions.atomic("default"):
    await widget.save()
    await StockMovement.objects.create(widget=widget, delta=-100)
```

If `using` is omitted, it falls back, in order: the single connection, if only one is
configured; otherwise the connection every registered app resolves its models to, if they all agree
on one `default_connection`; otherwise a connection literally named `"default"`, if one exists.
Otherwise `QueryError`, listing the configured connection names.

The block takes a connection from the pool when it starts. The `BEGIN` itself goes to the
database with the block's first statement — on the rust_pg driver in the same round trip, on asyncpg
and SQLite right before it — and a block that runs no statement never reaches the database: its
commit or rollback sends nothing. PostgreSQL takes a transaction's snapshot at its first statement,
not at `BEGIN`, and SQLite's `BEGIN` takes no lock before its first statement either, so what the
statements see is the same.

### <a id="nesting-and-concurrent-siblings"></a>Nesting and concurrent siblings

Calling `atomic()` again while already inside one opens a `SAVEPOINT` instead of
a new top-level transaction, and an exception in the inner level rolls back only that level:

```python
async with Transactions.atomic():
    await Widget.objects.using(...).create()
    async with Transactions.atomic():
        await StockMovement.objects.using(...).create()   # rolling back just this level leaves the outer work intact
```

Like the `BEGIN`, a level's `SAVEPOINT` goes to the database with the first statement run in it —
every enclosing level's first, outermost to innermost — and a level that runs no statement sends
nothing: neither the `SAVEPOINT` nor its `RELEASE`/`ROLLBACK TO`. Its `on_commit()`/`on_rollback()`
callbacks behave the same either way.

Spawning several **sibling** nested transactions concurrently off the same still-open parent — the
natural shape of doing per-item work inside a transaction via `asyncio.gather()` — is also safe:

```python
async def handle_item(item):
    async with Transactions.atomic():
        await Item.objects.create(item=item)


async with Transactions.atomic():
    async with Transactions.atomic():
        await asyncio.gather(*[handle_item(item) for item in items])
        await Batch.objects.create(status="processed")
```

The siblings fully serialize against each other on the underlying connection (SQL's savepoint stack
is strictly LIFO), but never deadlock against the still-open middle level they're nested under —
safe to rely on regardless of how many levels or siblings are involved.

A task spawned inside a nested block that is still running after the block has ended is no longer
part of it: its queries run at the level the block was opened from, without waiting on the finished
block.

The transaction's end is synchronized with every task working on it. The top-level `COMMIT`
(leaving the block, or a manual `commit()`) waits until the statements and savepoints sibling tasks
still run on it have finished, and holds the connection while it lands; a statement or savepoint a
sibling starts after the `COMMIT` began raises `TransactionManagementError` ("the transaction is
already being committed or rolled back"). If a sibling still holds its statement or savepoint after
30 seconds, nothing is committed: the transaction is rolled back and `TransactionManagementError` is
raised. A `ROLLBACK` waits the same way, but goes ahead after those 30 seconds. Once the transaction
has ended (a manual `commit()`/`rollback()` of the outer level inside a nested block included),
every wrapper of it — a nested one too — refuses to run anything with `TransactionManagementError`
("transaction already finalised"); nothing ever runs outside the transaction instead. A transaction
or nested context may be exited from a task other than the one that entered it.

A `COMMIT`/`ROLLBACK` that has started is never interrupted by cancellation, however many times the
task is cancelled (a `TaskGroup` tearing down, a repeated shutdown `cancel()`): the cancellation is
delivered only after the statement has landed, the callbacks have run and the connection is back in
the pool — bounded by 30 seconds against a server that stopped answering.

A transaction context is single-use — entering the same `atomic()` object a second time
raises `TransactionManagementError` before anything is sent to the database; create a new one with
`atomic()`. Any exception raised by the block, `TransactionManagementError` included, rolls
the transaction (or savepoint) back. On `rust_pg`, waiting for a free pool connection to start a
transaction is an ordinary cancellable wait, whether the pool is busy with other transactions or
with plain queries: `asyncio.wait_for()`/task cancellation act on it immediately, and `pool_acquire_timeout` (when set) bounds it.

### <a id="read-only-and-statement-timeouts"></a>Read-only transactions and statement timeouts

```python
async with Transactions.atomic(read_only=True, statement_timeout=5) as connection:
    rows = await connection.execute(user_sql)
```

`read_only=True` makes the **database itself** refuse every write in the block — there is no
client-side check, so raw SQL is refused too. `statement_timeout` (seconds, `0.001` up to
`2147483.647`) cancels any single statement that runs longer. `lock_timeout` (seconds, the same
range) fails any statement that waits longer for a lock another session holds, whether it runs or
not — a `statement_timeout` alone sets it on PostgreSQL too, an explicit `lock_timeout` takes its
place. All three are keyword-only, default to off, and are also accepted by `autonomous()`; a wrong
type or out-of-range value raises `QueryError`.

| Backend | `read_only=True` | `statement_timeout` | `lock_timeout` | Refused write raises |
|---|---|---|---|---|
| PostgreSQL (asyncpg, rust_pg) | `SET TRANSACTION READ ONLY`, the first statement after `BEGIN` | `SET LOCAL statement_timeout` and `SET LOCAL lock_timeout`, in whole milliseconds | `SET LOCAL lock_timeout`, in whole milliseconds | `TransactionManagementError` (SQLSTATE 25006) |
| SQLite | `PRAGMA query_only = ON` after `BEGIN`, switched back `OFF` before the `COMMIT`/`ROLLBACK` — also on an exception, a failed start or a manual `commit()`/`rollback()` — so it never outlives the block on the shared connection | the running query is interrupted (`sqlite3_interrupt`) once the timeout passes | the connection's `busy_timeout`, set before `BEGIN` and restored when the transaction ends | `TransactionManagementError`, as on PostgreSQL |
| ClickHouse ([`transactions=true`](../dialects/clickhouse/transactions-and-locks.md#transactions)) | `UnSupportedError` | `UnSupportedError` | bounds the waits for [row locks](../dialects/clickhouse/transactions-and-locks.md#row-locks) | — |

A timed-out statement raises `OperationalError` on every backend. Every setup statement
(`SET ...`/`PRAGMA ...`) runs through the ordinary query path, so observers of `QueryExecuted` see it, and the
transaction still fires the usual `BEGIN`/`COMMIT`/`ROLLBACK` transaction events. `SET LOCAL` ends
with the transaction, and the SQLite write refusal is lifted before `on_commit()` callbacks run, so
those callbacks can write. A plain transaction (no restriction requested) sends no extra statements.
There is no MySQL backend in hare-orm.

On SQLite a statement left by task cancellation (`asyncio.wait_for()`, `task.cancel()`) is
interrupted at once (`sqlite3_interrupt`) — inside or outside a transaction, with or without
`statement_timeout` — instead of running on and holding the shared connection. An interrupted
**write** makes SQLite roll back the whole transaction, savepoints
included — no savepoint can keep the outer transaction alive there. hare then treats the transaction
as aborted, like PostgreSQL does after an error: the timeout error says "SQLite rolled back the whole
transaction", every later statement in the block raises `TransactionManagementError` ("current
transaction is aborted"), leaving the block without an exception raises the same error instead of
committing, and nothing is committed. The same applies to any other statement SQLite answers by
ending the transaction, such as a trigger's `RAISE(ROLLBACK, ...)`.

Nesting rules:

- `read_only=True` inside a read-write transaction raises `QueryError` — a savepoint can't
  make part of a read-write transaction read-only.
- `read_only=True` inside a read-only transaction is allowed and is an ordinary savepoint.
- A plain nested `atomic()` inside a read-only transaction is allowed and stays read-only —
  its writes are refused by the database like any other write in the block.
- `statement_timeout` or `lock_timeout` on a nested transaction raises `QueryError`; a nested
  transaction runs with the outer transaction's timeouts.

### <a id="isolation-level"></a>Isolation level

```python
from hare.transactions.enums import IsolationLevel

async with Transactions.atomic(isolation=IsolationLevel.SERIALIZABLE):
    account = await Account.objects.get(pk=account_id)
    account.balance -= amount
    await account.save(update_fields=["balance"])
```

`isolation` takes an `IsolationLevel` — `READ_UNCOMMITTED`, `READ_COMMITTED`, `REPEATABLE_READ`,
`SERIALIZABLE`, weakest first — or its name (`"repeatable read"`); `None`, the default, leaves the
database's own default level. `autonomous()` takes it too. A name that is no level raises `QueryError`.

The transaction runs at the weakest level the database has that is at least as strong as the one
asked for — the SQL standard lets a database run a transaction at a stronger level than requested,
never a weaker one. A level stronger than any the database has raises `UnSupportedError` before
anything is sent.

| Database | Levels | Statement |
|---|---|---|
| PostgreSQL (asyncpg, rust_pg) | all four; `READ UNCOMMITTED` behaves as `READ COMMITTED` | `SET TRANSACTION ISOLATION LEVEL ...`, the first statement after `BEGIN` |
| SQLite | `SERIALIZABLE` only — every transaction already runs at it, so any level is accepted | none |
| ClickHouse | a snapshot — up to `REPEATABLE_READ`; `SERIALIZABLE` raises `UnSupportedError` | none |

A nested transaction runs at the level of the transaction it is nested in: it may repeat that level,
and naming another one raises `QueryError`. A connection whose database has no transactions
at all (`Features.supports_transactions` is `False`, see [Dialects and features](../dialects/dialects-and-features.md)) refuses
`atomic()` with `UnSupportedError`.

## <a id="atomic"></a>`atomic()` — decorator

```python
@Transactions.atomic("default")
async def transfer(source, destination, amount):
    ...
```

Every call of the function runs in a transaction of its own, as `async with Transactions.atomic(using, read_only=...,
statement_timeout=..., isolation=..., lock_timeout=...): return await func(...)` would; the arguments are validated
when the decorator is applied, and `retries` calls the function again (below).

## <a id="retries"></a>Running a transaction again — `retries`

A transaction at `SERIALIZABLE` (or `REPEATABLE READ`) can fail because of a concurrent one — the
database refuses to commit what would break serializability, or breaks a deadlock — and the same
transaction run again can succeed. hare raises `TransactionRetryError` for such an error (the
driver decides which errors they are). `retries=N` runs the transaction again up to `N` more times:

```python
@Transactions.atomic(isolation="serializable", retries=3)
async def transfer(source_id, destination_id, amount):
    source = await Account.objects.get(pk=source_id)
    ...

# A block runs again through `async for`: each attempt is a transaction of its own.
async for attempt in Transactions.atomic(isolation="serializable", retries=3):
    async with attempt:
        source = await Account.objects.get(pk=source_id)
        ...
```

- A run whose block or `COMMIT` ends with `TransactionRetryError` is rolled back (its `on_commit()`
  callbacks dropped) and followed by the next one, from the start of the function or block. The
  first run that commits ends it; when no run is left, the last `TransactionRetryError` is raised.
- Another error is never retried — it leaves at once, as without `retries`.
- Only a transaction that isn't nested runs again: nested in another one (a savepoint) it raises the
  error to the outer transaction, which has to run again as a whole — its own `retries` does that.
- Code in the block runs once per attempt: keep effects outside the database (a sent email, a
  call to a service) out of it, or in `on_commit()`.
- `retries` is an int from `0` (the default — one run) to `100`; another value raises `QueryError`.
  A plain `async with Transactions.atomic(retries=3):` raises `QueryError` when entered — a `with`
  block can't run twice; use the `async for` form. A run of the `async for` form that isn't entered
  with `async with attempt:` raises `QueryError` at the next one — nothing would ever end the loop.

## <a id="autonomous"></a>`autonomous()` — an independent connection

```python
async with Transactions.autonomous() as conn:
    await Job.objects.using(conn).create(status="running")
```

Hands you a brand-new connection that commits immediately, independent of any transaction already
open on the ambient connection — the write you make through it is durable even if the surrounding
`atomic()` block later rolls back. Closes the connection on exit.

With `read_only`, `statement_timeout`, `isolation` or `lock_timeout` (the same options `atomic()` takes, with
the same checks) the block runs in one transaction of its own on the new connection, committed when
the block exits and rolled back when it raises — a read-only, time-limited query apart from the
request's own transaction, such as a user's query in an application's SQL console:

```python
async with Transactions.autonomous(read_only=True, statement_timeout=5) as connection:
    rows = await connection.execute_dicts(user_sql)
```

Use this instead of configuring a second connection to the same database URL just
to get a write that commits outside a long-running enclosing transaction (e.g. progress reporting
for a long batch job) — `autonomous()` gets you the same independence on demand, and doesn't hold a
connection open for the app's whole lifetime.

> [!WARNING]
> Not a fit for SQLite `:memory:` (every connection is its own separate database), and SQLite in
> general serializes writers, so an autonomous connection there can block on the ambient one.

## <a id="on-commit"></a>`on_commit()`

```python
Transactions.on_commit(lambda: logger.info("order confirmed"))
```

Runs the callback only after the current transaction's real `COMMIT` (not after a savepoint
release). Outside a transaction, it runs immediately — an async callback outside a transaction
raises `QueryError` (await it yourself instead).

Callbacks run once the transaction is over, as ordinary code at the level the transaction was
opened from: the connection (and, on SQLite, its lock) is already free, so a callback can query,
open new transactions and register further callbacks. `on_commit()` called from inside a callback
sees no open transaction, so it runs immediately (an async callback raises `QueryError`, as
above). The same holds once the transaction has ended while it is still the ambient one — after a
manual `commit()`/`rollback()` inside the block, or from a task that outlived the block: like
Django once its atomic block is over, the callback runs immediately instead of being lost.

A callback belongs to the savepoint level it was registered at: when a nested block rolls back, only
the callbacks registered inside it are discarded (or, for `on_rollback()`, fired) — callbacks a
concurrent `asyncio.gather()`/`TaskGroup` sibling registered at another level meanwhile are
unaffected.

Every callback runs even if an earlier one failed. A single failure is raised as-is; two or more
are raised together as an `ExceptionGroup`. Observers of `TransactionEvent` still receive the
`COMMIT` event either way.

## <a id="on-rollback"></a>`on_rollback()`

```python
async with Transactions.autonomous() as conn:
    await JobProgress.objects.using(conn).create(status="started")

Transactions.on_rollback(lambda: logger.warning("main write was undone, rolling back progress too"))
```

The mirror of `on_commit()`: runs the callback only if the CURRENT transaction (or, for a callback
registered inside a nested savepoint, that savepoint itself) actually rolls back. A callback
registered inside a savepoint that instead releases successfully is NOT discarded — it stays live in
case the OUTER transaction later rolls back everything, savepoint included.

Typical use: a compensating action for a write issued through `autonomous()` (which commits
independently and so can't be undone by the enclosing transaction on its own) — register the
compensating action here so it runs if the enclosing transaction doesn't survive, even though the
two may be on entirely different connections/databases.

Unlike `on_commit()`, there is no "run immediately" fallback outside a transaction — a rollback that
can never happen has nothing sensible to attach to: `on_rollback()` outside a transaction raises
`QueryError`. On a transaction that has already ended (after a manual `commit()`/`rollback()`, or
from a task that outlived the block) the callback is never run and a warning is logged — that
transaction can no longer roll back.

A transaction whose connection is lost before its `COMMIT`/`ROLLBACK` lands is rolled back by the
server: it is reported as a `ROLLBACK` transaction event and its `on_rollback()` callbacks run. Only
when the connection breaks while the `COMMIT` itself is in flight is the outcome unknown — then
neither `on_commit()` nor `on_rollback()` callbacks run, the `ROLLBACK` event carries the connection
error as its `exception`, and `DBConnectionError` propagates.

Like `on_commit()` callbacks, these run after the transaction is over and may query and open
transactions. A transaction whose `COMMIT` is rejected (for example a deferred constraint violation)
counts as rolled back: its `on_rollback()` callbacks run and the `IntegrityError` still propagates.
If the block itself raised, that exception is the one that propagates — a failing `on_rollback()`
callback (or any other failure while rolling back) is logged and attached to it as a note instead of
replacing it. The same holds for a nested block (a savepoint).

On PostgreSQL a failed statement (an `IntegrityError` you caught, a statement cancelled by
`asyncio.timeout()`) aborts the whole transaction. Leaving such a block without an exception does
not commit: the transaction is rolled back, its `on_rollback()` callbacks run, no `on_commit()`
callback runs, the `ROLLBACK` transaction event is recorded and `TransactionManagementError`
("current transaction is aborted") is raised. A statement cancelled while in flight counts as failed
even when PostgreSQL never ran it, since whether it landed is unknown. To keep a transaction usable after an expected
error, wrap the failing statement in a nested `atomic()` and let the error leave it. A nested
block left normally whose savepoint can't be released (because a statement in it failed) is rolled
back to its savepoint — its `on_commit()` callbacks are discarded, its `on_rollback()` callbacks run
— and the release error (`TransactionManagementError`) is raised from it; the outer transaction stays
usable. SQLite doesn't abort a transaction on a failed statement, so there a caught
`IntegrityError` leaves the transaction committable.

`execute_script()` on a transaction's connection runs inside that transaction on every backend (on
SQLite statement by statement, so it doesn't commit the open transaction first).

> [!NOTE]
> **Not a real distributed rollback across different DBs**
>
> This is a compensating-action (saga) pattern, not an atomic guarantee — if the process crashes
> between the main transaction's rollback and the compensating callback running, the compensation
> never happens and the independent `autonomous()` write stays. True atomicity across two
> PostgreSQL databases is [`Transactions.distributed()`](distributed-transactions.md) — two-phase commit.

> [!WARNING]
> **Don't close a connection you got from `atomic()`/`autonomous()` yourself**
>
> Calling `.close()` on a connection while a transaction is open on it raises
> `TransactionManagementError` on every backend — closing mid-transaction isn't a coherent
> operation (on the Rust driver in particular, the transactional wrapper shares its pool with
> the outer, non-transactional client on that same alias, so tearing it down would take every
> other query on that connection down with it). Use `rollback()`/`commit()`, or simply exit the
> `async with` block, instead of closing the connection directly.
