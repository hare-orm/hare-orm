# Distributed transactions

`Transactions.distributed()` coordinates writes to several PostgreSQL databases using two-phase
commit. A durable coordinator decision determines whether prepared participants must commit or roll
back. After a failure, recovery completes that decision when the databases are reachable again.
Commits become visible separately in each database; there is no simultaneous cross-database snapshot.

```python
class Transactions:
    @staticmethod
    def distributed(
        coordinator: str, participants: Sequence[str]
    ) -> AbstractAsyncContextManager[DistributedTransactions]: ...
```

A loan written to the library's database, the copy taken off the shelf in the inventory database and the
reader's deposit in the billing database share one commit decision:

```python
from hare.query.expressions import F
from hare import Transactions

async with Transactions.distributed(coordinator="library", participants=["inventory", "billing"]) as distributed:
    await Loan.objects.using(distributed.coordinator).create(book_id=book.pk, reader_id=reader.pk)
    await Inventory.objects.using(distributed["inventory"]).filter(book_id=book.pk).update(
        available=F("available") - 1
    )
    await Deposit.objects.using(distributed["billing"]).create(reader_id=reader.pk, amount=5)
```

`distributed.coordinator` and `distributed[alias]` give the already-open client of the coordinator and of
each participant, to pass as `using=`; `distributed[coordinator]` is the coordinator's client as well.
The protocol uses PostgreSQL's `PREPARE TRANSACTION`/`COMMIT PREPARED`/`ROLLBACK PREPARED`.
Prepared participants retain their transactions until commit or rollback, including after a process
crash. [`on_rollback()`](transactions.md#on-rollback) instead runs an application compensating action.
A participant may remain prepared while another database's commit is already visible; recovery must
finish the pending participants.

## <a id="how-the-commit-runs"></a>How the commit runs

On a clean exit from the block:

1. Every participant is `PREPARE TRANSACTION`'d. If any fails, cleanup attempts to roll back all
   participants and the coordinator, and the original exception propagates. A failed or uncertain
   prepared rollback needs recovery.
2. The coordinator does an ordinary `COMMIT` of a row into its own `hare_distributed_decisions` table
   (created automatically on first use), together with the block's coordinator writes.
   **This makes the commit decision durable**; participants must subsequently commit. It does not make
   their writes visible at the same instant. No separate external coordinator service is required.
3. Every participant is told to `COMMIT PREPARED`. If one fails here, the commit decision is already
   durable from step 2 — this raises `DistributedTransactionPartiallyCommittedError` rather than implying
   the whole thing failed; run `hare distributed-recover` to finish delivering the participant(s) named
   on the exception.

If the coordinator's own `COMMIT` in step 2 fails with an unknown outcome (the connection dropped after
the server got it), `DistributedTransactionCommitAmbiguousError` is raised — every participant still
holds its prepared transaction, and `hare distributed-recover` settles them by the decision row. A
`COMMIT` that surely didn't land — the database rejected it, or the connection broke before it was sent
— recorded no decision either: cleanup attempts `ROLLBACK PREPARED` for each participant and raises the
`COMMIT`'s error. A participant whose rollback cannot be confirmed remains recovery work.

An exception raised inside the block itself (before step 1) rolls everything back as normal. An empty
`participants`, an alias given twice (the coordinator among the participants too) and an alias already
inside a transaction raise `QueryError` before anything opens. An empty alias (`""` or `None`) is the
default connection, as in `Transactions.atomic()` — one given among the participants names the
coordinator when that is the default connection, which is an alias given twice.

A cancellation during a participant's `PREPARE TRANSACTION` waits for the shielded operation within
a bounded interval. If preparation is confirmed, cleanup attempts `ROLLBACK PREPARED`; for an uncertain
outcome it checks for the prepared transaction using a fresh connection. An expired wait or unavailable
server can leave the outcome unknown or a prepared transaction pending. Run `hare distributed-recover`
after connectivity returns; cancellation does not guarantee an immediate rollback.

## <a id="databases"></a>Databases with two-phase commit

Only PostgreSQL has it: SQLite and ClickHouse have no `PREPARE TRANSACTION`. Every alias passed to
`distributed()` — the coordinator and every participant — must have two-phase commit
(`Features.supports_two_phase_commit`); `UnSupportedError` otherwise.

## <a id="max-prepared-transactions"></a>The server setting `max_prepared_transactions`

Many PostgreSQL installs ship with `max_prepared_transactions = 0`, which disables `PREPARE TRANSACTION`
entirely. `distributed()` raises a `ConfigurationError` naming the alias if it hits this — set it to a
nonzero value in `postgresql.conf` and restart PostgreSQL. When every slot is taken instead ("maximum
number of prepared transactions reached"), the `ConfigurationError` points at `hare distributed-recover`
and `pg_prepared_xacts` — leftover prepared transactions hold the slots. Any other `PREPARE TRANSACTION`
error is raised as is.

## <a id="recovering-stuck-prepared-transactions"></a>Recovering stuck prepared transactions

If a participant's `COMMIT PREPARED` fails synchronously (step 3 above) or the process crashes
mid-protocol, `hare distributed-recover` finds and finishes it:

```console
$ hare distributed-recover --coordinator library
$ hare distributed-recover --coordinator library --finish
$ hare distributed-recover --coordinator library --finish --older-than 60
```

Without `--finish` it only reports what it found. A decision row with no `resolved_at` means the
distributed transaction already committed — any participant still holding that prepared transaction needs
`COMMIT PREPARED`. A prepared transaction belonging to this coordinator with no matching decision row
is treated as presumed abort: `ROLLBACK PREPARED`. `--older-than` (nonnegative integer seconds,
default 300) skips anything young enough
to still be mid-flight in a live call; `0` skips nothing, whatever the server clock says. A young commit
decision still protects an older prepared participant from rollback: recovery waits until the decision
reaches the cutoff before proposing its commit. Safe to re-run.

The CLI uses the same recovery service available to applications:

```python
from hare.transactions import DistributedRecovery

report = await DistributedRecovery.run("library", older_than_seconds=300)
report = await DistributedRecovery.run("library", finish=True, older_than_seconds=300)
```

`entries` contains eligible prepared transactions. With `finish=True`, `attempts` records each
entry and its `error` (or `None`), `completed_xids` lists completed decision records, and
`decision_errors` contains `(xid, error)` pairs for failed log updates. A failed participant keeps
its entire decision pending. A later run rechecks which participants still need work; it also
completes a stale decision whose participants already finished before a crash. Run finishing
recovery outside a user transaction. Cancellation stops further actions, but an action already
sent to the database may have succeeded.

Finishing a prepared transaction also invalidates driver schema metadata, including during recovery.
When a temporary connection finishes it, the original pool is invalidated before that connection
closes; its next ORM statement refreshes metadata for any schema changes made by the transaction.
