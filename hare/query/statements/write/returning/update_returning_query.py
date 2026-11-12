from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import UnSupportedError
from hare.instrumentation.enums import RowOperation
from hare.models.write.rules.enums import WriteType
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.statements.write.update_query import UpdateQuery

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.execution.prepared_write import PreparedWrite
    from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.statements.write.returning.returned_rows import ReturnedRows


class UpdateReturningQuery(UpdateQuery):
    """``update(...).returning(...)``: the update, returning each row it wrote - as ``UPDATE ...
    RETURNING``: a dict of the named fields, or the model instance when none is named.
    """

    plan_slots: ClassVar[DeclaredPlanSlots] = (
        *UpdateQuery.plan_slots,
        ("returned_rows.plan_key", PlanKeyForm.VALUE),
    )

    __slots__ = ("returned_rows",)

    def __init__(
        self,
        source: QuerySpecification[Any],
        update_kwargs: dict[str, Any],
        returned_rows: ReturnedRows,
        write_type: WriteType = WriteType.UPDATE,
    ) -> None:
        """
        Args:
            source: The update or queryset whose rows are updated.
            update_kwargs: The values to set, by field name.
            returned_rows: What is returned for each row.
            write_type: See ``UpdateQuery``.
        """
        super().__init__(source, update_kwargs, write_type)
        self.returned_rows = returned_rows

    def _get_call_signature_type_part(self, prepared: PreparedWrite | None = None) -> tuple[Any, Sequence[Any]]:
        return self.returned_rows.get_call_signature_type_part(super()._get_call_signature_type_part(prepared))

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        keeps_plan = super()._build_statement(value_wrapper_references, records_for_caller=records_for_caller)
        self.query = self.query.returning(
            *self.returned_rows.get_returning_columns(), *self.returned_rows.get_old_value_terms()
        )
        return keeps_plan

    async def _execute_bound(self) -> list[Any]:
        if self._is_none:
            return await self._return_none()
        connection = self.get_connection(for_write=True)
        if not connection.features.supports_returning:
            raise UnSupportedError(f"update().returning() needs RETURNING, which {connection.dialect} doesn't have")
        if self.returned_rows.old_source_names_by_name and not connection.features.supports_returning_old_new:
            raise UnSupportedError(
                "update().returning(old=...) needs RETURNING of the old values, which the "
                f"{connection.dialect} "
                "server of this connection doesn't have"
            )
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        return await self._execute_returning(connection, self._make_prepared_write(connection.dialect))

    async def _return_none(self) -> list[Any]:
        return []

    async def _execute_returning(self, connection: DatabaseClient, prepared: PreparedWrite) -> list[Any]:
        """Runs the update - the instances read back in its transaction - and reports it.

        Returns:
            The returned rows, in the order the database returned them.
        """
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and not capture_needs.captures(RowOperation.UPDATE):
            capture_needs = None
        if not self.returned_rows.returns_instances and capture_needs is None:
            count, raw_rows = await self._run_on(connection, prepared, returns_rows=True)
            rows: list[Any] = self.returned_rows.get_rows(connection.dialect.types, raw_rows)
        elif not self.returned_rows.returns_instances:
            captured = self._get_captured_write(capture_needs, connection)
            async with captured.transaction():
                count, raw_rows = await self._run_captured(captured, prepared)
            rows = self.returned_rows.get_rows(connection.dialect.types, raw_rows)
        elif self.returned_rows.builds_instances_from_rows:
            count, raw_rows = await self._run_on(connection, prepared, returns_rows=True)
            rows = await self.returned_rows.get_instances(connection, raw_rows)
        else:
            async with connection._in_transaction() as transaction_connection:
                if capture_needs is not None:
                    count, raw_rows = await self._run_captured(
                        self._get_captured_write(capture_needs, transaction_connection), prepared
                    )
                else:
                    count, raw_rows = await self._run_on(transaction_connection, prepared, returns_rows=True)
                keys = self.returned_rows.get_primary_keys(transaction_connection.dialect.types, raw_rows)
                rows = await self.returned_rows.read(
                    transaction_connection, self._visibility.get_for_active_tenant(), keys
                )
        await self._report_updated_rows(count, connection)
        return rows
