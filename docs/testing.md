# Testing (`hare.contrib.test`)

## `hare_test_context()` — the pytest fixture pattern {: #hare-test-context }

```python
async def hare_test_context(
    modules: list[str],
    db_url: str = "sqlite://:memory:",
    app_label: str = "models",
    *,
    connection_label: str | None = None,
    use_tz: bool = True,
    timezone: str = DEFAULT_TIMEZONE,
    routers: list[str | type] | None = None,
    _create_db: bool = True,
    _generate_schemas: bool = True,
    _drop_db_on_exit: bool = True,
    reuse_databases: bool | None = None,
) -> AsyncGenerator[HareContext]
```

The recommended way to set a database up for a test — each call is a fully isolated `HareContext`
(own connections, own app registry, own database), so tests running in parallel (`pytest-xdist`)
never step on each other:

```python
import pytest_asyncio
from hare.contrib.test import hare_test_context


@pytest_asyncio.fixture
async def db():
    async with hare_test_context(["myapp.models"]) as ctx:
        yield ctx


async def test_create_author(db):
    author = await Author.objects.create(name="Ursula K. Le Guin")
    assert author.id is not None
```

For a file-backed SQLite `db_url`, that isolation needs either a unique path per call (pytest's
`tmp_path` fixture is already unique per worker) or the `{}` placeholder
(`db_url="sqlite:///some/dir/test-{}.sqlite"`), which is filled with a random UUID on each call.
SQLite serializes writers even across separate connections to one file, so two xdist workers using
the same fixed path at once would get `database is locked`; under `-n` (`PYTEST_XDIST_WORKER` set)
hare therefore appends the worker id to a fixed SQLite path. Outside `-n` the path is used exactly
as given.

Like `CREATE DATABASE` on Postgres, creating the database refuses one that already exists: a sqlite
file that is already there raises `OperationalError` before anything touches it, so a
`hare_test_context()` (or `Hare.init(_create_db=True)`) pointed at a real database file never
deletes it on exit. `:memory:` is unaffected.

The database is dropped on exit even when schema generation fails while entering the block - a
model whose DDL the database rejects doesn't leave a database behind.

### Reusable PostgreSQL test databases (`reuse_databases`) {: #reuse-databases }

Every `DROP DATABASE` makes Postgres request a checkpoint, and on a server with `fsync` on that
costs seconds under a parallel test run - more than creating and filling the database. With
`reuse_databases=True`, a Postgres `db_url` whose database name contains the `{}` placeholder takes
a database from a per-process pool (`ReusableTestDatabases`) instead of a new random name:

- the placeholder is filled with the id of a pool slot - 32 hex characters derived from the slot's
  number (and the `pytest-xdist` worker), so `test_{}` gives the same names on every run;
- on entering, the slot's database is created when it doesn't exist yet, reset when it is left over
  from an earlier run, and used as is when this process has already reset it;
- on exit, instead of `DROP DATABASE`, the database is reset and the slot goes back to the pool -
  also when the block raises or the reset itself fails (the database is then reset before its next
  use);
- a context entered while every slot is taken (nested contexts, several connections in one config)
  gets a new slot, so the pool grows to the largest number of databases used at the same time.

The reset brings the database back to the state of a freshly created one without dropping it: it
ends the other sessions connected to it (`pg_terminate_backend`), rolls back its prepared
transactions, drops every schema except the system ones (`DROP SCHEMA ... CASCADE`, which also drops
the extensions installed into them - `vector`, `postgis`, `citext`, `btree_gist`, ...), recreates
`public` with the owner, privileges and comment a new database gets, and clears database-level
settings (`ALTER DATABASE ... RESET ALL`, and `ALTER ROLE ... IN DATABASE ... RESET ALL` for every
role that has some). None of these statements requests a checkpoint. Objects that don't live in a
schema - large objects, event triggers, publications - are not removed.

`reuse_databases=None` (the default) reads the `HARE_TEST_REUSE_DATABASES` environment variable:
`1`/`true` turns the pool on, `0`/`false` or an unset variable leaves it off; any other value raises
`ConfigurationError`. An explicit `True`/`False` argument wins over the variable. SQLite URLs, and
Postgres URLs without `{}`, are never pooled.

```bash
HARE_TEST_REUSE_DATABASES=1 pytest -n 8
```

Pooled databases are not dropped when the process ends - the next run takes them over, since
`test_{}` gives the same names again. A test that checks the database lifecycle itself -
`CREATE DATABASE` refusing an existing name, `DROP DATABASE` with an active connection - should pass
`reuse_databases=False`.

## `HareLoopSwitchWarning` — event-loop switches between tests {: #hare-loop-switch-warning }

Emitted when a connection pool created on one event loop is used from a different one — hare
transparently opens a fresh connection for the new loop, but this is worth knowing about. In test
environments (function-scoped fixtures, a Starlette `TestClient`, ...) this is expected, and
`hare_test_context()` already suppresses it for you automatically. Filter it manually if you're not
going through `hare_test_context()`:

