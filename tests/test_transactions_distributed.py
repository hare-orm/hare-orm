from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import ReusableTestDatabases
from hare.core.connections import Connections
from hare.core.context import HareContext
from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.dialects.postgresql.constants import POSTGRESQL_DIALECT
from hare.dialects.postgresql.drivers.rust_pg.client import RustPgClient
from hare.exceptions import (
    ConfigurationError,
    DistributedTransactionCommitAmbiguousError,
    DistributedTransactionPartiallyCommittedError,
    QueryError,
    UnSupportedError,
)
from hare.instrumentation.observers import Observers
from hare.instrumentation.transaction_event import TransactionEvent
from hare.models import Model
from hare.transactions.distributed import DistributedCoordinator
from hare.transactions.enums import DistributedTransactionResolution, TransactionEventType
from hare.transactions.transactions import Transactions
from tests.utils.database_under_test import DatabaseUnderTest

COORDINATOR = "coordinator"
PARTICIPANT_A = "participant_a"
PARTICIPANT_B = "participant_b"


class DistributedWidget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()


class DistributedVersionedWidget(Model):
    id = fields.IntField(primary_key=True)
    name = fields.TextField()
    version = fields.IntField(default=0)

    class Meta:
        optimistic_lock_field = "version"


def _distributed_test_pg_url() -> str | None:
    """The Postgres URL these tests provision databases against - None (skip) unless
    HARE_TEST_DB is itself a Postgres URL, mirroring conftest.py's own sqlite-vs-postgres check."""
    raw_db_url = os.getenv("HARE_TEST_DB", "sqlite://:memory:")
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        return None
    return raw_db_url.replace("\\{", "{").replace("\\}", "}")


def _get_database_name_template(template: str) -> str:
    return template if "{}" in template else f"{template}_{{}}"


async def _force_drop_database(conn) -> None:
    """The same shape as DatabaseClient.db_delete(), but with WITH (FORCE) - rust_pg's own
    connection pool (unlike asyncpg's) doesn't always fully release every pooled connection back
    to the OS within close(), so an ordinary DROP DATABASE right after tearing down a distributed()
    test can still see one lingering idle connection and fail with "being accessed by other
    users". FORCE (Postgres 13+) disconnects any remaining session itself rather than needing the
    caller to chase down and close every pooled connection first."""
    await conn.create_connection(with_db=False)
    try:
        await conn.execute_script(f'DROP DATABASE "{conn.database}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def distributed_dbs() -> AsyncGenerator[None]:
    """3 real, freshly-created Postgres databases (coordinator/participant_a/participant_b),
    all sharing the DistributedWidget schema - skips cleanly on sqlite, or when the configured
    Postgres has max_prepared_transactions=0 (PREPARE TRANSACTION disabled there)."""
    template = _distributed_test_pg_url()
    if template is None:
        pytest.skip("Transactions.distributed() needs a Postgres HARE_TEST_DB")

    # Taken from the reusable database pool like any hare_test_context() database - its reset
    # ends lingering sessions the same way DROP DATABASE ... WITH (FORCE) does.
    is_reusing_databases = ReusableTestDatabases.is_enabled()
    with ReusableTestDatabases.track_new_leases() as own_lease_number_by_database_name:
        connections_config = {
            alias: DbUrlConfigGenerator.expand(
                _get_database_name_template(template), testing=True, reuse_databases=is_reusing_databases
            )
            for alias in (COORDINATOR, PARTICIPANT_A, PARTICIPANT_B)
        }
    config = {
        "connections": connections_config,
        "apps": {"models": {"models": [__name__], "default_connection": COORDINATOR}},
    }
    try:
        async with HareContext() as ctx:
            await ctx.init(config=config, _create_db=True)
            try:
                coordinator = Connections.get(COORDINATOR)
                if not coordinator.features.supports_two_phase_commit:
                    pytest.skip("Coordinator connection doesn't support distributed transactions")
                _, rows = await coordinator.execute("SHOW max_prepared_transactions")
                if int(rows[0]["max_prepared_transactions"]) == 0:
                    pytest.skip(
                        "Postgres has max_prepared_transactions=0 - set it >0 in postgresql.conf "
                        "and restart Postgres to run these tests"
                    )
                await ctx.generate_schemas(safe=True)
                schema_sql = coordinator.get_schema_sql(safe=True)
                for alias in (PARTICIPANT_A, PARTICIPANT_B):
                    await Connections.get(alias).execute_script(schema_sql)
                yield
            finally:
                await ctx.connections.close_all(discard=False)
                for conn in ctx.connections.all():
                    if is_reusing_databases:
                        await conn.db_delete()
                    else:
                        await _force_drop_database(conn)
    finally:
        ReusableTestDatabases.release_leases(own_lease_number_by_database_name)


async def _widget_count(alias: str) -> int:
    _, rows = await Connections.get(alias).execute("SELECT count(*) AS n FROM distributedwidget")
    return int(rows[0]["n"])


@pytest.mark.asyncio
async def test_distributed_commit_two_participants(distributed_dbs) -> None:
    async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]) as txns:
        await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
        await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
        await DistributedWidget.objects.using(txns[PARTICIPANT_B]).create(name="on-b")

    assert await _widget_count(COORDINATOR) == 1
    assert await _widget_count(PARTICIPANT_A) == 1
    assert await _widget_count(PARTICIPANT_B) == 1


@pytest.mark.asyncio
async def test_distributed_commit_and_rollback_escape_a_quote_in_the_gid(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_participant_gid = DistributedCoordinator._participant_gid
    monkeypatch.setattr(
        DistributedCoordinator,
        "_participant_gid",
        staticmethod(lambda xid, alias: f"{real_participant_gid(xid, alias)}'; DROP TABLE distributedwidget; --"),
    )
    async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
        await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
        await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")

    assert await _widget_count(COORDINATOR) == 1
    assert await _widget_count(PARTICIPANT_A) == 1

    # A PREPARE failure on participant_b (its GID already taken) makes distributed() issue
    # ROLLBACK PREPARED for the already-prepared participant_a, with the quoted GID.
    fixed_uuid_hex = uuid.uuid4().hex
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(fixed_uuid_hex))
    fixed_xid = DistributedCoordinator._make_xid(COORDINATOR)
    blocker = Connections.current().create_independent(PARTICIPANT_B)
    blocker_transaction = blocker._in_transaction()
    try:
        blocker_client = await blocker_transaction.__aenter__()
        await blocker_client._prepare_transaction(DistributedCoordinator._participant_gid(fixed_xid, PARTICIPANT_B))
    finally:
        await blocker_transaction.__aexit__(None, None, None)
    try:
        with pytest.raises(Exception):  # noqa: PT011 - the exact translated exception type is driver-specific
            async with Transactions.distributed(
                coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
            ) as txns:
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="rolled-back")
    finally:
        await DistributedCoordinator.rollback_prepared(blocker, PARTICIPANT_B, fixed_xid)
        await blocker.close()

    assert await _widget_count(PARTICIPANT_A) == 1
    for alias in (PARTICIPANT_A, PARTICIPANT_B):
        _, prepared_rows = await Connections.get(alias).execute(
            "SELECT count(*) AS n FROM pg_prepared_xacts WHERE database = current_database()"
        )
        assert int(prepared_rows[0]["n"]) == 0


