from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Coroutine, Iterable, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.instrumentation.change_events import ChangeEvents

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.enums import RowOperation
    from hare.models import Model

TResult = TypeVar("TResult")


class WriteSteps:
    """The checks and reports every write of instances runs."""

    #: True inside the task ``run_whole()`` runs a write in - a write nested in it runs in that task.
    IN_WHOLE_RUN: ClassVar[contextvars.ContextVar[bool]] = contextvars.ContextVar("hare_in_whole_run", default=False)

    @staticmethod
    def get_whole_run(connection: DatabaseClient, write: Coroutine[Any, Any, TResult]) -> Coroutine[Any, Any, TResult]:
        """A write of several steps - its statements and the report of its rows - as one a cancel can't
        split: on a database without transactions run to its end (``run_whole()``), elsewhere the write
        itself, without a coroutine more.

        Args:
            connection: The connection of the write.
            write: The write.

        Returns:
            What to await.
        """
        if connection.features.supports_transactions:
            return write
        return WriteSteps.run_whole(connection, write)

    @staticmethod
    async def run_whole(connection: DatabaseClient, write: Coroutine[Any, Any, TResult]) -> TResult:
        """Runs a write of several steps to its end on a database without transactions, even when the
        caller is cancelled meanwhile - a cancel between the steps would leave rows changed that no
        step after them reports, captures or cascades from. The cancel is raised once the write ends.

        Args:
            connection: The connection of the write.
            write: The write.

        Returns:
            What the write gives.
        """
        if connection.features.supports_transactions or WriteSteps.IN_WHOLE_RUN.get():
            return await write
        context = contextvars.copy_context()
        context.run(WriteSteps.IN_WHOLE_RUN.set, True)
        task = asyncio.get_running_loop().create_task(write, context=context)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.wait([task])
            if not task.cancelled():
                # Read, so it isn't logged as never retrieved - the caller's cancel is what is raised.
                task.exception()
            raise

    @staticmethod
    async def report(
        connection: DatabaseClient,
        model: type[Model],
        operation: RowOperation,
        *,
        instances: Sequence[Model] | None = None,
        pks: Sequence[Any] | None = None,
        fields: Iterable[str] | None = None,
    ) -> None:
        """Reports rows a write changed (``ChangeEvents``) - nothing without a listener.

        Args:
            connection: The connection the write ran on.
            model: The model.
            operation: The operation.
            instances: The written instances - their primary keys name the rows.
            pks: The primary keys of the rows, instead of ``instances``.
            fields: The fields written, None when not known.
        """
        if not ChangeEvents.is_observed(model):
            return
        if instances is not None:
            if not instances:
                return
            pks = (
                [instance.pk for instance in instances]
                if model._meta.has_primary_key and all(instance.pk is not None for instance in instances)
                else None
            )
        await ChangeEvents.report(connection, model, operation, pks=pks, fields=fields)
