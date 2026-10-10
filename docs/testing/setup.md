# Test setup

How a test suite gets its database: the pytest plugin hare ships, the `hare_test_context()`
fixture pattern with reusable PostgreSQL databases, `init_memory_sqlite()` for a quick script, the
warning about event-loop switches between tests, and skipping a test the database can't run.

## <a id="pytest-plugin"></a>The pytest plugin

`pip install hare-orm[pytest]` (pytest and pytest-asyncio) is all it takes: pytest loads hare's plugin
by itself, and the plugin does nothing until a test uses one of its fixtures. It reads the
configuration the pytest settings name — else `HARE_ORM` in the environment or `hare_orm` in
pyproject.toml's `[tool.hare]`, as the `hare` command does:

```toml
[tool.pytest.ini_options]
hare_config = "myapp.settings.HARE_ORM"
hare_db_url = "postgresql://postgres@localhost/test_{}"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

The configured databases are never opened: every connection of the configuration gets a test
database made from `hare_db_url` — in-memory SQLite without it; a `{}` in the database name is filled
with a fresh name per connection and run — created with the models' tables once for the session and
dropped at its end (`TemporaryDatabases`). The session's event loop runs the fixtures and the tests,
hence the two `asyncio_default_*_loop_scope` settings.

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
async def test_signup(hare_db, hare_assert_query_count):
    async with hare_assert_query_count(1):
        await User.objects.create(name="ann")


@pytest.mark.hare_requires(supports_transactions=True)
@pytest.mark.asyncio
async def test_rollback(hare_db): ...
```

