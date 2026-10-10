"""The rows the ORM's writes change, reported as ``RowsChanged`` events once the write's transaction
commits - never for a rolled back write. Every ORM write reports - saves, deletes, restores,
``update()``, bulk writes, what ``on_delete`` reaches, many-to-many links; raw SQL doesn't. A delete
reports itself and what it reaches once.
"""

from __future__ import annotations

import functools
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.dialects.base.client.transaction_lifecycle.transaction_callbacks import TransactionCallbacks
from hare.fields.enums import OnDelete
from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.enums import RowOperation
from hare.instrumentation.observers.observers import Observers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.models import Model


class ChangeEvents:
    """Reports the rows the ORM's writes change. An observer of ``RowsChanged`` narrowed to models
    gets the events of those models and of their subclasses (an observer of an abstract base model
    gets every model built on it). It runs once the transaction of the write commits - right after
    the write outside a transaction - in the task that committed it; it may query the database, and
    the writes it makes report their own events. Without observers the writes pay nothing.
    """

    #: True while a write that reports its changes as a whole runs - a delete with its cascade;
    #: the writes it makes on the way report nothing of their own.
    reported_as_a_whole: ClassVar[ContextVar[bool]] = ContextVar("hare_change_events_whole_write", default=False)

    @staticmethod
    def is_observed(model: type[Model] | None = None) -> bool:
        """Whether an observer gets the changes of ``model`` - of any model without it.

        Args:
            model: The model.

        Returns:
            True when such an observer exists.
        """
        return Observers.is_observed(RowsChanged, model)

    @classmethod
    @contextmanager
    def reporting_as_a_whole(cls) -> Generator[None]:
        """Keeps the writes made inside the block from reporting - the write running it reports
        every change once, after the block."""
        token = cls.reported_as_a_whole.set(True)
        try:
            yield
        finally:
            cls.reported_as_a_whole.reset(token)

    @classmethod
    async def report(
        cls,
        client: DatabaseClient,
        model: type[Model],
        operation: RowOperation,
        *,
        pks: Iterable[Any] | None = None,
        fields: Iterable[str] | None = None,
    ) -> None:
        """Reports rows a write changed - delivered once its transaction commits.

        Args:
            client: The connection the write ran on - a transaction's client inside one.
            model: The model.
            operation: What the write did.
            pks: The primary keys of the rows, None when it names no rows.
            fields: The fields it set, None when not known.
        """
        if cls.reported_as_a_whole.get() or not Observers.is_observed(RowsChanged):
            return
        await cls.deliver(client, [cls.build_event(client, model, operation, pks, fields)])

    @classmethod
    async def report_deletion(
        cls, client: DatabaseClient, model: type[Model], *, pks: Iterable[Any] | None = None, soft: bool = False
    ) -> None:
        """Reports deleted rows and what their ``on_delete`` reaches: ``CASCADE`` rows deleted too
        (soft deleted, for a soft delete of a model with ``Meta.soft_delete_field``),
        ``SET_NULL``/``SET_DEFAULT`` rows and the related rows of many-to-many links updated.

        Args:
            client: The connection the delete ran on.
            model: The model the rows were deleted from.
            pks: The primary keys, None when it names no rows.
            soft: A soft delete - the rows are updated, not removed.
        """
        if cls.reported_as_a_whole.get() or not Observers.is_observed(RowsChanged):
            return
        # Deferred: the deletion helpers import the models, which import this module.
        from hare.models.deletion.cascade.deletion_graph import DeletionGraph

        deletion = RowOperation.UPDATE if soft else RowOperation.DELETE
        soft_delete_fields = (model._meta.soft_delete_field,) if soft and model._meta.soft_delete_field else None
        events = [cls.build_event(client, model, deletion, pks, soft_delete_fields)]
        reached_models = DeletionGraph.get_cascade_models(model)
        for cascade_model in reached_models[1:]:
            cascade_soft_delete_field = cascade_model._meta.soft_delete_field if soft else None
            if cascade_soft_delete_field:
                events.append(
                    cls.build_event(client, cascade_model, RowOperation.UPDATE, None, (cascade_soft_delete_field,))
                )
            else:
                # A soft delete really deletes the rows of a model without soft delete.
                events.append(cls.build_event(client, cascade_model, RowOperation.DELETE, None, None))
        for reached_model in reached_models:
            for backward_field, foreign_key in DeletionGraph.get_backward_relations(reached_model):
                if foreign_key.on_delete in {OnDelete.SET_NULL, OnDelete.SET_DEFAULT}:
                    events.append(
                        cls.build_event(
                            client,
                            backward_field.related_model,
                            RowOperation.UPDATE,
                            None,
                            (foreign_key.model_field_name,),
                        )
                    )
            for relation_name in sorted(reached_model._meta.many_to_many_fields):
                relation = cast("ManyToManyFieldInstance[Any]", reached_model._meta.fields_map[relation_name])
                related_name = getattr(relation, "related_name", None)
                events.append(
                    cls.build_event(
                        client,
                        relation.related_model,
                        RowOperation.UPDATE,
                        None,
                        (related_name,) if related_name else None,
                    )
                )
        await cls.deliver(client, events)

    @staticmethod
    def build_event(
        client: DatabaseClient,
        model: type[Model],
        operation: RowOperation,
        pks: Iterable[Any] | None,
        fields: Iterable[str] | None,
    ) -> RowsChanged:
        """An event.

        Args:
            client: The connection the write ran on.
            model: The model.
            operation: What the write did.
            pks: The primary keys, None when not known.
            fields: The fields set, None when not known.

        Returns:
            The event.
        """
        return RowsChanged(
            model=model,
            operation=operation,
            pks=None if pks is None else tuple(pks),
            fields=None if fields is None else tuple(fields),
            connection_alias=client.connection_alias,
        )

    @classmethod
    async def deliver(cls, client: DatabaseClient, events: list[RowsChanged]) -> None:
        """Delivers events once the transaction of ``client`` commits - dropped with it when it
        rolls back, a savepoint's with the savepoint - or right away outside a transaction.

        Args:
            client: The connection the write ran on.
            events: The events.
        """
        # Deferred: the client module imports the instrumentation package.
        from hare.dialects.base.client.transaction_client import TransactionClient

        events = [event for event in events if Observers.is_observed(RowsChanged, event.model)]
        if not events:
            return
        if isinstance(client, TransactionClient) and not client._is_transaction_finished():
            TransactionCallbacks.add_on_commit_callback(client, functools.partial(cls.dispatch, events))
            return
        await cls.dispatch(events)

    @classmethod
    async def dispatch(cls, events: list[RowsChanged]) -> None:
        """Gives each event to its observers.

        Args:
            events: The events.
        """
        # The writes an observer makes report their own events.
        token = cls.reported_as_a_whole.set(False)
        try:
            for event in events:
                await Observers.deliver(event)
        finally:
            cls.reported_as_a_whole.reset(token)
