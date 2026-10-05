from __future__ import annotations

from collections.abc import Generator
from typing import TYPE_CHECKING, Any, ClassVar

from hare.exceptions import UnSupportedError
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.enums import RowOperation
from hare.query.plans.enums import PlanKeyForm
from hare.query.plans.statement.declared_plan_slots import DeclaredPlanSlots
from hare.query.statements.write.update_query import UpdateQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
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
    subquery_plan_slots: ClassVar[DeclaredPlanSlots] = (
        *UpdateQuery.subquery_plan_slots,
        ("returned_rows.plan_key", PlanKeyForm.VALUE),
    )

    __slots__ = ("returned_rows",)

    def __init__(
        self, source: QuerySpecification[Any], update_kwargs: dict[str, Any], returned_rows: ReturnedRows
    ) -> None:
        """
        Args:
            source: The update or queryset whose rows are updated.
            update_kwargs: The values to set, by field name.
            returned_rows: What is returned for each row.
        """
        super().__init__(source, update_kwargs)
        self.returned_rows = returned_rows

    def _get_call_signature_type_part(self) -> tuple[Any, list[Any]]:
        return self.returned_rows.get_call_signature_type_part(super()._get_call_signature_type_part())

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        super()._build_statement(value_wrapper_references, records_for_caller=records_for_caller)
        self.query = self.query.returning(
            *self.returned_rows.get_returning_columns(), *self.returned_rows.get_old_value_terms()
        )
        return True

    def __await__(self) -> Generator[Any, None, list[Any]]:  # type: ignore[override]
        if self._is_none or self._limit == 0:
            return self._return_none().__await__()
        query = self._get_execution_query(True)
        features = query._connection.features
        if not features.supports_returning:
            if not features.returns_rows_by_reading or self.returned_rows.old_source_names_by_name:
                raise UnSupportedError(
                    f"update().returning() needs RETURNING, which {query._connection.dialect} doesn't have"
                )
            return query._execute_returning_by_reading().__await__()
        if self.returned_rows.old_source_names_by_name and not query._connection.features.supports_returning_old_new:
            raise UnSupportedError(
                "update().returning(old=...) needs RETURNING of the old values, which the "
                f"{query._connection.dialect} "
                "server of this connection doesn't have"
            )
        return query._execute_returning().__await__()

    def _get_query_on(self, connection: DatabaseClient) -> UpdateReturningQuery:
        query = UpdateReturningQuery(self, self.update_kwargs, self.returned_rows)
        query._apply_connection(connection)
        query._connection_explicitly_chosen = True
        return query

    async def _return_none(self) -> list[Any]:
        return []

    async def _execute_returning_by_reading(self) -> list[Any]:
        """Runs the update and reads the rows it wrote by their keys - matched before the update, read
        after it: the database returns no rows.

        Returns:
            The rows, in the order of their keys.

        Raises:
            UnSupportedError: The model has no primary key to read its rows by.
        """
        if not self.model._meta.has_primary_key:
            raise UnSupportedError(
                f"update().returning() of {self.model.__name__} reads the rows by their keys on "
                f"{self._connection.dialect} - "
                "the model has no primary key"
            )
        keys = await self._get_matching_pks()
        if not keys:
            return []
        await UpdateQuery(self, self.update_kwargs)
        return await self.returned_rows.read(self._connection, self._visibility, keys)

    async def _execute_returning(self) -> list[Any]:
        """Runs the update - the instances read back in its transaction - and reports it.

        Returns:
            The returned rows, in the order the database returned them.
        """
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is not None and not capture_needs.captures(RowOperation.UPDATE):
            capture_needs = None
        if not self.returned_rows.returns_instances and capture_needs is None:
            self._make_query_to_run()
            count, raw_rows = await self._run_statement(returns_rows=True)
            rows: list[Any] = self.returned_rows.get_rows(self._connection.dialect.types, raw_rows)
        elif not self.returned_rows.returns_instances:
            async with ChangeCapturing.transaction(self._connection) as transaction_connection:
                query = (
                    self if transaction_connection is self._connection else self._get_query_on(transaction_connection)
                )
                count, raw_rows = await query._run_captured(capture_needs)
            rows = self.returned_rows.get_rows(self._connection.dialect.types, raw_rows)
        else:
            async with self._connection._in_transaction() as transaction_connection:
                query = self._get_query_on(transaction_connection)
                if capture_needs is not None:
                    count, raw_rows = await query._run_captured(capture_needs)
                else:
                    query._make_query_to_run()
                    count, raw_rows = await query._run_statement(returns_rows=True)
                keys = self.returned_rows.get_primary_keys(transaction_connection.dialect.types, raw_rows)
                rows = await self.returned_rows.read(transaction_connection, self._visibility, keys)
        await self._report_updated_rows(count)
        return rows