The marker `hare_requires(connection_alias=None, **conditions)` skips a test whose connection doesn't
meet the conditions — the same ones as [`requires_features()`](#requires-features); a test marked so
uses one of the fixtures `hare_db`, `hare_transactional_db` or `hare_database`.

| Option | Meaning |
|---|---|
| `--hare-db-url URL` | The URL the test databases are made from, over the ini's `hare_db_url`. |
| `--hare-reuse-db` | Lease the PostgreSQL test databases from [`ReusableTestDatabases`](#reuse-databases) instead of creating and dropping them. |

Without pytest-asyncio a test using a fixture fails with what to install.

## <a id="hare-test-context"></a>`hare_test_context()` — the pytest fixture pattern

```python
async def hare_test_context(
    modules: list[str],
    db_url: str = "sqlite+aiosqlite://:memory:",
    app_label: str = "models",
    *,
    connection_label: str | None = None,
    use_timezone: bool = True,
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
    async with hare_test_context(["myapp.models"]) as context:
        yield context


async def test_create_author(db):
    author = await Author.objects.create(name="Ursula K. Le Guin")
    assert author.id is not None
```

For a file-backed SQLite `db_url`, that isolation needs either a unique path per call (pytest's
`tmp_path` fixture is already unique per worker) or the `{}` placeholder
(`db_url="sqlite+aiosqlite:///some/dir/test-{}.sqlite"`), which is filled with a random UUID on each call.
SQLite serializes writers even across separate connections to one file, so two xdist workers using
the same fixed path at once would get `database is locked`; under `-n` (`PYTEST_XDIST_WORKER` set)
hare therefore appends the worker id to a fixed SQLite path. Outside `-n` the path is used exactly
as given.

Like `CREATE DATABASE` on PostgreSQL, creating the database refuses one that already exists: an SQLite
file that is already there raises `OperationalError` before anything touches it, so a
`hare_test_context()` (or `Hare.init(_create_db=True)`) pointed at a real database file never
deletes it on exit. `:memory:` is unaffected.

The database is dropped on exit even when schema generation fails while entering the block — a
model whose DDL the database rejects doesn't leave a database behind.

### <a id="reuse-databases"></a>Reusable PostgreSQL test databases (`reuse_databases`)

Every `DROP DATABASE` makes PostgreSQL request a checkpoint, and on a server with `fsync` on that
costs seconds under a parallel test run — more than creating and filling the database. With
`reuse_databases=True`, a PostgreSQL `db_url` whose database name contains the `{}` placeholder takes
a database from a per-process pool (`ReusableTestDatabases`) instead of a new random name:

- the placeholder is filled with the id of a pool slot — 32 hex characters derived from the slot's
  number (and the `pytest-xdist` worker), so `test_{}` gives the same names on every run;
- on entering, the slot's database is created when it doesn't exist yet, reset when it is left over
  from an earlier run, and used as is when this process has already reset it;
- on exit, instead of `DROP DATABASE`, the database is reset and the slot goes back to the pool —
  also when the block raises or the reset itself fails (the database is then reset before its next
  use);
- a context entered while every slot is taken (nested contexts, several connections in one config)
  gets a new slot, so the pool grows to the largest number of databases used at the same time.

The reset brings the database back to the state of a freshly created one without dropping it: it
ends the other sessions connected to it (`pg_terminate_backend`), rolls back its prepared
transactions, drops every schema except the system ones (`DROP SCHEMA ... CASCADE`, which also drops
the extensions installed into them — `vector`, `postgis`, `citext`, `btree_gist`, ...), recreates
`public` with the owner, privileges and comment a new database gets, and clears database-level
settings (`ALTER DATABASE ... RESET ALL`, and `ALTER ROLE ... IN DATABASE ... RESET ALL` for every
role that has some). None of these statements requests a checkpoint. Objects that don't live in a
schema — large objects, event triggers, publications — are not removed.

`reuse_databases=None` (the default) reads the `HARE_TEST_REUSE_DATABASES` environment variable:
`1`/`true` turns the pool on, `0`/`false` or an unset variable leaves it off; any other value raises
`ConfigurationError`. An explicit `True`/`False` argument wins over the variable. SQLite URLs, and
PostgreSQL URLs without `{}`, are never pooled.

```bash
HARE_TEST_REUSE_DATABASES=1 pytest -n 8
```

Pooled databases are not dropped when the process ends — the next run takes them over, since
`test_{}` gives the same names again. A test that checks the database lifecycle itself —
`CREATE DATABASE` refusing an existing name, `DROP DATABASE` with an active connection — should pass
`reuse_databases=False`.

## <a id="init-memory-sqlite"></a>`init_memory_sqlite()` — quick scripts

```python
from hare import Hare, fields, models
from hare.contrib.test import init_memory_sqlite


class MyModel(models.Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()


@init_memory_sqlite
async def run():
    obj = await MyModel.objects.create(name="")
    assert obj.id == 1


if __name__ == "__main__":
    Hare.run_async(run())
```

Spins up an in-memory SQLite database and generates schemas before calling the wrapped function —
meant for throwaway scripts/examples, not test suites (use `hare_test_context()` for those). Takes
an optional `models=[...]` (defaults to `["__main__"]`) when your models don't live in the calling
module.
Its connection URL is `hare.contrib.test.MEMORY_SQLITE` (`sqlite+aiosqlite://:memory:`).

## <a id="hare-loop-switch-warning"></a>`HareLoopSwitchWarning` — event-loop switches between tests

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

## <a id="requires-features"></a>`requires_features()` — skip by database capability

```python
def requires_features(connection_alias: str | None = None, **conditions: Any) -> Callable[[FT], FT]
```

```python
@requires_features(supports_transactions=True)
async def test_rolls_back(db): ...


@requires_features(dialect="sqlite")
async def test_sqlite_sql(db): ...
```

Each condition names a field of the connection's `Features` (`supports_transactions`,
`supports_returning`, ...), else an attribute of its dialect's `SqlLiterals`
(`identifier_quote_char`, ...), else of its dialect (`supports_distinct_on`,
`supports_partial_indexes`, ...), or `dialect` for the dialect's name — see [Dialects and features](../dialects/dialects-and-features.md). A test
that needs a capability names the capability, so it runs on every dialect that has it; `dialect=`
is for a test of one dialect's own SQL or types.

Checks each `condition` against the current context's `connection_alias` connection — its default connection when `None` (the single one `hare_test_context()` creates; with
several and none named `"default"`, the first configured one) — and raises
`unittest.SkipTest` if any don't match — works as a decorator on a single test function or on a
whole test class (applies to every `test_*` method).