@pytest.mark.asyncio
async def test_distributed_commit_one_participant(distributed_dbs) -> None:
    async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
        await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
        await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")

    assert await _widget_count(COORDINATOR) == 1
    assert await _widget_count(PARTICIPANT_A) == 1


@pytest.mark.asyncio
async def test_distributed_rejects_nesting_inside_an_ambient_transaction_on_the_coordinator(distributed_dbs) -> None:
    """distributed() used to silently create a NESTED (savepoint-based) transaction when the
    coordinator alias was already inside an open Transactions.atomic() - a savepoint
    doesn't compose with PREPARE TRANSACTION the way a real transaction does. Confirmed live: this
    didn't fail cleanly, it hung for AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS (30s) on the
    coordinator's first query and then raised a misleading "concurrent gather()/TaskGroup sibling"
    TransactionManagementError - there was no such sibling, it was this same task's own mis-nested
    savepoint. Now rejected immediately with a clear QueryError instead."""
    async with Transactions.atomic(COORDINATOR):
        with pytest.raises(QueryError, match="already inside an open transaction"):
            async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")

    assert await _widget_count(COORDINATOR) == 0
    assert await _widget_count(PARTICIPANT_A) == 0


@pytest.mark.asyncio
async def test_distributed_rejects_nesting_inside_an_ambient_transaction_on_a_participant(distributed_dbs) -> None:
    """Same rejection, but for a PARTICIPANT alias already inside an open transaction rather than
    the coordinator - the check covers every alias distributed() touches, not just the
    coordinator."""
    async with Transactions.atomic(PARTICIPANT_A):
        with pytest.raises(QueryError, match="already inside an open transaction"):
            async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]):
                pass

    assert await _widget_count(COORDINATOR) == 0
    assert await _widget_count(PARTICIPANT_A) == 0


@pytest.mark.asyncio
async def test_distributed_rollback_on_exception_in_block(distributed_dbs) -> None:
    with pytest.raises(RuntimeError, match="boom"):
        async with Transactions.distributed(
            coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
        ) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
            raise RuntimeError("boom")

    assert await _widget_count(COORDINATOR) == 0
    assert await _widget_count(PARTICIPANT_A) == 0
    assert await _widget_count(PARTICIPANT_B) == 0


@pytest.mark.asyncio
async def test_distributed_rollback_on_prepare_failure(distributed_dbs, monkeypatch: pytest.MonkeyPatch) -> None:
    """A PREPARE TRANSACTION failure on ANY participant rolls everything back - including
    participants that already prepared successfully, and the coordinator (never touched)."""
    fixed_uuid_hex = uuid.uuid4().hex
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(fixed_uuid_hex))
    fixed_xid = DistributedCoordinator._make_xid(COORDINATOR)

    # Pre-occupy the exact GID distributed() will construct for participant_b (xid:alias), from
    # an independent connection, so the real PREPARE TRANSACTION it issues there collides on a
    # duplicate gid and fails - after participant_a's own PREPARE has already succeeded. Goes
    # through _in_transaction()/__aenter__() (a plain client's own execute() acquires+
    # releases a pooled connection PER CALL, which would silently roll back a bare "BEGIN" before
    # "PREPARE TRANSACTION" ever ran on it) but calls __aexit__() right after preparing, before
    # distributed() ever runs - that resets the "participant_b" alias mapping __aenter__()
    # installed, so distributed()'s own lookup isn't affected by this blocker at all.
    blocking_gid = DistributedCoordinator._participant_gid(fixed_xid, PARTICIPANT_B)
    blocker = Connections.current().create_independent(PARTICIPANT_B)
    blocker_tx = blocker._in_transaction()
    try:
        blocker_client = await blocker_tx.__aenter__()
        await blocker_client.execute(f"PREPARE TRANSACTION '{blocking_gid}'")
        blocker_client._finalized = True
    finally:
        await blocker_tx.__aexit__(None, None, None)

    try:
        with pytest.raises(Exception):  # noqa: PT011 - the exact translated exception type is driver-specific
            async with Transactions.distributed(
                coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
            ) as txns:
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
    finally:
        await blocker.execute(f"ROLLBACK PREPARED '{blocking_gid}'")
        await blocker.close()

    assert await _widget_count(COORDINATOR) == 0
    assert await _widget_count(PARTICIPANT_A) == 0
    assert await _widget_count(PARTICIPANT_B) == 0


