import warnings

import pytest

from hare.dialects.base.results import StatementResult
from hare.dialects.postgresql.query import PostgresqlQuery
from hare.dialects.registry import DialectRegistry
from hare.dialects.sqlite.query import SqliteQuery
from hare.exceptions import OperationalError
from hare.migrations.loading.recorder import MigrationRecorder


class FakeConnection:
    def __init__(self, dialect: str) -> None:
        self.dialect = DialectRegistry.get_dialect(dialect)
        self.query_class = {"sqlite": SqliteQuery, "postgresql": PostgresqlQuery}[dialect]
        self.executed_scripts: list[str] = []
        self.inserts: list[tuple[str, list]] = []
        self.queries: list[tuple[str, list | None]] = []

    async def execute_script(self, query: str) -> None:
        self.executed_scripts.append(query)

    async def execute(self, query: str, values: list | None = None, *, returns_rows: bool | None = None):
        if query.lstrip().upper().startswith("INSERT"):
            self.inserts.append((query, values))
            return StatementResult(1, [])
        self.queries.append((query, values))
        return StatementResult(0, [])


@pytest.mark.asyncio
async def test_recorder_uses_parameterized_insert() -> None:
    """Ensure record_applied uses parameterized queries instead of string interpolation.

    Regression guard for query construction in general \u2014 originally reported against
    MariaDB, which rejects ISO 8601 datetime strings with timezone info
    (e.g. '2026-03-04T18:06:51+00:00') when inlined into the SQL text.
    See https://github.com/hare/hare-orm/issues/2132
    """
    from datetime import datetime

    connection = FakeConnection("sqlite")
    recorder = MigrationRecorder(connection)

    await recorder.record_applied("models", "0001_init")

    assert len(connection.inserts) == 1
    query, values = connection.inserts[0]
    # Query must use placeholders, not inline values
    assert "VALUES (?,?,?)" in query
    assert values[0] == "models"
    assert values[1] == "0001_init"
    assert isinstance(values[2], datetime)


@pytest.mark.asyncio
async def test_recorder_uses_parameterized_delete() -> None:
    """Ensure record_unapplied uses parameterized queries."""
    connection = FakeConnection("sqlite")
    recorder = MigrationRecorder(connection)

    await recorder.record_unapplied("models", "0001_init")

    assert len(connection.queries) == 1
    query, values = connection.queries[0]
    assert 'WHERE "app"=?' in query
    assert '"name"=?' in query
    assert values == ["models", "0001_init"]


@pytest.mark.asyncio
async def test_recorder_postgres_placeholders() -> None:
    connection = FakeConnection("postgresql")
    recorder = MigrationRecorder(connection)

    await recorder.record_applied("app", "0001_initial")

    assert len(connection.inserts) == 1
    query, values = connection.inserts[0]
    assert "VALUES ($1,$2,$3)" in query


@pytest.mark.asyncio
async def test_recorder_sqlite_placeholders() -> None:
    connection = FakeConnection("sqlite")
    recorder = MigrationRecorder(connection)

    await recorder.record_applied("app", "0001_initial")
    await recorder.record_unapplied("app", "0001_initial")

    insert_query = connection.inserts[0][0]
    assert "VALUES (?,?,?)" in insert_query

    delete_query = connection.queries[0][0]
    assert '"app"=?' in delete_query
    assert '"name"=?' in delete_query


@pytest.mark.asyncio
async def test_applied_migrations_returns_empty_when_table_missing() -> None:
    """Before ensure_schema() has ever run, the table genuinely doesn't exist yet - the
    existence check itself (not an exception from the real SELECT) reports this, and
    applied_migrations() must treat it as "no migrations applied yet", not raise."""

    class MissingTableConnection(FakeConnection):
        async def execute(self, query: str, values: list | None = None):
            # The existence check query (sqlite_master/pg_tables) genuinely finds nothing -
            # the real SELECT ... FROM hare_migrations is never even reached.
            return None, []

    recorder = MigrationRecorder(MissingTableConnection("sqlite"))
    assert await recorder.applied_migrations() == []


@pytest.mark.asyncio
async def test_applied_migrations_does_not_swallow_unrelated_bugs() -> None:
    """A bare ``except Exception`` here would hide a real programming error as
    "no migrations applied yet" - only the table-existence check decides that, never a
    caught exception from the real SELECT."""

    class BuggyConnection(FakeConnection):
        async def execute(self, query: str, values: list | None = None):
            if "sqlite_master" in query:
                return None, [{"name": "hare_migrations"}]
            raise TypeError("boom")

    recorder = MigrationRecorder(BuggyConnection("sqlite"))
    with pytest.raises(TypeError, match="boom"):
        await recorder.applied_migrations()


@pytest.mark.asyncio
async def test_applied_migrations_propagates_a_transient_error_on_an_existing_table() -> None:
    """Regression: applied_migrations() used to catch OperationalError broadly around the real
    SELECT, so a genuinely transient failure on an EXISTING, populated table (a lock timeout,
    a deadlock, a cancelled statement) was silently swallowed as "no migrations applied yet" -
    letting a caller re-attempt migrations that had, in fact, already been applied. The
    table-existence check now runs as its own, separate query first, so this can no longer be
    confused with the table genuinely missing."""

    class LockTimeoutConnection(FakeConnection):
        async def execute(self, query: str, values: list | None = None):
            if "sqlite_master" in query:
                return None, [{"name": "hare_migrations"}]
            raise OperationalError("simulated: database is locked")

    recorder = MigrationRecorder(LockTimeoutConnection("sqlite"))
    with pytest.raises(OperationalError, match="database is locked"):
        await recorder.applied_migrations()


@pytest.mark.asyncio
async def test_applied_migrations_ignores_a_migrations_table_in_another_schema(db_isolated_no_schema) -> None:
    """The existence check must look only where the unqualified SELECT resolves (the connection's
    current schema) - a same-named table in another schema used to count as existing, so the
    SELECT then failed on a connection configured with its own schema."""
    connection = db_isolated_no_schema.db()
    if connection.dialect.name != "postgresql":
        pytest.skip("Schemas are a Postgres-only concept")
    await connection.execute_script(
        "CREATE SCHEMA recorder_other_schema; CREATE TABLE recorder_other_schema.hare_migrations (id INTEGER)"
    )
    recorder = MigrationRecorder(connection)

    assert await recorder._table_exists() is False
    assert await recorder.applied_migrations() == []


def test_recorder_model_emits_no_field_deprecation_warnings() -> None:
    """Regression: the migration recorder must not trip hare's own
    ``pk``/``index`` field deprecation warnings. It builds its bookkeeping
    model internally, so downstream projects can't silence them.
    """
    with warnings.catch_warnings(record=True) as caught:
        # Reset filters so the repo-wide ``ignore:`pk` deprecation`` filter in
        # pyproject.toml doesn't mask a regression here.
        warnings.simplefilter("always")
        MigrationRecorder(FakeConnection("sqlite"))

    messages = [str(w.message) for w in caught if issubclass(w.category, DeprecationWarning)]
    assert not any("`pk` is deprecated" in m for m in messages), messages
    assert not any("`index` is deprecated" in m for m in messages), messages
