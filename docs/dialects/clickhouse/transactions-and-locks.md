# ClickHouse: transactions and locks

ClickHouse runs transactions over tables of the `MergeTree` family on a server with a ClickHouse Keeper —
an experimental server feature, so hare uses it only when the connection asks for it. Row locks are kept
in ClickHouse Keeper too.

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
`transactions=true` `atomic()` raises `UnSupportedError`.

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
  `read_only=True` and `statement_timeout` raise `UnSupportedError` — ClickHouse has neither for a
  transaction; `lock_timeout` bounds the waits for [row locks](#row-locks).
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
(`generateSerialID`, ClickHouse 25.1) — see [Models](models.md#keys). The series needs no
`transactions=true`.

## <a id="row-locks"></a>Row locks

```python
"clickhouse+clickhouse-connect://default:secret@clickhouse.local:8123/analytics?transactions=true&keeper_hosts=keeper-1:9181,keeper-2:9181"
```

With `transactions=true` and `keeper_hosts`, `select_for_update()` locks the rows it reads until the
transaction ends:

```python
async with Transactions.atomic():
    inventory = await Inventory.objects.select_for_update().get(id=inventory_id)
    await Inventory.objects.filter(id=inventory_id).update(available=inventory.available - 1)
```

1. The keys of the rows are read in the transaction — the query's filters, ordering and slice applied.
2. Each row is locked by an ephemeral node of ClickHouse Keeper, `/hare/locks/<database>/<table>/<key>`,
   in a session the transaction opens with its first lock. The locks are taken in the order of their
   names, so two transactions locking the same rows never wait for each other's next one.
3. The rows are read by their keys, in the query's order.

A `COMMIT` or `ROLLBACK` closes the session, and Keeper deletes its nodes — the locks are given back.

| Call | On a row another transaction holds |
|---|---|
| `select_for_update()` | Waits until that transaction ends — up to the transaction's `lock_timeout` (`atomic(lock_timeout=...)`), then `OperationalError`; without one, as long as it takes. |
| `select_for_update(nowait=True)` | `OperationalError` at once. |
| `select_for_update(skip_locked=True)` | Leaves the row out; a slice is filled with the next free rows. |
| `select_for_update(of=("author",))` | Locks the rows of the relations named, joined by `select_related()` — `"self"` for the model's own. |

`no_key=True` takes the same lock; `share=True` and `key_share=True` raise `UnSupportedError` — a lock of
Keeper is exclusive. `get()`, `first()`, `values()`, `iterator()` and `stream()` lock their rows the same
way.

- **A row changed while waited for.** The transaction reads the snapshot it began with, so after waiting
  for a lock it reads the rows it waited for again outside the transaction. A row the other transaction
  changed or deleted meanwhile raises `TransactionRetryError` — run the transaction again, as for a
  serialization failure on PostgreSQL (`atomic(retries=...)` does it).
- **Locks protect against `select_for_update()` alone.** A write through hare without
  `select_for_update()`, or a statement from outside hare, doesn't wait for them. Lock the rows you are
  about to change in every transaction changing them.
- **One lock per row.** A transaction holds at most 100,000 locks; more raise `QueryError`.
- **A lost session.** When the Keeper session holding the locks ends before the transaction does (the
  network, a Keeper restart), another transaction may have locked the rows since: the next lock and the
  `COMMIT` raise `TransactionManagementError`, and nothing is committed.
- **Outside a transaction** `select_for_update()` raises `QueryError`, as on every database; without
  `keeper_hosts` it raises `UnSupportedError`.

hare speaks ZooKeeper's protocol to Keeper itself — no library is installed for it.