@pytest.mark.asyncio
async def test_distributed_rollback_on_prepare_failure_fires_on_rollback_for_already_prepared_participant(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors test_distributed_rollback_on_prepare_failure's exact setup (participant_a
    successfully PREPAREs, participant_b's own PREPARE then fails on a colliding gid) - but here
    checks that participant_a's on_rollback() callback actually fires. Every abort path in
    Transactions.distributed() used to call _get_prepared_on_fresh_connection(alias, xid,
    commit=False) for an already-PREPARE'd participant with no on_rollback()-firing equivalent
    of the mirrored commit-success loop's own on_commit() callbacks - the participant's
    transaction genuinely rolled back, but nothing using on_rollback() for compensating actions
    (the exact pattern this hook exists for) ever learned it happened."""
    fixed_uuid_hex = uuid.uuid4().hex
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(fixed_uuid_hex))
    fixed_xid = DistributedCoordinator._make_xid(COORDINATOR)

    blocking_gid = DistributedCoordinator._participant_gid(fixed_xid, PARTICIPANT_B)
    blocker = Connections.current().create_independent(PARTICIPANT_B)
    blocker_tx = blocker._in_transaction()
    try:
        blocker_client = await blocker_tx.__aenter__()
        await blocker_client.execute(f"PREPARE TRANSACTION '{blocking_gid}'")
        blocker_client._finalized = True
    finally:
        await blocker_tx.__aexit__(None, None, None)

    rollback_callback = AsyncMock()
    try:
        with pytest.raises(Exception):  # noqa: PT011 - the exact translated exception type is driver-specific
            async with Transactions.distributed(
                coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
            ) as txns:
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
                Transactions.on_rollback(rollback_callback, using=PARTICIPANT_A)
    finally:
        await blocker.execute(f"ROLLBACK PREPARED '{blocking_gid}'")
        await blocker.close()

    rollback_callback.assert_awaited_once()


@pytest.mark.asyncio
async def test_distributed_rollback_on_prepare_failure_records_rollback_transaction_event(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same setup as the on_rollback() test above (participant_a successfully PREPAREs,
    participant_b's own PREPARE then fails on a colliding gid) - but here checks
    TransactionInstrumentation. _abort_prepared_participant() fired participant_a's
    on_rollback() callbacks but never called client._record_transaction_end(ROLLBACK), unlike
    the mirrored commit-success loop's own client._record_transaction_end(COMMIT) call - a
    registered TransactionHook saw participant_a's own BEGIN with no paired end event at all,
    permanently mislabeling it as still open even though it genuinely rolled back."""
    fixed_uuid_hex = uuid.uuid4().hex
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(fixed_uuid_hex))
    fixed_xid = DistributedCoordinator._make_xid(COORDINATOR)

    blocking_gid = DistributedCoordinator._participant_gid(fixed_xid, PARTICIPANT_B)
    blocker = Connections.current().create_independent(PARTICIPANT_B)
    blocker_tx = blocker._in_transaction()
    try:
        blocker_client = await blocker_tx.__aenter__()
        await blocker_client.execute(f"PREPARE TRANSACTION '{blocking_gid}'")
        blocker_client._finalized = True
    finally:
        await blocker_tx.__aexit__(None, None, None)

    participant_a_events = []

    def hook(event):
        event, connection_name = event.type, event.connection_name
        if connection_name == PARTICIPANT_A:
            participant_a_events.append(event)

    Observers.observe(TransactionEvent, hook)
    try:
        with pytest.raises(Exception):  # noqa: PT011 - the exact translated exception type is driver-specific
            async with Transactions.distributed(
                coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
            ) as txns:
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(TransactionEvent, hook)
        await blocker.execute(f"ROLLBACK PREPARED '{blocking_gid}'")
        await blocker.close()

    assert participant_a_events == [TransactionEventType.BEGIN, TransactionEventType.ROLLBACK]


