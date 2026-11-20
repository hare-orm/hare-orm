"""Process-local leases for isolated, reusable server test databases."""

from __future__ import annotations

import itertools
import os
import threading
import uuid
from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING

from hare.contrib.test.constants import (
    ENV_PYTEST_XDIST_WORKER,
    REUSABLE_DATABASE_SLOT_NAMESPACE,
    REUSE_DATABASES_DISABLED_VALUES,
    REUSE_DATABASES_ENABLED_VALUES,
    REUSE_DATABASES_ENVIRONMENT_VARIABLE,
)
from hare.contrib.test.databases.declarations import TestDatabaseIdentity, TestDatabaseLease
from hare.contrib.test.messages import TEST_DATABASE_LEASE_EXPIRED
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.contrib.test.databases.kept_test_schema import KeptTestSchema


class ReusableTestDatabases:
    """Lease slots explicitly, isolated by endpoint and by process/run namespace.

    The random namespace and process id prevent independent pytest processes, including
    forked children with the same worker name, from resetting each other's databases.
    Slots are reusable within one process; a later test run receives a new namespace.
    """

    run_namespace = uuid.uuid4().hex
    leases: dict[TestDatabaseIdentity, TestDatabaseLease] = {}
    clean_databases: set[TestDatabaseIdentity] = set()
    kept_schemas: dict[TestDatabaseIdentity, KeptTestSchema] = {}
    lock = threading.Lock()

    @classmethod
    def is_enabled(cls, reuse_databases: bool | None = None) -> bool:
        """Whether test databases come from the pool.

        Args:
            reuse_databases: An explicit choice, or None to read REUSE_DATABASES_ENVIRONMENT_VARIABLE.

        Returns:
            The explicit choice, else the environment variable's value (off when unset).

        Raises:
            ConfigurationError: The environment variable holds a value that is neither on nor off.
        """
        if reuse_databases is not None:
            return reuse_databases
        raw_value = os.environ.get(REUSE_DATABASES_ENVIRONMENT_VARIABLE)
        if raw_value is None:
            return False
        normalized_value = raw_value.strip().lower()
        if normalized_value in REUSE_DATABASES_ENABLED_VALUES:
            return True
        if normalized_value in REUSE_DATABASES_DISABLED_VALUES:
            return False
        accepted_values = sorted(REUSE_DATABASES_ENABLED_VALUES | REUSE_DATABASES_DISABLED_VALUES)
        raise ConfigurationError(
            f"Invalid value {raw_value!r} for {REUSE_DATABASES_ENVIRONMENT_VARIABLE}: "
            f"expected one of {accepted_values}"
        )

    @classmethod
    def get_slot_id(cls, slot_number: int) -> str:
        """Return a stable slot id within this process, never shared by separate runs."""
        worker = os.environ.get(ENV_PYTEST_XDIST_WORKER, "")
        return uuid.uuid5(
            REUSABLE_DATABASE_SLOT_NAMESPACE, f"{cls.run_namespace}:{os.getpid()}:{worker}:{slot_number}"
        ).hex

    @classmethod
    def acquire(cls, template: TestDatabaseIdentity, *, keep_schema: bool = False) -> TestDatabaseLease:
        """Reserve the first free database slot and return its exact release token."""
        with cls.lock:
            for slot in itertools.count():
                identity = replace(template, database=template.database.format(cls.get_slot_id(slot)))
                if identity in cls.leases:
                    continue
                lease = TestDatabaseLease(identity, keep_schema=keep_schema)
                cls.leases[identity] = lease
                return lease
        raise AssertionError("unreachable")

    @classmethod
    def is_leased(cls, lease: TestDatabaseLease) -> bool:
        """Whether this exact acquisition still holds its resource."""
        with cls.lock:
            current = cls.leases.get(lease.identity)
            return current is not None and current.token is lease.token

    @classmethod
    def is_clean(cls, identity: TestDatabaseIdentity) -> bool:
        """Whether an idle resource has a known empty database or retained empty schema."""
        with cls.lock:
            return identity in cls.clean_databases

    @classmethod
    def claim(cls, lease: TestDatabaseLease) -> tuple[bool, KeptTestSchema | None]:
        """Atomically consume the clean marker and any schema for this acquisition.

        Stale tokens cannot consume another scope's state. A scope not creating a
        schema must reset a retained schema before using its database.
        """
        with cls.lock:
            current = cls.leases.get(lease.identity)
            if current is None or current.token is not lease.token:
                raise ConfigurationError(TEST_DATABASE_LEASE_EXPIRED)
            clean = lease.identity in cls.clean_databases
            cls.clean_databases.discard(lease.identity)
            schema = cls.kept_schemas.pop(lease.identity, None)
            if schema is not None and not lease.keep_schema:
                return False, None
            return clean, schema

    @classmethod
    def release(cls, lease: TestDatabaseLease, *, is_clean: bool, kept_schema: KeptTestSchema | None = None) -> None:
        """Release exactly this acquisition; stale or repeated releases do nothing."""
        with cls.lock:
            current = cls.leases.get(lease.identity)
            if current is None or current.token is not lease.token:
                return
            del cls.leases[lease.identity]
            cls.clean_databases.discard(lease.identity)
            cls.kept_schemas.pop(lease.identity, None)
            if is_clean or kept_schema is not None:
                cls.clean_databases.add(lease.identity)
            if kept_schema is not None:
                cls.kept_schemas[lease.identity] = kept_schema

    @classmethod
    def release_leases(cls, leases: Iterable[TestDatabaseLease]) -> None:
        """Release a scope's exact acquisitions, marking unfinished cleanup as dirty."""
        for lease in leases:
            cls.release(lease, is_clean=False)
