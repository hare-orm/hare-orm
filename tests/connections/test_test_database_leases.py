from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from hare.contrib.test.databases.declarations import TestDatabaseIdentity as DatabaseIdentity
from hare.contrib.test.databases.kept_test_schema import KeptTestSchema
from hare.contrib.test.databases.reusable_test_databases import ReusableTestDatabases
from hare.contrib.test.databases.test_database_config import TestDatabaseConfig as DatabaseConfig
from hare.exceptions import ConfigurationError

pytestmark = pytest.mark.database_independent


@pytest.fixture
def database_template():
    return DatabaseIdentity(("server", 5432), f"test_{uuid.uuid4().hex[:12]}_{{}}")


def test_identical_names_on_different_endpoints_have_independent_leases(database_template):
    first = ReusableTestDatabases.acquire(database_template)
    second = ReusableTestDatabases.acquire(replace(database_template, endpoint=("another-server", 5432)))
    try:
        assert first.identity.database == second.identity.database
        ReusableTestDatabases.release(first, is_clean=True)
        assert ReusableTestDatabases.is_leased(second)
        assert not ReusableTestDatabases.is_clean(second.identity)
    finally:
        ReusableTestDatabases.release_leases([first, second])


def test_overlapping_acquisitions_release_only_their_exact_resources(database_template):
    ready = threading.Barrier(2, timeout=10)

    def acquire():
        lease = ReusableTestDatabases.acquire(database_template)
        ready.wait()
        return lease

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future, second_future = executor.submit(acquire), executor.submit(acquire)
        first, second = first_future.result(), second_future.result()
    try:
        assert first.identity != second.identity
        ReusableTestDatabases.release_leases([first])
        assert not ReusableTestDatabases.is_leased(first)
        assert ReusableTestDatabases.is_leased(second)
    finally:
        ReusableTestDatabases.release_leases([first, second])


def test_same_worker_name_in_different_runs_gets_distinct_slots(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setattr(ReusableTestDatabases, "run_namespace", "first-run")
    first = ReusableTestDatabases.get_slot_id(0)
    monkeypatch.setattr(ReusableTestDatabases, "run_namespace", "second-run")
    assert ReusableTestDatabases.get_slot_id(0) != first


@pytest.mark.parametrize("keep_schema", [False, True])
def test_claim_consumes_clean_marker_and_retained_schema_atomically(database_template, keep_schema):
    schema = KeptTestSchema("schema", 42, (), "fingerprint")
    previous = ReusableTestDatabases.acquire(database_template, keep_schema=True)
    ReusableTestDatabases.release(previous, is_clean=True, kept_schema=schema)
    current = ReusableTestDatabases.acquire(database_template, keep_schema=keep_schema)
    try:
        assert ReusableTestDatabases.claim(current) == ((True, schema) if keep_schema else (False, None))
        assert not ReusableTestDatabases.is_clean(current.identity)
        assert current.identity not in ReusableTestDatabases.kept_schemas
        assert ReusableTestDatabases.claim(current) == (False, None)
    finally:
        ReusableTestDatabases.release(current, is_clean=False)


def test_stale_token_cannot_consume_new_acquisitions_schema(database_template):
    first = ReusableTestDatabases.acquire(database_template)
    ReusableTestDatabases.release(first, is_clean=True)
    current = ReusableTestDatabases.acquire(database_template)
    try:
        with pytest.raises(ConfigurationError, match="no longer active"):
            ReusableTestDatabases.claim(first)
        ReusableTestDatabases.release(first, is_clean=False)
        assert ReusableTestDatabases.is_leased(current)
        assert ReusableTestDatabases.claim(current) == (True, None)
    finally:
        ReusableTestDatabases.release(current, is_clean=False)


def test_passwords_are_absent_from_lease_identity():
    database = DatabaseConfig.from_url("postgresql://reader:secret@example.test/test_{}", reuse=True)
    try:
        assert database.lease is not None
        assert "secret" not in repr(database.lease)
        assert "reader" not in repr(database.lease)
        assert "token=" not in repr(database.lease)
    finally:
        ReusableTestDatabases.release(database.lease, is_clean=False)