@pytest.mark.asyncio
async def test_distributed_partial_commit_raises_and_recovery_finishes_it(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a participant's COMMIT PREPARED fails after the coordinator already committed the
    decision, distributed() raises DistributedTransactionPartiallyCommittedError naming it -
    the write already durably happened, `hare distributed-recover` finishes delivering it."""
    real_commit_prepared = DistributedCoordinator.commit_prepared

    async def flaky_commit_prepared(client, alias: str, xid: str) -> None:
        if alias == PARTICIPANT_B:
            raise ConnectionError("simulated commit-prepared failure")
        await real_commit_prepared(client, alias, xid)

    monkeypatch.setattr(DistributedCoordinator, "commit_prepared", AsyncMock(side_effect=flaky_commit_prepared))

    with pytest.raises(DistributedTransactionPartiallyCommittedError) as exc_info:
        async with Transactions.distributed(
            coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
        ) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
            await DistributedWidget.objects.using(txns[PARTICIPANT_B]).create(name="on-b")

    assert exc_info.value.pending_participant_aliases == [PARTICIPANT_B]
    xid = exc_info.value.xid

    # The decision already committed durably - coordinator and participant_a both see it.
    assert await _widget_count(COORDINATOR) == 1
    assert await _widget_count(PARTICIPANT_A) == 1
    # participant_b's write is still only PREPARE'd, not yet visible.
    assert await _widget_count(PARTICIPANT_B) == 0

    stale = await DistributedCoordinator.detect_stale_prepared_transactions(COORDINATOR, older_than_seconds=0)
    matching = [entry for entry in stale if entry.xid == xid]
    assert len(matching) == 1
    assert matching[0].participant_alias == PARTICIPANT_B
    assert matching[0].resolution == DistributedTransactionResolution.COMMIT

    await real_commit_prepared(Connections.get(PARTICIPANT_B), PARTICIPANT_B, xid)
    assert await _widget_count(PARTICIPANT_B) == 1


@pytest.mark.asyncio
async def test_detect_stale_with_zero_age_survives_server_clock_stepping_backwards(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """older_than_seconds=0 finds a decision row whose created_at is later than the server's
    now() - what a backwards server clock step (NTP, VM time sync) right after the commit
    produces. It used to compare created_at < now() anyway and report nothing."""
    real_commit_prepared = DistributedCoordinator.commit_prepared

    async def flaky_commit_prepared(client, alias: str, xid: str) -> None:
        if alias == PARTICIPANT_B:
            raise ConnectionError("simulated commit-prepared failure")
        await real_commit_prepared(client, alias, xid)

    monkeypatch.setattr(DistributedCoordinator, "commit_prepared", AsyncMock(side_effect=flaky_commit_prepared))

    with pytest.raises(DistributedTransactionPartiallyCommittedError) as exc_info:
        async with Transactions.distributed(
            coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
        ) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
            await DistributedWidget.objects.using(txns[PARTICIPANT_B]).create(name="on-b")
    xid = exc_info.value.xid
    try:
        await Connections.get(COORDINATOR).execute(
            "UPDATE hare_distributed_decisions SET created_at = now() + interval '1 hour' WHERE xid = $1", [xid]
        )

        stale = await DistributedCoordinator.detect_stale_prepared_transactions(COORDINATOR, older_than_seconds=0)
        matching = [entry for entry in stale if entry.xid == xid]
        assert [(entry.participant_alias, entry.resolution) for entry in matching] == [
            (PARTICIPANT_B, DistributedTransactionResolution.COMMIT)
        ]

        # A nonzero age still filters: neither the decision row nor the fresh prepared
        # transaction is an hour old.
        stale = await DistributedCoordinator.detect_stale_prepared_transactions(COORDINATOR, older_than_seconds=3600)
        assert [entry for entry in stale if entry.xid == xid] == []
    finally:
        await real_commit_prepared(Connections.get(PARTICIPANT_B), PARTICIPANT_B, xid)


@pytest.mark.asyncio
async def test_distributed_partial_commit_does_not_permanently_strand_rollback_restore_bookkeeping(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Model._register_rollback_restore() (Meta.optimistic_lock_field/soft_delete_field
    optimistic-bump protection) registers its own on_commit()/on_rollback() pair on the
    participant's client the moment save() runs inside distributed()'s block. If that
    participant's own COMMIT PREPARED later fails (the exact partial-commit scenario the test
    above covers), distributed()'s commit-phase loop deliberately fires NEITHER callback for it
    (correctly - which of "committed" or "not" actually happened is unknown, so firing either
    would risk lying to an unrelated caller's own on_rollback()/on_commit()). That correctly
    leaves this mechanism's own pending marker on the model instance without a normal way to
    clear - confirmed live as a real bug: a LATER, entirely separate, ordinary transaction on
    the same instance that itself rolls back for an unrelated reason silently skipped
    registering its OWN restore (the marker looked "already pending"), so its own version bump
    was never rolled back in memory either, permanently strand-locking the instance out of the
    very protection round AR's own rollback-restore fix added."""
    real_commit_prepared = DistributedCoordinator.commit_prepared

    async def flaky_commit_prepared(client, alias: str, xid: str) -> None:
        if alias == PARTICIPANT_B:
            raise ConnectionError("simulated commit-prepared failure")
        await real_commit_prepared(client, alias, xid)

    monkeypatch.setattr(DistributedCoordinator, "commit_prepared", AsyncMock(side_effect=flaky_commit_prepared))

    widget = await DistributedVersionedWidget.objects.using(Connections.get(PARTICIPANT_B)).create(name="v0")
    assert widget.version == 0

    with pytest.raises(DistributedTransactionPartiallyCommittedError) as exc_info:
        async with Transactions.distributed(
            coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
        ) as txns:
            widget.name = "v1"
            await widget.save(using=txns[PARTICIPANT_B])

    xid = exc_info.value.xid
    # The marker is stuck at this point - confirmed as the live bug before the fix - resolve the
    # participant properly (as `hare distributed-recover` would) before using it again below.
    await real_commit_prepared(Connections.get(PARTICIPANT_B), PARTICIPANT_B, xid)

    class BoomError(Exception):
        pass

    with pytest.raises(BoomError):
        async with Transactions.atomic(PARTICIPANT_B) as conn:
            widget.name = "v2"
            await widget.save(using=conn)
            assert widget.version == 2
            raise BoomError("unrelated failure")

    # The second save()'s own version bump must have been rolled back in memory - not stuck at
    # 2 (this save()'s own bumped value) or 1 (the never-cleared value from the partial commit).
    assert widget.version == 1
    fresh = await DistributedVersionedWidget.objects.using(Connections.get(PARTICIPANT_B)).get(pk=widget.pk)
    assert fresh.version == 1
    assert fresh.name == "v1"


@pytest.mark.asyncio
async def test_detect_stale_prepared_transactions_ignores_a_different_coordinators_xid(distributed_dbs) -> None:
    """A prepared transaction on a shared participant, left there by a DIFFERENT coordinator that
    hasn't (yet, or ever) written its own decision row, must not be mistaken for an orphan of THIS
    coordinator and rolled back out from under the other coordinator's own in-flight or already-
    decided transaction. _detect_stale_prepared_transactions(COORDINATOR, ...) used to scan every
    Postgres alias for any prepared xid under the global hare_dtx_ prefix and treat "no decision
    row in COORDINATOR's own table" as "orphaned, safe to roll back" - true just as often of a
    xid belonging to a different coordinator that simply hasn't committed its own decision yet.
    The xid now embeds which coordinator owns it, so this scan can tell the two cases apart."""
    other_coordinator_xid = DistributedCoordinator._make_xid(PARTICIPANT_B)
    participant = Connections.current().create_independent(PARTICIPANT_A)
    tx = participant._in_transaction()
    try:
        client = await tx.__aenter__()
        await DistributedWidget.objects.using(client).create(name="belongs-to-a-different-coordinator")
        await DistributedCoordinator._prepare_participant(client, PARTICIPANT_A, other_coordinator_xid)
    finally:
        await tx.__aexit__(None, None, None)
        await participant.close()

    try:
        stale = await DistributedCoordinator.detect_stale_prepared_transactions(COORDINATOR, older_than_seconds=0)
        assert all(entry.xid != other_coordinator_xid for entry in stale)
    finally:
        await DistributedCoordinator.rollback_prepared(
            Connections.get(PARTICIPANT_A), PARTICIPANT_A, other_coordinator_xid
        )


@pytest.mark.asyncio
async def test_detect_stale_prepared_transactions_ignores_same_alias_gid_from_another_database(
    distributed_dbs,
) -> None:
    """pg_prepared_xacts is cluster-wide: a hare-shaped GID for alias PARTICIPANT_A prepared in a
    DIFFERENT database on the same server (another deployment reusing the same alias names) must
    not be reported as this deployment's orphan, nor seen as prepared on PARTICIPANT_A."""
    xid = DistributedCoordinator._make_xid(COORDINATOR)
    other_database = Connections.current().create_independent(PARTICIPANT_B)
    tx = other_database._in_transaction()
    try:
        client = await tx.__aenter__()
        await DistributedCoordinator._prepare_participant(client, PARTICIPANT_A, xid)
    finally:
        await tx.__aexit__(None, None, None)
        await other_database.close()

    try:
        stale = await DistributedCoordinator.detect_stale_prepared_transactions(COORDINATOR, older_than_seconds=0)
        assert all(entry.xid != xid for entry in stale)
        assert not await DistributedCoordinator._xid_is_prepared(Connections.get(PARTICIPANT_A), PARTICIPANT_A, xid)
    finally:
        await DistributedCoordinator.rollback_prepared(Connections.get(PARTICIPANT_B), PARTICIPANT_A, xid)


@pytest.mark.asyncio
async def test_distributed_cancellation_during_participant_commit_raises_partially_committed(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task cancelled while distributed() is mid-loop over COMMIT PREPARED for each participant
    used to propagate a bare asyncio.CancelledError - unlike every OTHER partial-failure path in
    this same function, which all raise the documented DistributedTransactionPartiallyCommittedError
    instead. The participant whose COMMIT PREPARED was cancelled, AND every participant not yet
    reached in iteration order, must both end up in pending_participant_aliases - the loop
    can't safely attempt more work reactively after being told to stop."""
    real_commit_prepared = DistributedCoordinator.commit_prepared

    async def cancelled_commit_prepared(client, alias: str, xid: str) -> None:
        if alias == PARTICIPANT_A:
            raise asyncio.CancelledError
        await real_commit_prepared(client, alias, xid)

    monkeypatch.setattr(DistributedCoordinator, "commit_prepared", AsyncMock(side_effect=cancelled_commit_prepared))

    with pytest.raises(DistributedTransactionPartiallyCommittedError) as exc_info:
        async with Transactions.distributed(
            coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
        ) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
            await DistributedWidget.objects.using(txns[PARTICIPANT_B]).create(name="on-b")

    assert exc_info.value.pending_participant_aliases == [PARTICIPANT_A, PARTICIPANT_B]
    assert isinstance(exc_info.value.__cause__, asyncio.CancelledError)
    xid = exc_info.value.xid

    # The decision already committed durably on the coordinator - only the participants are
    # still outstanding.
    assert await _widget_count(COORDINATOR) == 1
    assert await _widget_count(PARTICIPANT_A) == 0
    assert await _widget_count(PARTICIPANT_B) == 0

    await real_commit_prepared(Connections.get(PARTICIPANT_A), PARTICIPANT_A, xid)
    await real_commit_prepared(Connections.get(PARTICIPANT_B), PARTICIPANT_B, xid)
    assert await _widget_count(PARTICIPANT_A) == 1
    assert await _widget_count(PARTICIPANT_B) == 1


@pytest.mark.asyncio
async def test_distributed_coordinator_commit_failure_is_ambiguous_not_rollback(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the coordinator's own commit() itself fails or its outcome is unknown, distributed()
    must NOT guess and roll back the already-PREPARE'd participants - the commit may have
    actually landed server-side despite the client-side failure. It raises
    DistributedTransactionCommitAmbiguousError instead, leaving every participant's prepared
    state untouched for `hare distributed-recover` (or a manual check) to resolve correctly."""
    # Discover the actual per-driver transaction-wrapper class at test time (asyncpg vs rust_pg)
    # rather than importing either directly - this test runs against whichever HARE_TEST_DB names.
    probe_context = Connections.get(COORDINATOR)._in_transaction()
    probe_client = await probe_context.__aenter__()
    wrapper_cls = type(probe_client)
    await probe_context.__aexit__(None, None, None)

    real_commit = wrapper_cls.commit

    async def flaky_commit(self) -> None:
        raise ConnectionError("simulated coordinator commit failure")

    monkeypatch.setattr(wrapper_cls, "commit", flaky_commit)
    try:
        with pytest.raises(DistributedTransactionCommitAmbiguousError) as exc_info:
            async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
    finally:
        monkeypatch.setattr(wrapper_cls, "commit", real_commit)

    assert exc_info.value.coordinator_alias == COORDINATOR
    assert exc_info.value.participant_aliases == [PARTICIPANT_A]
    xid = exc_info.value.xid

    # The mocked commit() never ran real SQL, so the coordinator's own INSERT never actually
    # committed either - no decision row exists, so this is the "presumed abort" side: recovery's
    # orphan scan finds participant_a's still-prepared xid with no matching decision.
    stale = await DistributedCoordinator.detect_stale_prepared_transactions(COORDINATOR, older_than_seconds=0)
    matching = [entry for entry in stale if entry.xid == xid]
    assert len(matching) == 1
    assert matching[0].participant_alias == PARTICIPANT_A
    assert matching[0].resolution == DistributedTransactionResolution.ROLLBACK

    await DistributedCoordinator.rollback_prepared(Connections.get(PARTICIPANT_A), PARTICIPANT_A, xid)
    assert await _widget_count(COORDINATOR) == 0
    assert await _widget_count(PARTICIPANT_A) == 0


@pytest.mark.asyncio
async def test_distributed_ambiguous_commit_that_really_landed_does_not_fire_on_rollback(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ambiguous-commit cleanup branch used to call coordinator_client.rollback() "just in
    case" the COMMIT never really landed - that wrapper method unconditionally fires every
    registered on_rollback() callback once its own SQL lands without error, and can't tell
    "genuinely rolled back" apart from "no-op against an already-committed session" (Postgres
    treats a ROLLBACK with no open transaction as a well-defined no-op, not an error). Confirmed
    live: a coordinator commit that genuinely lands server-side but reports ambiguous client-side
    (e.g. the acknowledgement is lost after the server processed it) still fired on_rollback() -
    misleading a compensating-action callback into thinking a write that, in fact, landed was
    undone. Fixed by issuing a raw ROLLBACK instead of the callback-firing wrapper method."""
    probe_context = Connections.get(COORDINATOR)._in_transaction()
    probe_client = await probe_context.__aenter__()
    wrapper_cls = type(probe_client)
    await probe_context.__aexit__(None, None, None)

    real_commit = wrapper_cls.commit

    async def commit_for_real_then_report_ambiguous(self) -> None:
        # A real COMMIT sent directly, bypassing the driver's own Transaction object (so its
        # internal state never learns the transaction actually ended) - then raises, simulating
        # an acknowledgement lost after the server already processed it.
        await self.execute("COMMIT")
        raise ConnectionError("simulated ack loss after a real commit")

    monkeypatch.setattr(wrapper_cls, "commit", commit_for_real_then_report_ambiguous)

    rollback_callback = AsyncMock()
    try:
        with pytest.raises(DistributedTransactionCommitAmbiguousError) as exc_info:
            async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
                Transactions.on_rollback(rollback_callback, using=COORDINATOR)
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
    finally:
        monkeypatch.setattr(wrapper_cls, "commit", real_commit)

    xid = exc_info.value.xid
    assert await _widget_count(COORDINATOR) == 1, "the raw COMMIT should have really landed"
    rollback_callback.assert_not_awaited()

    await DistributedCoordinator.rollback_prepared(Connections.get(PARTICIPANT_A), PARTICIPANT_A, xid)


@pytest.mark.asyncio
async def test_distributed_coordinator_commit_failure_with_two_participants_leaves_pools_usable(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ambiguous-commit cleanup branch tears down every participant context via
    context.__aexit__() (Connections.current().reset(token) under the hood) - with 2+
    participants, each __aenter__() pushed a nested contextvars.Token onto the SAME
    ConnectionHandler ContextVar, and reset() must unwind them in strict LIFO order or
    ConnectionHandler.reset()'s own "restore pre-transaction state" snapshot gets corrupted,
    closing a still-live participant's connection pool out from under it (the exact bug this
    branch had - every OTHER cleanup branch in distributed() already reversed this loop, this
    one didn't). Both participants' pools must stay usable after cleanup, not just rolled back."""
    probe_context = Connections.get(COORDINATOR)._in_transaction()
    probe_client = await probe_context.__aenter__()
    wrapper_cls = type(probe_client)
    await probe_context.__aexit__(None, None, None)

    real_commit = wrapper_cls.commit

    async def flaky_commit(self) -> None:
        raise ConnectionError("simulated coordinator commit failure")

    monkeypatch.setattr(wrapper_cls, "commit", flaky_commit)
    try:
        with pytest.raises(DistributedTransactionCommitAmbiguousError) as exc_info:
            async with Transactions.distributed(
                coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
            ) as txns:
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
                await DistributedWidget.objects.using(txns[PARTICIPANT_B]).create(name="on-b")
    finally:
        monkeypatch.setattr(wrapper_cls, "commit", real_commit)

    xid = exc_info.value.xid

    # Both participants' ambient connections must still work - this is what the out-of-order
    # LIFO unwind used to break (one of them would raise DBConnectionError: pool is closing).
    assert await _widget_count(PARTICIPANT_A) == 0
    assert await _widget_count(PARTICIPANT_B) == 0

    await DistributedCoordinator.rollback_prepared(Connections.get(PARTICIPANT_A), PARTICIPANT_A, xid)
    await DistributedCoordinator.rollback_prepared(Connections.get(PARTICIPANT_B), PARTICIPANT_B, xid)


@pytest.mark.asyncio
async def test_distributed_coordinator_on_commit_callback_failure_is_not_ambiguous(distributed_dbs) -> None:
    """A broken on_commit() callback on the COORDINATOR must not be conflated with a genuinely
    ambiguous commit outcome (test_distributed_coordinator_commit_failure_is_ambiguous_not_rollback
    above covers that real case) - commit() marks _finalized before running callbacks, so by the
    time the callback raises, the real COMMIT is already known to have landed. Every participant
    must still receive its own COMMIT PREPARED, and the caller must see the callback's own
    exception, not the misleading DistributedTransactionCommitAmbiguousError."""

    def failing_callback() -> None:
        raise RuntimeError("boom from coordinator on_commit")

    with pytest.raises(RuntimeError, match="boom from coordinator on_commit"):
        async with Transactions.distributed(
            coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
        ) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
            await DistributedWidget.objects.using(txns[PARTICIPANT_B]).create(name="on-b")
            Transactions.on_commit(failing_callback, using=COORDINATOR)

    # The distributed transaction itself committed fully on every alias, and every connection is
    # still usable (not left pointing at a dead, _finalized=True wrapper the way the unfixed bug
    # left it) - _widget_count() below runs a real query against each one directly.
    assert await _widget_count(COORDINATOR) == 1
    assert await _widget_count(PARTICIPANT_A) == 1
    assert await _widget_count(PARTICIPANT_B) == 1


@pytest.mark.asyncio
async def test_distributed_participant_on_commit_callback_failure_does_not_abort_other_participants(
    distributed_dbs,
) -> None:
    """Same reasoning as the coordinator case above, for a PARTICIPANT's own on_commit() callback -
    its COMMIT PREPARED genuinely landed by the time the callback runs, so a broken callback must
    not skip COMMIT PREPARED/cleanup for the OTHER participants, corrupt connection state, or be
    reported as DistributedTransactionPartiallyCommittedError (this participant did NOT fail to
    commit - only its own callback did)."""

    def failing_callback() -> None:
        raise RuntimeError("boom from participant_a on_commit")

    with pytest.raises(RuntimeError, match="boom from participant_a on_commit"):
        async with Transactions.distributed(
            coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
        ) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
            await DistributedWidget.objects.using(txns[PARTICIPANT_B]).create(name="on-b")
            Transactions.on_commit(failing_callback, using=PARTICIPANT_A)

    # PARTICIPANT_B (reached AFTER participant_a in iteration order) must still have received its
    # own COMMIT PREPARED despite participant_a's callback blowing up - every alias's connection
    # must still be usable afterward too.
    assert await _widget_count(COORDINATOR) == 1
    assert await _widget_count(PARTICIPANT_A) == 1
    assert await _widget_count(PARTICIPANT_B) == 1


@pytest.mark.asyncio
async def test_distributed_participant_on_commit_callback_can_query_its_own_alias(distributed_dbs) -> None:
    """Regression: a participant's own on_commit() callback used to run directly on the already-
    _finalized TransactionClient, with Connections.current() for this alias still pointing at
    that same finalized wrapper (its own context's __aexit__() - the only thing that resets the
    ContextVar - hadn't run yet). Any query the callback tried to issue on this alias (Connections.
    get(alias)) hit that same finalized wrapper and immediately raised TransactionManagementError
    ("already finalised"), even though the real COMMIT PREPARED for this participant had already
    genuinely landed. Fixed by routing through _finish_top_level_operation(), the same helper an
    ordinary top-level commit() already uses, which aliases Connections.current() back to the
    participant's own non-transactional client for the duration of its callbacks - exactly like an
    ordinary (non-distributed) transaction's on_commit() callback already could."""
    async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
        await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
        await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")

        async def callback_creates_a_new_row() -> None:
            await DistributedWidget.objects.using(Connections.get(PARTICIPANT_A)).create(name="from-callback")

        Transactions.on_commit(callback_creates_a_new_row, using=PARTICIPANT_A)

    assert await _widget_count(PARTICIPANT_A) == 2


@pytest.mark.asyncio
async def test_distributed_participant_on_rollback_callback_can_query_its_own_alias(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same regression as the on_commit() test above, but for _abort_prepared_participant()'s own
    on_rollback() firing path - mirrors test_distributed_rollback_on_prepare_failure_fires_on_
    rollback_for_already_prepared_participant's setup (participant_a successfully PREPAREs,
    participant_b's own PREPARE then fails on a colliding gid, so participant_a is rolled back via
    ROLLBACK PREPARED and its on_rollback() callbacks fire)."""
    fixed_uuid_hex = uuid.uuid4().hex
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(fixed_uuid_hex))
    fixed_xid = DistributedCoordinator._make_xid(COORDINATOR)

    blocking_gid = DistributedCoordinator._participant_gid(fixed_xid, PARTICIPANT_B)
    blocker = Connections.current().create_independent(PARTICIPANT_B)
    blocker_tx = blocker._in_transaction()
    try:
        blocker_client = await blocker_tx.__aenter__()
        await blocker_client.execute(f"PREPARE TRANSACTION '{blocking_gid}'")
        blocker_client._finalized = True
    finally:
        await blocker_tx.__aexit__(None, None, None)

    async def callback_creates_a_new_row() -> None:
        await DistributedWidget.objects.using(Connections.get(PARTICIPANT_A)).create(name="from-rollback-callback")

    try:
        with pytest.raises(Exception):  # noqa: PT011 - the exact translated exception type is driver-specific
            async with Transactions.distributed(
                coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_B]
            ) as txns:
                await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
                await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
                Transactions.on_rollback(callback_creates_a_new_row, using=PARTICIPANT_A)
    finally:
        await blocker.execute(f"ROLLBACK PREPARED '{blocking_gid}'")
        await blocker.close()

    # participant_a's own rolled-back write ("on-a") never landed, but the on_rollback()
    # callback's own INDEPENDENT write (issued fresh, after the real ROLLBACK PREPARED) must have.
    assert await _widget_count(PARTICIPANT_A) == 1


@pytest.mark.asyncio
async def test_distributed_rust_pg_participant_connection_is_released_after_commit(distributed_dbs) -> None:
    """rust_pg-only regression guard: PREPARE TRANSACTION ends the SQL-level transaction but,
    unlike a real commit()/rollback(), never goes through pg.Transaction's own commit()/
    rollback() methods - the only two places that release the pinned connection back to the
    pool. Without RustPgTransactionContext.__aexit__ explicitly releasing it afterward (via the
    Rust-side finish_prepared()), every successful Transactions.distributed() call permanently
    leaked each participant's pooled connection."""
    participant_client = Connections.get(PARTICIPANT_A)
    if not isinstance(participant_client, RustPgClient):
        pytest.skip(
            "this regression is rust_pg-specific - asyncpg's TransactionContextPooled "
            "already releases the pool unconditionally regardless of _finalized"
        )

    async def other_connection_count() -> int:
        _, rows = await participant_client.execute(
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = current_database() AND pid != pg_backend_pid()"
        )
        return int(rows[0]["n"])

    before = await other_connection_count()
    for i in range(3):
        async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name=f"c{i}")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name=f"p{i}")
    after = await other_connection_count()

    assert after <= before, (
        f"participant_a's pool leaked a connection per distributed() call: {before} -> {after} "
        "other connections after 3 calls"
    )


@pytest.mark.asyncio
async def test_distributed_survives_participant_connection_termination(distributed_dbs) -> None:
    """A prepared transaction is NOT tied to the connection that issued PREPARE TRANSACTION -
    killing that connection's own backend still leaves it resolvable from a fresh connection,
    the guarantee hare distributed-recover's own crash-recovery story depends on."""
    xid = f"hare_dtx_{uuid.uuid4().hex}"
    participant = Connections.current().create_independent(PARTICIPANT_A)
    tx = participant._in_transaction()
    backend_pid: int | None = None
    try:
        client = await tx.__aenter__()
        await DistributedWidget.objects.using(client).create(name="mid-flight")
        _, pid_rows = await client.execute("SELECT pg_backend_pid() AS pid")
        backend_pid = pid_rows[0]["pid"]
        await DistributedCoordinator._prepare_participant(client, PARTICIPANT_A, xid)
    finally:
        # __aexit__ is a no-op on commit/rollback (already _finalized by PREPARE TRANSACTION
        # above) but still releases the pool slot and resets the "participant_a" alias mapping
        # __aenter__ installed - skipping it would leave Connections.get(PARTICIPANT_A) returning
        # this dead, finalized client for the rest of the test instead of a fresh connection.
        await tx.__aexit__(None, None, None)
        await participant.close()

    killer = Connections.current().create_independent(PARTICIPANT_A)
    try:
        await killer.execute("SELECT pg_terminate_backend($1)", [backend_pid])
    finally:
        await killer.close()

    # Resolve from a brand-new connection - proves the prepared transaction outlived the one
    # that created it, and survives the connection that prepared it being killed outright.
    fresh = Connections.get(PARTICIPANT_A)
    await DistributedCoordinator.commit_prepared(fresh, PARTICIPANT_A, xid)
    assert await _widget_count(PARTICIPANT_A) == 1


@pytest.mark.asyncio
async def test_distributed_requires_at_least_one_participant(distributed_dbs) -> None:
    with pytest.raises(QueryError):
        async with Transactions.distributed(coordinator=COORDINATOR, participants=[]):
            pass


@pytest.mark.asyncio
async def test_distributed_rejects_duplicate_aliases(distributed_dbs) -> None:
    with pytest.raises(QueryError):
        async with Transactions.distributed(coordinator=COORDINATOR, participants=[COORDINATOR]):
            pass
    with pytest.raises(QueryError):
        async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A, PARTICIPANT_A]):
            pass


