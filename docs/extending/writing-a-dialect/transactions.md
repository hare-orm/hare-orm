# Writing a dialect: transactions

Step 7 of [Writing a dialect](index.md): transactions, savepoints and concurrency.

## <a id="transactions"></a>7. Transactions and concurrency

`Features.supports_transactions` decides whether transactions exist at all (see
[Features](driver-and-client.md#features)).

The transaction itself is one state machine, written once in `TransactionClient`
(`hare.dialects.base.client`): `begin()`, `commit()`, `rollback()`, `savepoint()`, `release_savepoint()`
and `savepoint_rollback()`, nesting, the `on_commit()`/`on_rollback()` callbacks, shielding a `COMMIT`
from task cancellation, refusing a statement after the transaction ended, and reporting
`TransactionEvent`s. A driver's transaction client only supplies the primitives it calls:

| Primitive | Does |
|---|---|
| `_driver_begin()`, `_driver_commit()`, `_driver_rollback()` | Start the transaction on the connection; send its `COMMIT`; send its `ROLLBACK`. |
| `_driver_savepoint(name)`, `_driver_release_savepoint(name)`, `_driver_rollback_to_savepoint(name)` | Open, release and roll back to a savepoint. |
| `_get_new_savepoint_name()` | A savepoint name not used on this connection yet. |
| `_is_connection_lost(error)` | Whether a driver error of a `COMMIT`/`ROLLBACK` means the connection is gone — the server rolls such a transaction back. `False` by default. |
| `_is_commit_outcome_unknown(error)` | Whether a lost connection leaves it unknown if a `COMMIT` in flight landed. |
| `_is_commit_rejection(error)` | Whether the database answered the `COMMIT` with an error — the transaction is over. |
| `_is_transaction_finished_error(error)` | Whether the driver reports that an earlier, interrupted `COMMIT`/`ROLLBACK` already ended the transaction. |
| `_check_commit_allowed()`, `_check_savepoint_allowed()`, `_before_top_level_end(event)`, `_after_rejected_commit(error)` | Optional hooks around the state machine's steps — SQLite refuses a `COMMIT` of an aborted transaction and switches a read-only one back before it ends. |
| `_take_transaction_resources()`, `_give_back_transaction_resources()` | Take what a top-level transaction holds for its whole life before `BEGIN` — a pooled connection, the connection's lock — and give it back as soon as the `COMMIT`/`ROLLBACK` landed, before the callbacks run. Nothing by default. |
| `_undo_failed_begin()` | End a transaction whose `BEGIN` raised but may have landed. Nothing by default. |
| `_end_unfinished_transaction()` | End what a top-level transaction left open on the driver once its block exits. Nothing by default. |

The six `_driver_*` methods and `_get_new_savepoint_name()` are abstract; the rest have defaults. A
nested transaction is a client of the same class sharing the connection — the base class makes it.

A client applying a transaction's `lock_timeout` itself — SQLite's busy timeout, the waits for
ClickHouse's row locks — sets `enforces_lock_timeout_itself = True`, and a database without a statement
for it then takes the option.

The dialect's `TransactionStatements` (`build_transactions()`) set a transaction up:

| Member | Meaning |
|---|---|
| `get_isolation_level(requested)` | The level a transaction asking for one runs at — the weakest of `Features.isolation_levels` at least as strong. |
| `get_isolation_level_sql(level)` | The statement setting it; None where every transaction runs at that level. |
| `get_read_only_sql()` | The statement making the transaction read-only (ISO `SET TRANSACTION READ ONLY` by default). |
| `get_begin_sql()`, `get_commit_sql()`, `get_rollback_sql()` | The statements opening, committing and rolling back a transaction (ISO `START TRANSACTION`, `COMMIT`, `ROLLBACK` by default) — written around the SQL `sqlmigrate` prints for an atomic migration, and sent where hare ends a transaction past its own callbacks. |
| `get_statement_timeout_sql(milliseconds)`, `get_lock_timeout_sql(milliseconds)` | Transaction-local statement and lock timeouts; None where the database has none. |

The schema editor's `TableLocks` part (`table_locks_class`): `get_lock_table_sql()` locks a table for the
transaction and `get_migration_lock_sql()` serializes concurrent `migrate` runs;
`build_two_phase_commit()` gives `Transactions.distributed()` its statements.
