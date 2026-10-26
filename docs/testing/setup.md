# Test setup

How a test suite gets its database: the pytest plugin hare ships, the `hare_test_context()` fixture
pattern with reusable PostgreSQL databases, `init_memory_database()` for a quick script, the warning about
event-loop switches between tests, and skipping a test the database can't run. Objects for the tests come
from [factories](factories.md).

## <a id="pytest-plugin"></a>The pytest plugin

`pip install hare-orm[pytest]` (pytest and pytest-asyncio) is all it takes: pytest loads hare's plugin by
itself, and the plugin does nothing until a test uses one of its fixtures. It reads the configuration the
pytest settings name — else `HARE_ORM` in the environment or `hare_orm` in pyproject.toml's
`[tool.hare]`, as the `hare` command does:

```toml
[tool.pytest.ini_options]
hare_config = "library.settings.HARE_ORM"
hare_db_url = "postgresql://postgres@localhost/test_{}"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

The configured databases are never opened: every connection of the configuration gets a test database
made from `hare_db_url` — in-memory SQLite without it; a `{}` in the database name is filled with a fresh
name per connection and run — created with the models' tables once for the session and dropped at its end
(`TemporaryDatabases`). The session's event loop runs the fixtures and the tests, hence the two
`asyncio_default_*_loop_scope` settings.

| Fixture | What a test gets |
|---|---|
| `hare_db` | The context, its writes rolled back when the test ends ([`RollbackIsolation`](isolation-and-cleanup.md#rollback-isolation)). A transaction the test opens is a savepoint, and `on_commit()` callbacks don't run. |
| `hare_transactional_db` | The context, its writes really committed — `on_commit()` callbacks run — and every table emptied when the test ends ([`truncate_all_models()`](isolation-and-cleanup.md#truncate-all-models)). For code that needs a real commit. |
| `hare_database` | The session's context, shared by every test — nothing undone. |
| `hare_assert_query_count` | [`assert_query_count()`](assertions.md#assert-num-queries): `async with hare_assert_query_count(1): ...`. |
| `hare_capture_on_commit` | `RollbackIsolation.capture_on_commit()` of the test's `hare_db`. |
| `hare_rollback_isolation` | The `RollbackIsolation` of the test's `hare_db`. |

```python
@pytest.mark.asyncio
async def test_new_reader(hare_db, hare_assert_query_count):
    async with hare_assert_query_count(1):
        await Reader.objects.create(name="ann")


@pytest.mark.hare_requires(supports_transactions=True)
@pytest.mark.asyncio
async def test_rollback(hare_db): ...
```

The marker `hare_requires(connection_alias=None, **conditions)` skips a test whose connection doesn't
meet the conditions — the same ones as [`requires_features()`](#requires-features); a test marked so uses
one of the fixtures `hare_db`, `hare_transactional_db` or `hare_database`.

| Option | Meaning |
|---|---|
| `--hare-db-url URL` | The URL the test databases are made from, over the ini's `hare_db_url`. |
| `--hare-reuse-db` | Lease the PostgreSQL test databases from [`ReusableTestDatabases`](../dialects/postgresql/testing.md#reuse-databases) instead of creating and dropping them. |

Without pytest-asyncio a test using a fixture fails with what to install.

## <a id="hare-test-context"></a>A context per test: `hare_test_context()`

```python
def hare_test_context(
    modules: list[str],
    db_url: str = "sqlite+aiosqlite://:memory:",
    app_label: str = "models",
    *,
    connection_label: str | None = None,
    use_timezone: bool = True,
    timezone: str = "UTC",
    routers: list[str | type] | None = None,
    reuse_databases: bool | None = None,
) -> AbstractAsyncContextManager[HareContext]: ...
```

The recommended way to set a database up for a test — each call is a fully isolated `HareContext` (own
connections, own app registry, own database), so tests running in parallel (`pytest-xdist`) never step on
each other:

```python
import pytest_asyncio
from hare.contrib.test import hare_test_context


@pytest_asyncio.fixture
async def database():
    async with hare_test_context(["library.models"]) as context:
        yield context


