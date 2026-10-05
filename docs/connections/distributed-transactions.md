# Distributed transactions

`Transactions.distributed()` writes to several PostgreSQL databases as one transaction, by two-phase
commit: either every database keeps its write or none does, a crash in the middle included.

```python
class Transactions:
    @staticmethod
    def distributed(
        coordinator: str, participants: Sequence[str]
    ) -> AbstractAsyncContextManager[DistributedTransactions]: ...
```

A loan written to the library's database, the copy taken off the shelf in the inventory database and the
reader's deposit in the billing database — all three or none:

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
The protocol is PostgreSQL's own `PREPARE TRANSACTION`/`COMMIT PREPARED`/`ROLLBACK PREPARED` — where
[`on_rollback()`](transactions.md#on-rollback) only runs a compensating action, a distributed transaction
never leaves one database written and another not.

## <a id="how-the-commit-runs"></a>How the commit runs

On a clean exit from the block:

1. Every participant is `PREPARE TRANSACTION`'d — if any fails, everything (including the coordinator,
   never touched yet) rolls back and the original exception propagates.
2. The coordinator does an ordinary `COMMIT` of a row into its own `hare_distributed_decisions` table
   (created automatically on first use). **This commit is the single atomic instant the whole distributed
   transaction durably happens** — no external coordinator or WAL needed.
3. Every participant is told to `COMMIT PREPARED`. If one fails here, the distributed transaction already
   committed in step 2 — this raises `DistributedTransactionPartiallyCommittedError` rather than implying
   the whole thing failed; run `hare distributed-recover` to finish delivering the participant(s) named
   on the exception.

If the coordinator's own `COMMIT` in step 2 fails with an unknown outcome (the connection dropped after
the server got it), `DistributedTransactionCommitAmbiguousError` is raised — every participant still
holds its prepared transaction, and `hare distributed-recover` settles them by the decision row.

An exception raised inside the block itself (before step 1) rolls everything back as normal. An empty
`participants`, an alias given twice (the coordinator among the participants too) and an alias already
inside a transaction raise `QueryError` before anything opens.

A cancellation that arrives while a participant's `PREPARE TRANSACTION` is in flight waits for it to
finish, so a participant that did prepare is always ended with `ROLLBACK PREPARED` instead of being left
behind with no decision row.

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
`COMMIT PREPARED`. A prepared transaction with no matching decision row anywhere never actually committed
— presumed abort, `ROLLBACK PREPARED`. `--older-than` (seconds, default 300) skips anything young enough
to still be mid-flight in a live call; `0` skips nothing, whatever the server clock says. Safe to re-run.