@pytest_asyncio.fixture
async def sqlite_multi_db() -> AsyncGenerator[None]:
    async with HareContext() as ctx:
        await ctx.init(
            config={
                "connections": {"first": "sqlite://:memory:", "second": "sqlite://:memory:"},
                "apps": {"models": {"models": [__name__], "default_connection": "first"}},
            }
        )
        await ctx.generate_schemas()
        yield


@pytest.mark.asyncio
async def test_distributed_rejects_non_postgres_alias(sqlite_multi_db) -> None:
    with pytest.raises(UnSupportedError, match="sqlite"):
        async with Transactions.distributed(coordinator="first", participants=["second"]):
            pass


def _fake_participant(*, rollback_raises: bool = False) -> tuple[object, object]:
    client = MagicMock()
    client._finalized = False
    client.log = MagicMock()
    client.rollback = AsyncMock(side_effect=RuntimeError("connection is closed") if rollback_raises else None)
    context = MagicMock()
    context.__aexit__ = AsyncMock(return_value=None)
    return client, context


@pytest.mark.asyncio
async def test_roll_back_distributed_transaction_survives_one_dead_participant() -> None:
    """A participant whose own connection already died (a network drop right before its own
    PREPARE TRANSACTION - client.rollback() then genuinely raises) must never stop the rest of
    distributed()'s own cleanup: every OTHER participant still gets rolled back and its
    transaction context exited, and - critically - the coordinator still gets rolled back and
    exited too, so its transaction is never left open indefinitely."""
    healthy_client, healthy_context = _fake_participant()
    dead_client, dead_context = _fake_participant(rollback_raises=True)
    coordinator_client, coordinator_context = _fake_participant()

    participant_clients = {"healthy": healthy_client, "dead": dead_client}
    participant_contexts = {"healthy": healthy_context, "dead": dead_context}

    # Must not raise - the whole point of the fix.
    await DistributedCoordinator._roll_back_distributed_transaction(
        participant_contexts, participant_clients, coordinator_context, coordinator_client, "xid-123"
    )

    healthy_client.rollback.assert_awaited_once()
    healthy_context.__aexit__.assert_awaited_once()
    dead_client.rollback.assert_awaited_once()
    # The dead participant's OWN context exit must still be attempted despite its rollback failing.
    dead_context.__aexit__.assert_awaited_once()
    # The coordinator must still be rolled back and its context exited, even though a participant
    # earlier in the loop failed - this is the "idle in transaction" leak the fix closes.
    coordinator_client.rollback.assert_awaited_once()
    coordinator_context.__aexit__.assert_awaited_once()


