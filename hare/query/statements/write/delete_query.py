from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar

from hare.classes.deferred_import import DeferredImport
from hare.core.routing.written_connections import WrittenConnections
from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import (
    CascadeDepthLimitError,
)
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.deletion.cascade.cascade_deletion import CascadeDeletion
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.deletion_scope import DeletionScope
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.deletion.deletion_routes import DeletionRoutes
from hare.models.deletion.enums import DeletionRoute
from hare.models.deletion.instance_deletion import InstanceDeletion
from hare.models.write.rules.enums import WriteType
from hare.models.write.write_steps import WriteSteps
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.plans.statement.declarations import CallPlanHit
from hare.query.statements.building.query_ctes import QueryCtes
from hare.query.statements.write.matching_rows_query import MatchingRowsQuery
from hare.query.statements.write.returning.returned_rows import ReturnedRows
from hare.query.statements.write.update_query import UpdateQuery

if TYPE_CHECKING:
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.write.returning.delete_returning_query import DeleteReturningQuery


class DeleteQuery(MatchingRowsQuery):
    """``queryset.delete()`` - the queryset's rows deleted, or soft-deleted, with their cascade."""

    #: ``Model`` - imported at its first read, as its module imports this one.
    model_class: ClassVar[DeferredImport[type[Model]]] = DeferredImport("hare.models", "Model")

    written_method_name: ClassVar[str] = "delete"

    __slots__ = ()

    ordering_can_reference_annotation_alias: ClassVar[bool] = False
    #: Delete the rows for real even when ``Meta.soft_delete_field`` is set.
    deletes_permanently: ClassVar[bool] = False

    def _orders_rows(self) -> bool:
        # A delete orders its rows for its LIMIT alone.
        return self._limit is not None

    def _deletes_softly(self) -> bool:
        """Whether this delete soft-deletes the matched rows instead of deleting them."""
        return not self.deletes_permanently and self.model._meta.soft_delete_field is not None

    def _has_overridden_delete(self) -> bool:
        """Whether the model overrides the instance method this delete stands for - its rows then
        go through that override one by one."""

        if self.deletes_permanently:
            return self.model.hard_delete is not DeleteQuery.model_class.hard_delete
        return self.model.delete is not DeleteQuery.model_class.delete

    def _build_statement(
        self, value_wrapper_references: RecordedValueReferences | None, *, records_for_caller: bool
    ) -> bool:
        self._raise_if_rows_unwritable()
        self._build_matching_rows(value_wrapper_references)
        if self._filters_need_primary_key_subquery():
            # A DELETE takes no JOINs - the rows are picked by a primary key subquery. The
            # annotation terms QueryConditions.get_filters() selected would become extra subquery columns.
            self.query._selects = []
            matching_rows_criterion = self._get_matching_rows_criterion(self.query)
            self.query = self._get_base_query()
            self.query = self.query.where(matching_rows_criterion)

        self.query._delete_from = True
        self._apply_capture_returning()
        # Must run last - the joined-subquery branch above replaces self.query with a fresh copy
        # of the basequery, which would silently drop a WITH clause applied any earlier. Mirrors
        # the select/aggregate paths' identical placement.
        QueryCtes.apply_with_ctes(self, value_wrapper_references=value_wrapper_references)
        return True

    def _apply_capture_returning(self) -> None:
        """Returns the deleted rows' keys and captured fields, for a model with
        ``Meta.change_capture``."""
        capture_needs = self.model._meta.change_capture_needs
        if (
            capture_needs is not None
            and capture_needs.captures(RowOperation.DELETE)
            and self._connection.features.supports_returning
        ):
            self.query = self.query.returning(*capture_needs.columns)

    def _has_protected_relations(self) -> bool:
        return DeletionGraph.has_protecting_relations(self.model) or DeletionGraph.has_transitive_protect(self.model)

    async def _check_protected(self, pks: list[Any], connection: DatabaseClient) -> None:
        """Checks ``on_delete=PROTECT`` for the matched rows - once per guarded relation, not per row.
        A hard delete also looks for a PROTECT further down its CASCADE chain.
        """
        if self._has_protected_relations():
            await DeletionCollector.check_protected(self.model, pks, connection)
            if not self._deletes_softly():
                await DeletionCollector.check_protected_transitively(self.model, pks, connection)

    async def _delete_matching_rows_with_cascade(
        self, connection: DatabaseClient, *, live_only: bool = False
    ) -> int | None:
        """Deletes every matched row together with the cascade ``Model.delete()`` would run for it, in
        batches, inside one transaction.

        Args:
            live_only: Skip rows already soft-deleted.

        Returns:
            The number of matched rows.
        """
        pks = await self._get_matching_pks(connection, live_only=live_only)
        if not pks:
            return 0
        await self._check_protected(pks, connection)
        soft_deletes = self._deletes_softly()
        async with connection._in_transaction() as transaction_connection:
            if soft_deletes:
                # A row soft-deleted since it was matched (a concurrent delete this one waited
                # for) is left alone, as a row already deleted before is.
                soft_delete_values = await RelatedRows.lock_soft_delete_values(self.model, pks, transaction_connection)
                pks = [pk for pk in pks if pk in soft_delete_values and soft_delete_values[pk] is None]
                if not pks:
                    return 0
            cascade_deletion = CascadeDeletion(
                self.model,
                transaction_connection,
                only_unconstrained=not soft_deletes,
                persist_as_hard_delete=not soft_deletes,
            )
            if soft_deletes:
                await cascade_deletion.run(pks, persist_roots=True)
            else:
                async with ProtectConstraintDeferral.defer(self.model, transaction_connection):
                    await cascade_deletion.run(pks, persist_roots=True)
        return len(pks)

    async def _delete_each_matching_instance(
        self, connection: DatabaseClient, *, live_only: bool = False
    ) -> int | None:
        """Fetches every matched row and deletes each through ``Model.delete()`` - for a model
        overriding ``delete()``, and for the retry of a native cascade stopped at its depth limit.
        Several rows share one transaction.

        Args:
            live_only: Skip rows already soft-deleted.
        """
        pks = await self._get_matching_pks(connection, live_only=live_only)
        if not pks:
            return 0
        await self._check_protected(pks, connection)
        from hare.query.queryset.queryset import QuerySet

        instances_query = QuerySet(self.model).filter(pk__in=pks)
        instances_query._apply_connection(connection)
        # Exactly the matched rows - of every tenant this query sees, soft-deleted ones included.
        instances_query._visibility = replace(
            self._visibility.get_for_active_tenant(), include_deleted=True, only_deleted=False
        )
        instances = await instances_query
        if len(instances) <= 1:
            for instance in instances:
                await self._delete_matching_instance(instance, connection)
            return len(instances)
        async with connection._in_transaction() as transaction_connection:
            if self._deletes_softly():
                await self._delete_matching_instances_once(instances, transaction_connection)
            else:
                async with ProtectConstraintDeferral.defer(self.model, transaction_connection):
                    await self._delete_matching_instances_once(instances, transaction_connection)
        return len(instances)

    async def _delete_matching_instances_once(self, objs: list[Model], connection: DatabaseClient) -> None:
        """Deletes each of ``objs`` through ``_delete_matching_instance()``, skipping one the
        cascade of an earlier one already removed.

        Args:
            objs: The matched rows.
            connection: Connection the deletes and their cascades run through.
        """
        reached_keys: set[tuple[Any, ...]] = set()
        token = DeletionCollector.reached_keys_collector.set(reached_keys)
        try:
            for instance in objs:
                if DeletionScope.get_row_identity(type(instance), instance.pk, connection) in reached_keys:
                    continue
                await self._delete_matching_instance(instance, connection)
        finally:
            DeletionCollector.reached_keys_collector.reset(token)

    async def _delete_matching_instance(self, obj: Model, connection: DatabaseClient) -> None:
        """Deletes one row this DeleteQuery matched through ``Model.delete()`` - or, for an
        explicit ``.all_tenants()`` query, through the same cascade without the active-tenant-scope
        guard, so a row of another tenant is deleted exactly as the single-statement path would.

        Args:
            obj: The matched row.
            connection: Connection the delete and its cascade run through.
        """
        if self.deletes_permanently:
            if self._visibility.all_tenants:
                await InstanceDeletion.delete_row_permanently(obj, connection, apply_active_tenant_scope_guard=False)
            else:
                await obj.hard_delete(using=connection)
        elif self._visibility.all_tenants:
            await InstanceDeletion.delete_with_cascade(obj, connection, apply_active_tenant_scope_guard=False)
        else:
            await obj.delete(using=connection)

    async def _execute_soft_delete(self, connection: DatabaseClient) -> int | None:
        """The bulk delete of a soft-delete model: an UPDATE with the cascade ``Model.delete()`` runs -
        a single UPDATE when the model has no incoming relation. Rows already soft-deleted are left
        alone and not counted.
        """
        soft_delete_field: str = self.model._meta.soft_delete_field
        # Without include_deleted()/only_deleted() the default filter already excludes them.
        live_only = self._visibility.include_deleted
        # Many-to-many relations aren't among the backward relations - checked too.
        if DeletionRoutes.get(self.model, connection.features, self.deletes_permanently) is DeletionRoute.SOFT_UPDATE:
            # Fast path: no incoming relations at all, so no PROTECT/CASCADE/SET_NULL is possible
            # - one UPDATE with this DeleteQuery's own filters, no extra SELECT needed.
            if live_only and not self.model._meta.has_primary_key:
                return await UpdateQuery(
                    self._get_live_rows(connection),
                    {soft_delete_field: CascadeDeletion.get_deleted_at()},
                    WriteType.SOFT_DELETE,
                    "delete",
                )
            if live_only:
                live_pks = await self._get_matching_pks(connection, live_only=True)
                if not live_pks:
                    return 0
                from hare.query.queryset.queryset import QuerySet

                # Keep the original slice's membership, then narrow to still-live rows. A plain
                # QuerySet has no manager scope; the resolved visibility still guards related rows.
                live_rows = QuerySet(self.model)
                live_rows._visibility = replace(
                    self._visibility.get_for_active_tenant(), include_deleted=True, only_deleted=False
                )
                live_rows._call_signature = (QuerySet,)
                live_rows = live_rows.using(connection).filter(
                    pk__in=live_pks, **{f"{soft_delete_field}__isnull": True}
                )
                return await UpdateQuery(
                    live_rows, {soft_delete_field: CascadeDeletion.get_deleted_at()}, WriteType.SOFT_DELETE, "delete"
                )
            return await UpdateQuery(
                self._matching_queryset(connection),
                {soft_delete_field: CascadeDeletion.get_deleted_at()},
                WriteType.SOFT_DELETE,
                "delete",
            )
        return await self._delete_matching_rows_with_cascade(connection, live_only=live_only)

    def _get_live_rows(self, connection: DatabaseClient) -> QuerySet[Any]:
        """The rows of a soft delete seeing deleted rows too - ``include_deleted()`` - that aren't deleted
        yet: its own filters and the condition of a live row, for a model without a primary key to pick
        them by.

        Returns:
            The rows.
        """
        soft_delete_field: str = self.model._meta.soft_delete_field
        return self._filter_matching_rows(
            self._matching_queryset(connection), **{f"{soft_delete_field}__isnull": True}
        )

    async def _execute_bound(self) -> int | None | list[Any]:
        # .limit(0) matches no rows - nothing to write.
        if self._is_none:
            return await self._execute_none()
        connection = self.get_connection(for_write=True)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        if self._has_overridden_delete():
            # Each matched row goes through the model's override, one by one.
            live_only = self._deletes_softly() and self._visibility.include_deleted
            return await WriteSteps.get_whole_run(
                connection,
                self._report_delete(self._delete_each_matching_instance(connection, live_only=live_only), connection),
            )
        route = DeletionRoutes.get(self.model, connection.features, self.deletes_permanently)
        if route in (DeletionRoute.SOFT_UPDATE, DeletionRoute.SOFT_CASCADE):
            return await WriteSteps.get_whole_run(
                connection, self._report_delete(self._execute_soft_delete(connection), connection)
            )
        if route is DeletionRoute.PYTHON_CASCADE:
            # A relation the database doesn't enforce, or rows of a captured model it reaches: its
            # on_delete is carried out in Python first, in batches.
            return await WriteSteps.get_whole_run(
                connection, self._report_delete(self._delete_matching_rows_with_cascade(connection), connection)
            )
        return await WriteSteps.get_whole_run(
            connection, self._report_delete(self._execute_on(connection), connection)
        )

    async def _report_delete(self, delete: Awaitable[int | None], connection: DatabaseClient) -> int | None:
        """Runs the delete and reports the rows it removed and the rows their ``on_delete``
        reached (``ChangeEvents``).

        Args:
            delete: The delete.
            connection: The execution's connection.

        Returns:
            How many rows it deleted.
        """
        with ChangeEvents.reporting_as_a_whole():
            count = await delete
        await self._report_deleted_rows(count, connection)
        return count

    async def _report_deleted_rows(self, count: int | None, connection: DatabaseClient) -> None:
        """Reports the rows a delete removed and the rows their ``on_delete`` reached
        (``ChangeEvents``).

        Args:
            count: How many rows it deleted.
            connection: The execution's connection.
        """
        # A count the database doesn't report may be of removed rows.
        if (count is None or count) and ChangeEvents.is_observed():
            await ChangeEvents.report_deletion(connection, self.model, soft=self._deletes_softly())

    def returning(self, *field_names: str) -> DeleteReturningQuery:
        """The delete returning each row it deleted - ``DELETE ... RETURNING``: a dict of
        ``field_names`` per row, or the model instances when none is named. A row soft-deleted is
        returned as it is after the delete.

        Args:
            field_names: The fields - a concrete field, a forward relation (its key) or ``pk``.

        Returns:
            The delete, its result the list of rows.

        Raises:
            FieldError: A name isn't a field written in the model's table, or is given twice.
            UnSupportedError: When run, a direct SQL delete requires ``RETURNING`` that the database
                lacks. Deletion through model methods, cascades or soft-delete can read rows instead.
        """
        from hare.query.statements.write.declarations import HardDeleteReturningQuery
        from hare.query.statements.write.returning.delete_returning_query import DeleteReturningQuery

        returning_query_class = HardDeleteReturningQuery if self.deletes_permanently else DeleteReturningQuery
        return returning_query_class(self, ReturnedRows(self.model, field_names))

    async def _execute_none(self) -> int:
        return 0

    async def execute_statement(self, connection: DatabaseClient | None = None) -> int | None:
        """Runs this query's own ``DELETE`` statement alone - no PROTECT check, Python-side
        cascade or retry; the deleted rows of a model with ``Meta.change_capture`` captured, in one
        transaction with it.

        Returns:
            The number of deleted rows.
        """
        connection = connection if connection is not None else self.get_connection(for_write=True)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        capture_needs = self.model._meta.change_capture_needs
        if capture_needs is None or not capture_needs.captures(RowOperation.DELETE):
            return (await self._run_statement_on(connection))[0]
        captured = CapturedWrite(self.model, capture_needs, connection, RowOperation.DELETE)
        async with captured.transaction() as transaction_connection:
            if not transaction_connection.features.supports_returning:

                async def run_statement(source: QuerySet[Any]) -> int | None:
                    return (await self._run_statement_on(transaction_connection, source))[0]

                count, _pks = await self._run_captured_by_reading(captured, run_statement)
                return count
            count, raw_rows = await self._run_statement_on(transaction_connection, returns_rows=True)
            await captured.capture_returned(raw_rows)
        return count

    async def _run_statement_on(
        self, connection: DatabaseClient, source: QuerySet[Any] | None = None, *, returns_rows: bool = False
    ) -> tuple[int | None, Any]:
        """Run this delete's SQL only, with a fresh binding on the supplied connection."""
        statement = self._get_statement_on(connection, source, type_part=self._get_call_signature_type_part())
        if isinstance(statement, CallPlanHit):
            sql, parameters = statement.sql, statement.parameters
        else:
            _query, (sql, parameters) = statement
        return await connection.execute(
            sql,
            parameters,
            returns_rows=returns_rows,
            recovery=connection.get_write_recovery(self.model._meta.basetable),
        )

    async def _execute_on(self, connection: DatabaseClient) -> int | None:
        if self._has_protected_relations():
            await self._check_protected(await self._get_matching_pks(connection), connection)
        route = DeletionRoutes.get(self.model, connection.features, self.deletes_permanently)
        if route is DeletionRoute.DEPTH_LIMITED:
            # Only a model with a cascade cycle can reach the native cascade's depth limit - any
            # other keeps the plain path.
            try:
                # In a transaction: a DELETE stopped at the depth limit is half-done, and is rolled
                # back before the retry.
                async with connection._in_transaction() as transaction_connection:
                    return await self.execute_statement(transaction_connection)
            except CascadeDepthLimitError:
                # Row by row: each row's Model.delete() retries and falls back to a leaves-first
                # cascade of its own chain.
                return await self._delete_each_matching_instance(connection)
        if route is DeletionRoute.PROTECT_DEFERRED:
            async with (
                connection._in_transaction() as transaction_connection,
                ProtectConstraintDeferral.defer(self.model, transaction_connection),
            ):
                return await self.execute_statement(transaction_connection)
        return await self.execute_statement(connection)
