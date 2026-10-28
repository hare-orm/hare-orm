# ClickHouse: transactions and locks

ClickHouse runs transactions over tables of the `MergeTree` family on a server with a ClickHouse Keeper —
an experimental server feature, so hare uses it only when the connection asks for it. ClickHouse has no
row locks.

## <a id="transactions"></a>Transactions

```python
config = {
    "connections": {
        "analytics": "clickhouse+clickhouse-connect://default:secret@clickhouse.local:8123/analytics?transactions=true"
    }
}
```

With `transactions=true`, `Transactions.atomic()` runs a ClickHouse transaction. The server must run a
ClickHouse Keeper and have `allow_experimental_transactions` on: the connection checks it when it opens,
with a `BEGIN TRANSACTION` and its `ROLLBACK`, and raises `ConfigurationError` otherwise. Without
`transactions=true` `atomic()` raises `UnSupportedError`. `Transactions.autonomous()` with an option
(`isolation`, `read_only`, ...) runs one on its new connection too, rolled back when the block raises.

```python
async with Transactions.atomic():
    await Loan.objects.create(id=1, book_id=7, reader_id=3)
    await Inventory.objects.filter(book_id=7).update(available=0)
    await Reservation.objects.filter(book_id=7).delete()
```

- **A connection of its own.** The transaction holds an HTTP session (clickhouse-connect) or a TCP
  connection (clickhouse-driver) from `BEGIN TRANSACTION` to `COMMIT`/`ROLLBACK`; its statements run on
  it one at a time, those of concurrent tasks waiting their turn.
- **What runs in it.** Inserts — the binary ones of `bulk_create()` too —, `SELECT`, mutations and
  lightweight `DELETE` of `MergeTree` tables. A statement the server runs in no transaction — DDL, a
  query of a `system` table whose storage takes no part in one (`system.parts`; `system.one` and
  `system.tables` are read), any statement of a `Replicated` table or of a `Distributed` one (a model of
  `distributed_over`) — raises `UnSupportedError`.
- **The first failed statement ends it.** The server rolls the transaction back and takes nothing but its
  `ROLLBACK`: a later statement raises `TransactionManagementError`, and so does the `COMMIT`, after
  rolling back. A statement the client stopped waiting for (a cancelled task, `asyncio.wait_for`) runs on
  in the transaction's HTTP session; the rollback waits for it — up to the server's session timeout — so
  the transaction's rows don't stay visible.
- **No savepoints.** A nested `atomic()` joins the transaction it is nested in. An error leaving the
  nested block can't be undone apart from the rest: even if the outer block catches it, the transaction
  rolls back when it ends and raises `TransactionManagementError` naming the error.
- **A snapshot.** A transaction reads the rows as they were when it began, with its own writes:
  `isolation` up to `REPEATABLE_READ` runs at it, `SERIALIZABLE` raises `UnSupportedError`.
  `read_only=True`, `statement_timeout` and `lock_timeout` raise `UnSupportedError` — ClickHouse has
  none of them for a transaction, and no lock to wait for.
- **Reads outside a transaction see uncommitted rows.** A statement run outside every transaction reads
  the rows other transactions wrote and haven't committed. A reader that must not see them runs in a
  transaction of its own, or with the setting `implicit_transaction=1`.
- **A part another transaction holds.** A mutation of the transaction (`update()`, a captured write's
  update) can meet a part a background merge of the server holds; the server fails it with a
  serialization error, which hare raises as `TransactionRetryError` — the transaction is rolled back, and
  running it again succeeds once the merge is done (`atomic(retries=...)` does it). A write of a model
  with `Meta.change_capture` runs in such a transaction even without `atomic()`.

## <a id="generated-keys"></a>Generated keys

A model whose key the database generates takes its keys from a series of ClickHouse Keeper
(`generateSerialID`) — see [Models](models.md#keys). The series needs no
`transactions=true`.

## <a id="row-locks"></a>Row locks

ClickHouse locks no row: `select_for_update()` raises `UnSupportedError` before any SQL is sent, in a
transaction or out of one, and so does `RowChanges.get_for_update()` of a request query. A write can't
wait for another one to end — two `update()`s of one row both apply, the later mutation's values staying.

ClickHouse Keeper (`keeper_hosts`) holds `migrate`'s [migration lock](../../migrations/migrations.md#migration-lock),
and deleting a database (`db_delete()`) deletes its nodes there — the [series of its keys](models.md#keys) and
the node of its lock; hare speaks ZooKeeper's protocol to it itself — no library is installed for it.
