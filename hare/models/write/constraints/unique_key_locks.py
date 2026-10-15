from __future__ import annotations

import asyncio
from collections.abc import Hashable, Sequence
from typing import TYPE_CHECKING, ClassVar

from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.models.write.constraints.unique_checks import UniqueChecks

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class UniqueKeyLocks:
    """The values of a declared uniqueness this process is checking and writing - on a database keeping
    no uniqueness (``Features.checks_constraints_before_write``). A write holds the values of its rows
    from their check until its statements end, so two writes of one process never both pass the check
    of one value. A task holding a value takes it again without waiting; a write takes all its values
    at once, never some of them while waiting for the others."""

    #: The task holding each value - by connection, table, fields and the values' key.
    HOLDERS: ClassVar[dict[Hashable, asyncio.Task[object] | None]] = {}
    #: The futures of the writes waiting for a value, by the value.
    WAITERS: ClassVar[dict[Hashable, list[asyncio.Future[None]]]] = {}

    @staticmethod
    def get_keys(
        model: type[Model],
        connection: DatabaseClient,
        objs: Sequence[Model],
        changed_attribute_names: set[str] | None = None,
    ) -> set[Hashable]:
        """The values the objs hold in each declared uniqueness - but values unique by themselves (a
        NULL among them) and the objs a partial uniqueness doesn't hold for.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs written.
            changed_attribute_names: The attributes an update changes - only the uniqueness reading one
                of them; None for every uniqueness.

        Returns:
            The values, each with its connection, table and fields.
        """
        connection_alias = connection.connection_alias
        table = model._meta.db_table
        keys: set[Hashable] = set()
        for uniqueness in DeclaredUniqueness.get_declared(model):
            if changed_attribute_names is not None and not uniqueness.reads_any(changed_attribute_names):
                continue
            prefix = (connection_alias, table, uniqueness.attribute_names)
            checks_condition = uniqueness.condition is not None
            for obj in objs:
                if checks_condition and not uniqueness.holds_for(obj):
                    continue
                values = uniqueness.get_values(obj)
                if values is not None:
                    keys.add((*prefix, UniqueChecks.get_value_key(values)))
        return keys

    @classmethod
    async def acquire(
        cls,
        model: type[Model],
        connection: DatabaseClient,
        objs: Sequence[Model],
        changed_attribute_names: set[str] | None = None,
    ) -> list[Hashable]:
        """Takes the values the objs hold in each declared uniqueness, waiting while another task
        holds one of them.

        Args:
            model: The model.
            connection: The connection the rows are written on.
            objs: The objs written.
            changed_attribute_names: The attributes an update changes; None for new rows.

        Returns:
            The values taken - the ones the task held already left out - for ``release()``.
        """
        task = asyncio.current_task()
        holders = cls.HOLDERS
        keys = [
            key
            for key in cls.get_keys(model, connection, objs, changed_attribute_names)
            if key not in holders or holders[key] is not task
        ]
        while True:
            held_key = next((key for key in keys if key in holders), None)
            if held_key is None:
                break
            waiter = asyncio.get_running_loop().create_future()
            cls.WAITERS.setdefault(held_key, []).append(waiter)
            await waiter
        for key in keys:
            holders[key] = task
        return keys

    @classmethod
    def release(cls, keys: list[Hashable]) -> None:
        """Gives back values ``acquire()`` took, waking the writes waiting for them.

        Args:
            keys: The values.
        """
        for key in keys:
            cls.HOLDERS.pop(key, None)
            for waiter in cls.WAITERS.pop(key, ()):
                if not waiter.done() and not waiter.get_loop().is_closed():
                    waiter.set_result(None)