async def test_create_author(database):
    author = await Author.objects.create(name="Ursula K. Le Guin")
    assert author.id is not None
```

For a file-backed SQLite `db_url`, that isolation needs either a unique path per call (pytest's
`tmp_path` fixture is already unique per worker) or the `{}` placeholder
(`db_url="sqlite+aiosqlite://test-{}.sqlite"`, a file in the current directory), which is filled with
a random UUID on each call. SQLite serializes writers even across separate connections to one file, so
two xdist workers using
the same fixed path at once would get `database is locked`; under `-n` (`PYTEST_XDIST_WORKER` set) hare
therefore appends the worker id to a fixed SQLite path. Outside `-n` the path is used exactly as given.

Like `CREATE DATABASE` on PostgreSQL, creating the database refuses one that already exists: an SQLite
file that is already there raises `OperationalError` before anything touches it, so a
`hare_test_context()` (or `Hare.init(_create_db=True)`) pointed at a real database file never deletes it
on exit. `:memory:` is unaffected.

The database is dropped on exit even when schema generation fails while entering the block — a model
whose DDL the database rejects doesn't leave a database behind.

With `reuse_databases=True` a PostgreSQL test database is leased from a pool instead of created and
dropped — see [PostgreSQL: testing](../dialects/postgresql/testing.md#reuse-databases).

## <a id="init-memory-database"></a>Quick scripts: `init_memory_database()`

```python
from hare import Hare, fields
from hare.contrib.test import init_memory_database
from hare.models import Model


class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.TextField()


@init_memory_database
async def run():
    obj = await Book.objects.create(title="Moby Dick")
    assert obj.id == 1


if __name__ == "__main__":
    Hare.run_async(run())
```

Spins up an in-memory SQLite database and generates schemas before calling the wrapped function — meant
for throwaway scripts/examples, not test suites (use `hare_test_context()` for those). Takes an optional
`models=[...]` (defaults to `["__main__"]`) when your models don't live in the calling module. Its
connection URL is `hare.contrib.test.MEMORY_DATABASE` (`sqlite+aiosqlite://:memory:`).

## <a id="hare-loop-switch-warning"></a>Event-loop switches between tests: `HareLoopSwitchWarning`

Emitted when a connection pool created on one event loop is used from a different one — hare
transparently opens a fresh connection for the new loop, but this is worth knowing about. In test
environments (function-scoped fixtures, a Starlette `TestClient`, ...) this is expected, and
`hare_test_context()` already suppresses it for you automatically. Filter it manually if you're not going
through `hare_test_context()`:

```python
import warnings
from hare.warnings import HareLoopSwitchWarning

warnings.filterwarnings("ignore", category=HareLoopSwitchWarning)
```

The replaced connection is closed on the event loop that opened it, while that loop still runs (a loop of
another thread); when that loop is already closed, its connections are aborted — the server ends their
sessions. Either way none stays open on the server.

Outside tests, this warning usually indicates a real bug — investigate why the event loop changed between
connection creation and use.

## <a id="requires-features"></a>Skipping by database capability: `requires_features()`

```python
def requires_features(
    connection_alias: str | None = None,
    **conditions: Any,
) -> Callable[[TestItem], TestItem]: ...
```

```python
@requires_features(supports_transactions=True)
async def test_rolls_back(database): ...


@requires_features(dialect="sqlite")
async def test_sqlite_sql(database): ...
```

Each condition names a field of the connection's `Features` (`supports_transactions`,
`supports_returning`, ...), else an attribute of its dialect's `SqlLiterals` (`identifier_quote_char`,
...), else of its dialect (`minimum_server_version`, ...), or `dialect` for the
dialect's name — see [Dialects and features](../dialects/dialects-and-features.md). A test that needs a
capability names the capability, so it runs on every dialect that has it; `dialect=` is for a test of one
dialect's own SQL or types.

Checks each `condition` against the current context's `connection_alias` connection — its default
connection when `None` (the single one `hare_test_context()` creates; with several and none named
`"default"`, the first configured one) — and raises `unittest.SkipTest` if any don't match — works as a
decorator on a single test function or on a whole test class (applies to every `test_*` method).
