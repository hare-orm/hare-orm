"""Per-process pool of test databases of a server that are reset and reused instead of dropped."""

from __future__ import annotations

import itertools
import os
import threading
import uuid
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from typing import TYPE_CHECKING

from hare.contrib.test.constants import (
    REUSABLE_DATABASE_SLOT_NAMESPACE,
    REUSE_DATABASES_DISABLED_VALUES,
    REUSE_DATABASES_ENABLED_VALUES,
    REUSE_DATABASES_ENVIRONMENT_VARIABLE,
)
from hare.core.constants import ENV_PYTEST_XDIST_WORKER
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.contrib.test.databases.kept_test_schema import KeptTestSchema


class ReusableTestDatabases:
    """Leases test databases of a server out of a per-process pool of slots.

    A slot is a database whose name is the test URL's "{}" placeholder filled with a stable id of
    the slot's number. A leased slot belongs to one hare_test_context() until its db_delete()
    resets the database's contents and releases the slot, so the next context takes the same,
    already-existing database instead of paying for CREATE DATABASE and DROP DATABASE again. A new
    slot is only added when every existing one is leased (nested contexts, several connections in
    one config).
    """

    #: Lease number of every currently leased database, by database name.
    lease_number_by_database_name: dict[str, int] = {}
    #: Databases known to be reset to an empty state and not touched since.
    clean_database_names: set[str] = set()
    #: Databases holding a schema their last lease created and left unchanged, with every row
    #: cleared - the next lease asking for the same schema skips creating it.
    kept_schema_by_database_name: dict[str, KeptTestSchema] = {}
    #: Leases whose context creates its models' schema - only they may take a kept one.
    schema_creating_lease_numbers: set[int] = set()
    #: Last lease number handed out - lease numbers only ever grow.
    last_lease_number: int = 0
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
        """The 32-hex id filling the database name template for a slot.

        Args:
            slot_number: The slot's number, from 0.

        Returns:
            An id that depends only on the slot number and the pytest-xdist worker.
        """
        xdist_worker_id = os.environ.get(ENV_PYTEST_XDIST_WORKER, "")
        return uuid.uuid5(REUSABLE_DATABASE_SLOT_NAMESPACE, f"{xdist_worker_id}:{slot_number}").hex

    @classmethod
    def acquire(cls, database_name_template: str) -> tuple[str, int]:
        """Leases the first free slot of a database name template, adding a slot when all are leased.

        Args:
            database_name_template: The database name with one "{}" placeholder.

        Returns:
            The leased database name and its lease number.
        """
        with cls.lock:
            for slot_number in itertools.count():
                database_name = database_name_template.format(cls.get_slot_id(slot_number))
                if database_name not in cls.lease_number_by_database_name:
                    cls.last_lease_number += 1
                    cls.lease_number_by_database_name[database_name] = cls.last_lease_number
                    return database_name, cls.last_lease_number
        raise AssertionError("unreachable")

    @classmethod
    def is_leased(cls, database_name: str, lease_number: int) -> bool:
        """Whether a lease is still held.

        Args:
            database_name: The leased database's name.
            lease_number: The lease number acquire() returned.

        Returns:
            True while that lease has not been released.
        """
        return cls.lease_number_by_database_name.get(database_name) == lease_number

    @classmethod
    def is_clean(cls, database_name: str) -> bool:
        """Whether a database was reset by this process and not used since.

        Args:
            database_name: The database's name.

        Returns:
            True if its next user can skip resetting it.
        """
        return database_name in cls.clean_database_names

    @classmethod
    def mark_used(cls, database_name: str) -> None:
        """Records that a database may now hold data.

        Args:
            database_name: The database's name.
        """
        with cls.lock:
            cls.clean_database_names.discard(database_name)
            cls.kept_schema_by_database_name.pop(database_name, None)

    @classmethod
    def allow_kept_schemas(cls, lease_numbers: Iterable[int]) -> None:
        """Lets leases take the schema their database kept - for a context creating its models' schema,
        which skips creating one it finds kept.

        Args:
            lease_numbers: The leases.
        """
        with cls.lock:
            cls.schema_creating_lease_numbers.update(lease_numbers)

    @classmethod
    def may_keep_schema(cls, lease_number: int) -> bool:
        """Whether a lease's database may keep the schema its context creates.

        Args:
            lease_number: The lease number acquire() returned.

        Returns:
            True for a lease of a context creating its models' schema first thing.
        """
        return lease_number in cls.schema_creating_lease_numbers

    @classmethod
    def take_kept_schema(cls, database_name: str, lease_number: int) -> KeptTestSchema | None:
        """The schema a leased database kept, for a lease allowed to take it. The database counts as
        holding data from now on either way: a lease not allowed one resets it.

        Args:
            database_name: The leased database's name.
            lease_number: The lease number acquire() returned.

        Returns:
            The kept schema, or None.
        """
        with cls.lock:
            kept_schema = cls.kept_schema_by_database_name.pop(database_name, None)
            if lease_number not in cls.schema_creating_lease_numbers:
                if kept_schema is not None:
                    cls.clean_database_names.discard(database_name)
                return None
            return kept_schema

    @classmethod
    def release(
        cls, database_name: str, lease_number: int, *, is_clean: bool, kept_schema: KeptTestSchema | None = None
    ) -> None:
        """Returns a leased slot to the pool; a no-op when that lease was already released.

        Args:
            database_name: The leased database's name.
            lease_number: The lease number acquire() returned.
            is_clean: Whether the database was just reset, so its next user can skip resetting it.
            kept_schema: The schema the database keeps, its rows cleared - None when it keeps none.
        """
        with cls.lock:
            if cls.lease_number_by_database_name.get(database_name) != lease_number:
                return
            del cls.lease_number_by_database_name[database_name]
            cls.schema_creating_lease_numbers.discard(lease_number)
            if is_clean:
                cls.clean_database_names.add(database_name)
            else:
                cls.clean_database_names.discard(database_name)
            if kept_schema is None:
                cls.kept_schema_by_database_name.pop(database_name, None)
            else:
                cls.kept_schema_by_database_name[database_name] = kept_schema

    @classmethod
    @contextmanager
    def track_new_leases(cls) -> Generator[dict[str, int]]:
        """Collects the leases acquired while the block runs and still held when it ends.

        Returns:
            A dict filled on exit with the lease number of every such database, by name.
        """
        first_lease_number = cls.last_lease_number + 1
        new_lease_number_by_database_name: dict[str, int] = {}
        try:
            yield new_lease_number_by_database_name
        finally:
            with cls.lock:
                new_lease_number_by_database_name.update(
                    (database_name, lease_number)
                    for database_name, lease_number in cls.lease_number_by_database_name.items()
                    if lease_number >= first_lease_number
                )

    @classmethod
    def release_leases(cls, lease_number_by_database_name: dict[str, int]) -> None:
        """Releases every lease still held, keeping the databases marked as needing a reset.

        Args:
            lease_number_by_database_name: Leases collected by track_new_leases().
        """
        for database_name, lease_number in lease_number_by_database_name.items():
            cls.release(database_name, lease_number, is_clean=False)
