from __future__ import annotations

from collections.abc import Generator, Sequence
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import UnSupportedError
from hare.instrumentation.change_events import ChangeEvents
from hare.models.deletion.cascade.cascade_deletion import CascadeDeletion
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral
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

    def _get_call_signature_type_part(self) -> tuple[Any, list[Any]]:
        return self.returned_rows.get_call_signature_type_part(super()._get_call_signature_type_part())

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        keeps_plan = super()._build_statement(value_wrapper_references, records_for_caller=records_for_caller)
        self.query = self.query.returning(*self.returned_rows.get_returning_columns())
        return keeps_plan

    def __await__(self) -> Generator[Any, None, list[Any]]:  # type: ignore[override]
        if self._is_none:
            return self._return_none().__await__()
        query = self._get_execution_query(True)
        features = query._connection.features
        # A soft delete, a cascade hare runs and an overridden delete read the rows in their
        # transaction on every database.
        if not features.supports_returning and not (
            query._deletes_softly()
            or DeletionGraph.needs_python_cascade(query.model)
            or query._has_overridden_delete()
        ):
            raise UnSupportedError(
                f"delete().returning() needs RETURNING, which {query._connection.dialect} doesn't have"
            )
        if not features.supports_returning and not query.model._meta.has_primary_key:
            raise UnSupportedError(
                f"delete().returning() of {query.model.__name__} reads the rows by their keys on "
                f"{query._connection.dialect} "
                "- the model has no primary key"
            )
        return query._execute_returning().__await__()

    async def _return_none(self) -> list[Any]:
        return []

    def _returns_from_statement(self) -> bool:
        """Whether the rows come from one ``DELETE ... RETURNING`` - a hard delete the database
        carries out alone, without an overridden delete, returning named fields or the instances of a
        model without a primary key."""
        return (
            self._connection.features.supports_returning
            and (not self.returned_rows.returns_instances or self.returned_rows.builds_instances_from_rows)
            and not self._deletes_softly()
            and not DeletionGraph.needs_python_cascade(self.model)
            and not self._has_overridden_delete()
            and not (
                self._connection.features.cascade_depth_limit is not None
                and DeletionGraph.has_self_cascading_constrained_relations(self.model)
            )
        )

    async def _execute_returning(self) -> list[Any]:
        """Runs the delete and reads the rows it deleted.

        Returns:
            The rows - in the order the database returned them, else in the order the delete
            matched them.
        """
        if self._returns_from_statement():
            with ChangeEvents.reporting_as_a_whole():
                count, raw_rows = await self._run_statement_returning()
            await self._report_deleted_rows(count)
            if self.returned_rows.builds_instances_from_rows:
                return await self.returned_rows.get_instances(self._connection, raw_rows)
            return self.returned_rows.get_rows(self._connection.dialect.types, raw_rows)
        soft_deletes = self._deletes_softly()
        if soft_deletes and not self.model._meta.has_primary_key:
            return await self._soft_delete_returning()
        async with self._connection._in_transaction() as transaction_connection:
            deleting_query = self._get_deleting_query(transaction_connection)
            # Without include_deleted() the default scope already leaves the deleted rows out.
            keys = await deleting_query._get_matching_pks(live_only=soft_deletes and self._visibility.include_deleted)
            rows = (
                [] if soft_deletes else await self.returned_rows.read(transaction_connection, self._visibility, keys)
            )
            # Reports the delete itself.
            await deleting_query
            if soft_deletes:
                rows = await self.returned_rows.read(transaction_connection, self._visibility, keys)
        return rows

    async def _soft_delete_returning(self) -> list[Any]:
        """Runs the soft delete of a model without a primary key - its ``UPDATE ... RETURNING`` returning
        the rows, as there is no key to read them back by.

        Returns:
            The rows, as they are after the delete.
        """
        from hare.query.statements.write.returning.update_returning_query import UpdateReturningQuery

        soft_delete_field = cast("str", self.model._meta.soft_delete_field)
        async with self._connection._in_transaction() as transaction_connection:
            deleting_query = self._get_deleting_query(transaction_connection)
            soft_deleting_query = UpdateReturningQuery(
                deleting_query._get_live_rows() if self._visibility.include_deleted else deleting_query,
                {soft_delete_field: CascadeDeletion.get_deleted_at()},
                self.returned_rows,
                WriteType.SOFT_DELETE,
            )
            soft_deleting_query._apply_connection(transaction_connection)
            soft_deleting_query._connection_explicitly_chosen = True
            with ChangeEvents.reporting_as_a_whole():
                rows = await soft_deleting_query
            await deleting_query._report_deleted_rows(len(rows))
        return rows

    def _get_deleting_query(self, connection: DatabaseClient) -> DeleteQuery:
        """This delete without ``returning()``, run on ``connection``.

        Args:
            connection: The connection - the transaction the rows are read in.

        Returns:
            The delete.
        """
        from hare.query.statements.write.declarations import HardDeleteQuery

        deleting_query = HardDeleteQuery(self) if self.deletes_permanently else DeleteQuery(self)
        deleting_query._apply_connection(connection)
        deleting_query._connection_explicitly_chosen = True
        return deleting_query

    async def _run_statement_returning(self) -> tuple[int | None, Sequence[Any]]:
        """Runs the built ``DELETE ... RETURNING`` - after the ``PROTECT`` check, with the deferred
        constraints a ``PROTECT`` needs.

        Returns:
            The number of deleted rows and the returned rows.
        """
        self._make_query_to_run()
        if self._has_protected_relations():
            await self._check_protected(await self._get_matching_pks())
        sql, parameters = self._get_parameterized_sql()
        if ProtectConstraintDeferral.is_needed(self.model, self._connection):
            async with (
                self._connection._in_transaction() as transaction_connection,
                ProtectConstraintDeferral.defer(self.model, transaction_connection),
            ):
                return await transaction_connection.execute(sql, parameters, returns_rows=True)
        return await self._connection.execute(sql, parameters, returns_rows=True)