@pytest.mark.asyncio
async def test_roll_back_distributed_transaction_survives_dead_coordinator_too() -> None:
    """Symmetric case: even if the COORDINATOR's own rollback raises, every participant must
    still have been rolled back and exited before that - a dead coordinator connection must not
    retroactively skip cleanup that already happened, and the helper itself must not raise (its
    callers always re-raise the ORIGINAL failure immediately afterward)."""
    healthy_client, healthy_context = _fake_participant()
    coordinator_client, coordinator_context = _fake_participant(rollback_raises=True)

    participant_clients = {"healthy": healthy_client}
    participant_contexts = {"healthy": healthy_context}

    await DistributedCoordinator._roll_back_distributed_transaction(
        participant_contexts, participant_clients, coordinator_context, coordinator_client, "xid-456"
    )

    healthy_client.rollback.assert_awaited_once()
    healthy_context.__aexit__.assert_awaited_once()
    coordinator_client.rollback.assert_awaited_once()
    coordinator_context.__aexit__.assert_awaited_once()


async def _prepared_gids_for(xid: str) -> list[str]:
    _, rows = await Connections.get(COORDINATOR).execute(
        "SELECT gid FROM pg_catalog.pg_prepared_xacts WHERE gid LIKE $1", [f"{xid}:%"]
    )
    return [row["gid"] for row in rows]


