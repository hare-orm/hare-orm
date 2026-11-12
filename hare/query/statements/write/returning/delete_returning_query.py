from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import UnSupportedError
from hare.instrumentation.change_events import ChangeEvents
from hare.models.deletion.cascade.cascade_deletion import CascadeDeletion
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral
from hare.models.deletion.deletion_routes import DeletionRoutes
from hare.models.deletion.enums import DeletionRoute
from hare.models.write.rules.enums import WriteType
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.statements.write.delete_query import DeleteQuery

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.statements.write.returning.returned_rows import ReturnedRows


class DeleteReturningQuery(DeleteQuery):
    """``delete().returning(...)``: the delete, returning each row it deleted - a dict of the named
    fields, or the model instance when none is named. A plain ``DELETE`` returns the named fields
    through its ``RETURNING``; a delete carried out in Python (a cascade, a soft delete) reads the
    rows in its transaction - a hard-deleted row before the delete, a soft-deleted one after it.
    """

    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *DeleteQuery.plan_slots,
        ("returned_rows.plan_key", PlanKeyForm.VALUE),
    )

    __slots__ = ("returned_rows",)

    def __init__(self, source: QuerySpecification[Any], returned_rows: ReturnedRows) -> None:
        """
        Args:
            source: The delete or queryset whose rows are deleted.
            returned_rows: What is returned for each row.
        """
        super().__init__(source)
        self.returned_rows = returned_rows

    def _get_call_signature_type_part(self) -> tuple[Any, Sequence[Any]]:
        return self.returned_rows.get_call_signature_type_part(super()._get_call_signature_type_part())

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        keeps_plan = super()._build_statement(value_wrapper_references, records_for_caller=records_for_caller)
        self.query = self.query.returning(*self.returned_rows.get_returning_columns())
        return keeps_plan

    async def _execute_bound(self) -> list[Any]:
        if self._is_none:
            return await self._return_none()
        connection = self.get_connection(for_write=True)
        features = connection.features
        route = DeletionRoutes.get(self.model, features, self.deletes_permanently)
        # A soft delete, a cascade hare runs and an overridden delete read the rows in their
        # transaction on every database.
        if not features.supports_returning and not (
            route in (DeletionRoute.SOFT_UPDATE, DeletionRoute.SOFT_CASCADE, DeletionRoute.PYTHON_CASCADE)
            or self._has_overridden_delete()
        ):
            raise UnSupportedError(f"delete().returning() needs RETURNING, which {connection.dialect} doesn't have")
        if not features.supports_returning and not self.model._meta.has_primary_key:
            raise UnSupportedError(
                f"delete().returning() of {self.model.__name__} reads the rows by their keys on "
                f"{connection.dialect} "
                "- the model has no primary key"
            )
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        return await self._execute_returning(connection)

    async def _return_none(self) -> list[Any]:
        return []

    def _returns_from_statement(self, connection: DatabaseClient) -> bool:
        """Whether the rows come from one ``DELETE ... RETURNING`` - a hard delete the database
        carries out alone, without an overridden delete, returning named fields or the instances of a
        model without a primary key."""
        return (
            connection.features.supports_returning
            and (not self.returned_rows.returns_instances or self.returned_rows.builds_instances_from_rows)
            and not self._has_overridden_delete()
            and DeletionRoutes.get(self.model, connection.features, self.deletes_permanently)
            in (DeletionRoute.STATEMENT, DeletionRoute.PROTECT_DEFERRED)
        )

    async def _execute_returning(self, connection: DatabaseClient) -> list[Any]:
        """Runs the delete and reads the rows it deleted.

        Returns:
            The rows - in the order the database returned them, else in the order the delete
            matched them.
        """
        if self._returns_from_statement(connection):
            with ChangeEvents.reporting_as_a_whole():
                count, raw_rows = await self._run_statement_returning(connection)
            await self._report_deleted_rows(count, connection)
            if self.returned_rows.builds_instances_from_rows:
                return await self.returned_rows.get_instances(connection, raw_rows)
            return self.returned_rows.get_rows(connection.dialect.types, raw_rows)
        soft_deletes = self._deletes_softly()
        visibility = self._visibility.get_for_active_tenant()
        if soft_deletes and not self.model._meta.has_primary_key:
            return await self._soft_delete_returning(connection)
        async with connection._in_transaction() as transaction_connection:
            deleting_query = self._get_deleting_query(transaction_connection)
            # Without include_deleted() the default scope already leaves the deleted rows out.
            keys = await deleting_query._get_matching_pks(
                transaction_connection, live_only=soft_deletes and self._visibility.include_deleted
            )
            rows = [] if soft_deletes else await self.returned_rows.read(transaction_connection, visibility, keys)
            # Reports the delete itself.
            await deleting_query
            if soft_deletes:
                rows = await self.returned_rows.read(transaction_connection, visibility, keys)
        return rows

    async def _soft_delete_returning(self, connection: DatabaseClient) -> list[Any]:
        """Runs the soft delete of a model without a primary key - its ``UPDATE ... RETURNING`` returning
        the rows, as there is no key to read them back by.

        Returns:
            The rows, as they are after the delete.
        """
        from hare.query.statements.write.returning.update_returning_query import UpdateReturningQuery

        soft_delete_field: str = self.model._meta.soft_delete_field
        async with connection._in_transaction() as transaction_connection:
            deleting_query = self._get_deleting_query(transaction_connection)
            soft_deleting_query = UpdateReturningQuery(
                deleting_query._get_live_rows(transaction_connection)
                if self._visibility.include_deleted
                else deleting_query,
                {soft_delete_field: CascadeDeletion.get_deleted_at()},
                self.returned_rows,
                WriteType.SOFT_DELETE,
            )
            with ChangeEvents.reporting_as_a_whole():
                rows = await soft_deleting_query
            await deleting_query._report_deleted_rows(len(rows), transaction_connection)
        return rows

    def _get_deleting_query(self, connection: DatabaseClient) -> DeleteQuery:
        """This delete without ``returning()``, run on ``connection``.

        Args:
            connection: The connection - the transaction the rows are read in.

        Returns:
            The delete.
        """
        from hare.query.statements.write.declarations import HardDeleteQuery

        source = self._matching_queryset(connection)
        return HardDeleteQuery(source) if self.deletes_permanently else DeleteQuery(source)

    async def _run_statement_returning(self, connection: DatabaseClient) -> tuple[int | None, Sequence[Any]]:
        """Runs the built ``DELETE ... RETURNING`` - after the ``PROTECT`` check, with the deferred
        constraints a ``PROTECT`` needs.

        Returns:
            The number of deleted rows and the returned rows.
        """
        if self._has_protected_relations():
            deleting_query = self._get_deleting_query(connection)
            await deleting_query._check_protected(await deleting_query._get_matching_pks(connection), connection)
        if ProtectConstraintDeferral.is_needed(self.model, connection):
            async with (
                connection._in_transaction() as transaction_connection,
                ProtectConstraintDeferral.defer(self.model, transaction_connection),
            ):
                return await self._run_statement_on(transaction_connection, returns_rows=True)
        return await self._run_statement_on(connection, returns_rows=True)
