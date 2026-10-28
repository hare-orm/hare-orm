from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.enums import RowOperation

if TYPE_CHECKING:
    import datetime

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.capture.capture_needs import CaptureNeeds
    from hare.instrumentation.capture.captured_change import CapturedChange
    from hare.models import Model


class CapturedWrite:
    """The capture of one write of a model with ``Meta.change_capture``: the changes of the rows it wrote, given
    to the model's sink in the write's transaction. The write runs its statements and finds what is its own - the
    keys of its rows, their values before it; the changes are built and captured here alike for every write.

    Args:
        model: The written model.
        needs: The model's needs.
        connection: The connection of the write.
        operation: What the write does to its rows.
        changed: The fields an update sets - the ones its caller named.
    """

    __slots__ = ("model", "needs", "connection", "operation", "changed", "occurred_at")

    def __init__(
        self,
        model: type[Model],
        needs: CaptureNeeds,
        connection: DatabaseClient,
        operation: RowOperation,
        *,
        changed: Iterable[str] | None = None,
    ) -> None:
        # Before the write's statements - a refusal of the sink leaves no row written without its changes.
        needs.sink.check_connection(connection)
        self.model = model
        self.needs = needs
        #: The connection the write runs on - its transaction once ``transaction()`` opened one.
        self.connection = connection
        self.operation = operation
        self.changed: tuple[str, ...] | None = None if changed is None else tuple(changed)
        #: When the write ran - the moment of its first change.
        self.occurred_at: datetime.datetime | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[DatabaseClient]:
        """The transaction the write and its changes share - the open one, else a new one around both; on a
        database without transactions, none.

        Yields:
            The connection to write on.
        """
        async with ChangeCapturing.transaction(self.connection) as transaction_connection:
            self.connection = transaction_connection
            yield transaction_connection

    async def read(self, keys: Any, *, lock: bool = False) -> dict[Any, tuple[dict[str, Any], Any]]:
        """Reads the captured fields of the write's rows - every tenant's, soft-deleted ones included.

        Args:
            keys: The rows' primary keys - a list, or a query of them.
            lock: Lock the rows until the transaction ends, where the database locks rows.

        Returns:
            The values and the tenant of each row found, by primary key.
        """
        if isinstance(keys, Sequence):
            return await ChangeCapturing.read_values(self.model, self.connection, keys, self.needs, lock=lock)
        return await ChangeCapturing.read_values_of(self.model, self.connection, keys, self.needs, lock=lock)

    async def read_before(self, keys: Any) -> dict[Any, dict[str, Any]]:
        """Reads the captured fields of the rows an update is about to write, locked until it ends.

        Args:
            keys: The rows' primary keys - a list, or a query of them.

        Returns:
            The values of each row found, by primary key.
        """
        return {pk: values for pk, (values, _tenant) in (await self.read(keys, lock=True)).items()}

    def get_returned_changes(
        self,
        raw_rows: Iterable[Any],
        *,
        operation: RowOperation | None = None,
        returns_old: bool = False,
        before_by_pk: Mapping[Any, Mapping[str, Any]] | None = None,
    ) -> list[CapturedChange]:
        """The changes of the rows the write returned - ``RETURNING`` of the captured columns.

        Args:
            raw_rows: The returned rows, by column.
            operation: What the write did to these rows - the write's operation by default.
            returns_old: The rows hold the values before the write too (``RETURNING OLD``).
            before_by_pk: The values before the write, read before it, by primary key.

        Returns:
            The changes, one per row.
        """
        return ChangeCapturing.get_returned_changes(
            self.model,
            self.needs,
            self.connection.dialect.types,
            raw_rows,
            operation or self.operation,
            changed=self.changed,
            returns_old=returns_old,
            before_by_pk=before_by_pk,
        )

    async def capture_returned(
        self,
        raw_rows: Iterable[Any],
        *,
        returns_old: bool = False,
        before_by_pk: Mapping[Any, Mapping[str, Any]] | None = None,
    ) -> list[CapturedChange]:
        """Captures the rows the write returned - ``RETURNING`` of the captured columns.

        Args:
            raw_rows: The returned rows, by column.
            returns_old: The rows hold the values before the write too (``RETURNING OLD``).
            before_by_pk: The values before the write, read before it, by primary key.

        Returns:
            The changes, one per row.
        """
        changes = self.get_returned_changes(raw_rows, returns_old=returns_old, before_by_pk=before_by_pk)
        await self.capture(changes)
        return changes

    async def capture_read(
        self, read_before: Mapping[Any, tuple[Mapping[str, Any], Any]], pks: Sequence[Any]
    ) -> list[CapturedChange]:
        """Captures the rows of a write on a database whose writes return no rows - as read before it and, for
        an update or an insert, read again after it.

        Args:
            read_before: The captured fields and the tenant of each row before the write, by primary key - empty
                for an insert.
            pks: The keys of the rows written.

        Returns:
            The changes, one per row.
        """
        read_after = (
            None
            if self.operation is RowOperation.DELETE
            else await ChangeCapturing.read_values(self.model, self.connection, pks, self.needs)
        )
        changes = ChangeCapturing.get_read_changes(
            self.model, self.needs, self.operation, read_before, read_after, changed=self.changed
        )
        await self.capture(changes)
        return changes

    def build_change(
        self,
        pk: Any,
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        tenant: Any = None,
        operation: RowOperation | None = None,
    ) -> CapturedChange:
        """The change of one row the write wrote - every change of the write at one moment.

        Args:
            pk: The row's primary key.
            before: The captured fields before the write, None when not read.
            after: The captured fields after it, None when not read.
            tenant: The row's tenant.
            operation: What the write did to the row - the write's operation by default.

        Returns:
            The change.
        """
        change = ChangeCapturing.build_change(
            self.model,
            self.needs,
            operation or self.operation,
            pk,
            changed=self.changed,
            before=before,
            after=after,
            tenant=tenant,
            occurred_at=self.occurred_at,
        )
        self.occurred_at = change.occurred_at
        return change

    async def capture(self, changes: list[CapturedChange]) -> None:
        """Gives changes of the write to the model's sink - those of a captured operation.

        Args:
            changes: The changes.
        """
        await ChangeCapturing.capture(self.connection, self.model, changes)

    async def capture_values(
        self, rows: Iterable[tuple[Any, Mapping[str, Any] | None, Mapping[str, Any] | None, Any]]
    ) -> list[CapturedChange]:
        """Captures rows whose values the write knows itself - from the objects it wrote, or read.

        Args:
            rows: ``(primary key, values before, values after, tenant)`` of each row.

        Returns:
            The changes, one per row.
        """
        changes = [
            self.build_change(pk, before=before, after=after, tenant=tenant) for pk, before, after, tenant in rows
        ]
        await self.capture(changes)
        return changes