```python
import warnings
from hare.warnings import HareLoopSwitchWarning

warnings.filterwarnings("ignore", category=HareLoopSwitchWarning)
```

Outside tests, this warning usually indicates a real bug — investigate why the event loop changed
between connection creation and use.

## `requires_features()` — skip by database capability {: #requires-features }

```python
def requires_features(connection_name: str | None = None, **conditions: Any) -> Callable[[FT], FT]
```

```python
@requires_features(supports_transactions=True)
async def test_rolls_back(db): ...


@requires_features(dialect="sqlite")
async def test_sqlite_sql(db): ...
```

Each condition names a field of the connection's `Features` (`supports_transactions`,
`supports_returning`, ...), else an attribute of its dialect (`supports_distinct_on`,
`supports_partial_indexes`, ...), or `dialect` for the dialect's name - see [Dialects and features](dialects/dialects-and-features.md). A test
that needs a capability names the capability, so it runs on every dialect that has it; `dialect=`
is for a test of one dialect's own SQL or types.

Checks each `condition` against the current context's `connection_name` connection — its default connection when `None` (the single one `hare_test_context()` creates; with
several and none named `"default"`, the first configured one) — and raises
`unittest.SkipTest` if any don't match — works as a decorator on a single test function or on a
whole test class (applies to every `test_*` method).

## `assert_num_queries()` / `capture_queries()` — catch N+1 regressions {: #assert-num-queries }

```python
async def capture_queries(using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]

async def assert_num_queries(expected: int, *, using: str | DatabaseClient | None = None) -> AsyncGenerator[QueryCounter]
```

```python
async with assert_num_queries(1):
    await Event.objects.all().select_related("tournament")
```

`assert_num_queries` fails with an `AssertionError` listing every captured query's SQL text if the
block runs anything other than exactly `expected` queries — the direct way to pin down a missing
`select_related()`/`prefetch_related()` as a test failure instead of discovering it in production.
`capture_queries` is the lower-level primitive it's built on, for when you want the count/query list
without asserting on it:

```python
async with capture_queries() as counter:
    await Event.objects.all().select_related("tournament")
assert counter.count == 1
```

`QueryCounter` (`count: int`, `queries: list[str]`) updates live as the block runs, not only once it
exits. Every statement counts exactly once - model loads, `values()`/`values_list()`,
`aggregate()`, `count()`/`exists()`, writes, raw SQL, each `stream()` and each
`bulk_create(use_copy=True)` batch (recorded as `COPY <table> (<columns>) FROM STDIN`) - even where
one client method internally delegates to another.

## `init_memory_sqlite()` — quick scripts {: #init-memory-sqlite }

```python
from hare import fields, models, run_async
from hare.contrib.test import init_memory_sqlite


class MyModel(models.Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()


@init_memory_sqlite
async def run():
    obj = await MyModel.objects.create(name="")
    assert obj.id == 1


if __name__ == "__main__":
    run_async(run())
```

Spins up an in-memory SQLite database and generates schemas before calling the wrapped function —
meant for throwaway scripts/examples, not test suites (use `hare_test_context()` for those). Takes
an optional `models=[...]` (defaults to `["__main__"]`) when your models don't live in the calling
module.

## `truncate_all_models()` — fast cleanup between tests {: #truncate-all-models }

```python
async def truncate_all_models(context: HareContext | None = None) -> None
```

Deletes every row from every registered model's table in `context` (the current one when `None`).
Each connection's dialect empties its tables (`Dialect.clear_tables()`), given in foreign-key order -
a table before the tables it references - and qualified with `Meta.schema` where the dialect has
schemas: PostgreSQL with one `TRUNCATE ... CASCADE` statement over the tables that hold rows (it
asks which do first - an empty table costs the server as much to truncate as a filled one), SQLite
with a `DELETE` per table, sent as one script, while its foreign keys are off. Auto-created `ManyToManyField` through tables are cleared too. A
`Meta.managed = False` model is skipped - its table (or view) isn't hare-orm's to empty - and so is a
model swapped for another one by its `swappable` setting, which has no table. Raises
`ConfigurationError` if no apps are loaded.

## Value matchers (`hare.contrib.test.condition`) {: #value-matchers }

Not re-exported from the top-level `hare.contrib.test` package — import from the submodule
directly:

```python
from hare.contrib.test.condition import In, NotEQ, NotIn
```

Usable on the right-hand side of an `==` comparison, e.g. inside a dict compared against real data
in an assertion:

| Class | Matches |
|---|---|
| `NotEQ(value)` | Anything not equal to `value`. |
| `In(*values)` | Anything that is one of `values`. |
| `NotIn(*values)` | Anything that is none of `values`. |

```python
assert response.json() == {"id": In(*known_ids), "status": NotEQ("deleted")}
```