@pytest.mark.asyncio
async def test_distributed_cancellation_after_participant_prepare_landed_leaves_no_orphan(
    distributed_dbs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancellation arriving after a participant's PREPARE TRANSACTION landed server-side, but
    before its acknowledgement was processed, must still end in ROLLBACK PREPARED - not a plain
    rollback that leaves the prepared transaction orphaned (no decision row) holding its locks."""
    fixed_uuid_hex = uuid.uuid4().hex
    monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(fixed_uuid_hex))
    fixed_xid = DistributedCoordinator._make_xid(COORDINATOR)

    prepare_landed = asyncio.Event()

    async def run_distributed() -> None:
        async with Transactions.distributed(coordinator=COORDINATOR, participants=[PARTICIPANT_A]) as txns:
            await DistributedWidget.objects.using(txns.coordinator).create(name="on-coordinator")
            await DistributedWidget.objects.using(txns[PARTICIPANT_A]).create(name="on-a")
            transaction_client_class = type(txns[PARTICIPANT_A])
            original_execute_query = transaction_client_class.execute

            async def execute_query_with_slow_prepare_acknowledgement(self, query, *args, **kwargs):
                result = await original_execute_query(self, query, *args, **kwargs)
                if query.startswith("PREPARE TRANSACTION"):
                    prepare_landed.set()
                    await asyncio.sleep(0.5)
                return result

            monkeypatch.setattr(transaction_client_class, "execute", execute_query_with_slow_prepare_acknowledgement)

    task = asyncio.create_task(run_distributed())
    await asyncio.wait_for(prepare_landed.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    monkeypatch.undo()

    orphaned_gids = await _prepared_gids_for(fixed_xid)
    for gid in orphaned_gids:
        await Connections.get(PARTICIPANT_A).execute(f"ROLLBACK PREPARED '{gid}'")
    assert orphaned_gids == []
    assert await _widget_count(COORDINATOR) == 0
    assert await _widget_count(PARTICIPANT_A) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("database_message", "expected_message"),
    [
        ("prepared transactions are disabled", "max_prepared_transactions=0"),
        ("maximum number of prepared transactions reached", "distributed-recover"),
    ],
)
async def test_prepare_participant_maps_prepared_transaction_limits(database_message, expected_message) -> None:
    from hare.exceptions import OperationalError

    client = MagicMock()
    client.dialect = POSTGRESQL_DIALECT
    client._prepare_transaction = AsyncMock(side_effect=OperationalError(database_message))
    with pytest.raises(ConfigurationError, match=expected_message) as raised:
        await DistributedCoordinator._prepare_participant(client, PARTICIPANT_A, "hare_dtx_coordinator:xid")
    if expected_message == "distributed-recover":
        assert "restart Postgres" not in str(raised.value)


@pytest.mark.asyncio
async def test_prepare_participant_reraises_other_prepared_transaction_errors_unchanged() -> None:
    from hare.exceptions import OperationalError

    original_error = OperationalError('transaction identifier "x" is already in use for a prepared transaction')
    client = MagicMock()
    client.dialect = POSTGRESQL_DIALECT
    client._prepare_transaction = AsyncMock(side_effect=original_error)
    with pytest.raises(OperationalError) as raised:
        await DistributedCoordinator._prepare_participant(client, PARTICIPANT_A, "hare_dtx_coordinator:xid")
    assert raised.value is original_error
