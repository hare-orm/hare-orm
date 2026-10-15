# Isolation and cleanup

Two ways to start every test from the same data: roll back a transaction around each test, or empty the
tables after it.

## <a id="rollback-isolation"></a>A rolled-back transaction per test: `RollbackIsolation`

```python
class RollbackIsolation:
    def __init__(self, context: HareContext | None = None) -> None: ...

    async def __aenter__(self) -> HareContext: ...

    def capture_on_commit(
        self, *, execute: bool = False
    ) -> AbstractAsyncContextManager[list[Callable[[], Any]]]: ...
```

The fastest isolation between tests: a database created once per module, each test inside a transaction
that is rolled back when it ends.

```python
import pytest_asyncio
from hare.contrib.test import RollbackIsolation, hare_test_context

@pytest_asyncio.fixture(scope="module")
async def database():
    async with hare_test_context(["library.models"]) as context:
        yield context

@pytest_asyncio.fixture
async def isolated_context(database):
    async with RollbackIsolation(database) as context:
        yield context
```

Inside the block every connection of the context (the current one when `context` is None) runs in a
transaction of its own, and the context is the current one; when the block ends, each transaction is
rolled back. A connection whose database has no transactions has its models' tables emptied instead
(`truncate_all_models(connections=...)`). A `Transactions.atomic()` block the test opens is a savepoint
of the isolating transaction, so the test sees what it wrote and a failing block rolls back as usual. A
database without savepoints (ClickHouse, with `transactions=true`) joins the block to the isolating
transaction instead: a failing block's writes and its `on_commit()` callbacks stay until the isolation
ends — see [ClickHouse transactions](../dialects/clickhouse/transactions-and-locks.md).

The isolating transaction never commits: committing it by hand (`await connection.commit()`) raises
`TransactionManagementError`, and `on_commit()` callbacks registered in the test never run by themselves.
`capture_on_commit()` hands them out:

```python
isolation = RollbackIsolation(database)
async with isolation:
    async with isolation.capture_on_commit(execute=True) as callbacks:
        await lend_book()            # calls Transactions.on_commit(send_due_date_reminder)
    assert len(callbacks) == 1       # send_due_date_reminder ran when the block exited
```

The list gets the callbacks registered in the block, in order, when it exits; with `execute=True` they
run then too (an `async def` one is awaited), and so do the callbacks they register in turn. A callback
registered in a savepoint that rolled back isn't among them, nor one hare registers for its own
bookkeeping of a write (it still runs with `execute=True`).

## <a id="truncate-all-models"></a>Emptying the tables: `truncate_all_models()`

```python
async def truncate_all_models(
    context: HareContext | None = None,
    *,
    connections: Collection[DatabaseClient] | None = None,
) -> None: ...
```

Deletes every row from every registered model's table in `context` (the current one when `None`) — with
`connections`, only of the models those connections hold, each on its own connection. Each connection's
dialect empties its tables (its schema editor's `clear_tables()`), given in foreign-key order — a table
before the tables it references — and qualified with `Meta.schema` where the dialect has schemas:

| Database | Behavior |
|---|---|
| PostgreSQL | One `TRUNCATE ... CASCADE` statement over the tables that hold rows — it asks which do first: an empty table costs the server as much to truncate as a filled one |
| SQLite | A `DELETE` per table, sent as one script, while its foreign keys are off |
| ClickHouse | A `TRUNCATE TABLE` per table |

Auto-created `ManyToManyField` through tables are cleared too. A `Meta.managed = False` model is skipped
— its table (or view) isn't hare's to empty — and so is a model swapped for another one by its
`swappable` setting, which has no table. Raises `ConfigurationError` if no apps are loaded.

`topological_sort_models(models)` gives that order for any list of models — a model before the models it
references, the order their rows can be deleted in.
