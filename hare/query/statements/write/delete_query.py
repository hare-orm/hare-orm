from __future__ import annotations

from collections.abc import Awaitable, Generator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.dialects.base.client.database_client import DatabaseClient
from hare.exceptions import (
    CascadeDepthLimitError,
)
from hare.instrumentation.change_events import ChangeEvents
from hare.models.deletion.cascade_deletion import CascadeDeletion
from hare.models.deletion.deletion_collector import DeletionCollector
from hare.models.deletion.deletion_graph import DeletionGraph
from hare.models.deletion.protect_constraint_deferral import ProtectConstraintDeferral
from hare.models.deletion.related_rows import RelatedRows
from hare.query.expressions import Q
from hare.query.expressions.value_refs.value_ref_types import RecordedValueRefs
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.queryset.query_options import QueryOptions
from hare.query.statements.write.matching_rows_query import MatchingRowsQuery
from hare.query.statements.write.update_query import UpdateQuery

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class DeleteQuery(MatchingRowsQuery):
    __slots__ = ()

    ordering_can_reference_annotation_alias: ClassVar[bool] = False
    #: Delete the rows for real even when ``Meta.soft_delete_field`` is set.
    deletes_permanently: ClassVar[bool] = False

    def _deletes_softly(self) -> bool:
        """Whether this delete soft-deletes the matched rows instead of deleting them."""
        return not self.deletes_permanently and self.model._meta.soft_delete_field is not None

    def _has_own_delete(self) -> bool:
        """Whether the model overrides the instance method this delete stands for - its rows then
        go through that override one by one."""
        from hare.models import Model

        if self.deletes_permanently:
            return self.model.hard_delete is not Model.hard_delete
        return self.model.delete is not Model.delete

    def _get_build_plan_description(self) -> PlanDescription | None:
        description = self._get_write_plan_description()
        if description is None:
            return None
        return PlanDescription(description.structure, description.values + self._get_ctes_values())

    def _build_statement(self, value_wrapper_refs: RecordedValueRefs | None, *, records_for_caller: bool) -> bool:
        if self._needs_primary_key_subquery_for_rows():
            self.query = self._get_base_query().where(self._get_matching_primary_key_criterion(value_wrapper_refs))
            self.query._delete_from = True
            self._apply_with_ctes(value_wrapper_refs=value_wrapper_refs)
            return True
        self.query = self._get_base_query()
        if self._limit is not None:
            self.query._limit = self._limit_term = self.query._wrapper_cls(self._limit)
            self.get_ordering(
                model=self.model,
                table=self.model._meta.basetable,
                orderings=self._orderings,
                annotations=self._annotations,
            )
        self.get_filters(value_wrapper_refs=value_wrapper_refs)
        self._record_matching_rows_limit(value_wrapper_refs)
        # An aggregate annotation in the filters needs its GROUP BY, or the HAVING aggregates over
        # the whole joined result.
        self._apply_auto_group_by()
        if self._filters_need_primary_key_subquery():
            # A DELETE takes no JOINs - the rows are picked by a primary key subquery. The
            # annotation terms get_filters() selected would become extra subquery columns.
            self.query._selects = []
            matching_rows_criterion = self._get_matching_rows_criterion(self.query)
            self.query = self._get_base_query()
            self.query = self.query.where(matching_rows_criterion)

        self.query._delete_from = True
        # Must run last - the joined-subquery branch above replaces self.query with a fresh copy
        # of the basequery, which would silently drop a WITH clause applied any earlier. Mirrors
        # the select/aggregate paths' identical placement.
        self._apply_with_ctes(value_wrapper_refs=value_wrapper_refs)
        return True

    def _has_protected_relations(self) -> bool:
        return DeletionGraph.has_protecting_relations(self.model) or DeletionGraph.has_transitive_protect(self.model)

    async def _check_protected(self, pks: list[Any]) -> None:
        """Checks ``on_delete=PROTECT`` for the matched rows - once per guarded relation, not per row.
        A hard delete also looks for a PROTECT further down its CASCADE chain.
        """
        if self._has_protected_relations():
            await DeletionCollector.check_protected(self.model, pks, self._db)
            if not self._deletes_softly():
                await DeletionCollector.check_protected_transitively(self.model, pks, self._db)

    async def _get_matching_pks(self, *, live_only: bool = False) -> list[Any]:
        """Primary keys of every row this DeleteQuery's own filters match, each listed once - a
        filter through a to-many relation JOINs, so the matching queryset returns the same parent
        row once per matching child.

        Args:
            live_only: Drop the rows ``Meta.soft_delete_field`` already marks as deleted, after
                the filters (and any slice) picked the matching rows.

        Returns:
            The distinct pks in first-seen order; a tuple per row for a composite primary key.
        """
        pk_attr_names = self.model._meta.pk_attr_names
        is_single_pk = len(pk_attr_names) == 1
        matching_pks = list(dict.fromkeys(await self._matching_queryset()._get_primary_key_values_query()))
        if not live_only or not matching_pks:
            return matching_pks
        from hare.query.queryset.queryset import QuerySet

        soft_delete_field = cast("str", self.model._meta.soft_delete_field)
        live_query = QuerySet(self.model).filter(pk__in=matching_pks, **{f"{soft_delete_field}__isnull": True})
        live_query._apply_db(self._db)
        live_query._visibility = replace(self._visibility, include_deleted=True, only_deleted=False)
        live_pks = set(await live_query.values_list(*pk_attr_names, flat=is_single_pk))
        return [pk for pk in matching_pks if pk in live_pks]

    async def _delete_matching_rows_with_cascade(self, *, live_only: bool = False) -> int:
        """Deletes every matched row together with the cascade ``Model.delete()`` would run for it, in
        batches, inside one transaction. A model overriding ``delete()`` goes through that override,
        row by row.

        Args:
            live_only: Skip rows already soft-deleted.

        Returns:
            The number of matched rows.
        """
        if self._has_own_delete():
            return await self._delete_each_matching_instance(live_only=live_only)
        pks = await self._get_matching_pks(live_only=live_only)
        if not pks:
            return 0
        await self._check_protected(pks)
        soft_deletes = self._deletes_softly()
        async with self._db._in_transaction() as transaction_db:
            if soft_deletes:
                # A row soft-deleted since it was matched (a concurrent delete this one waited
                # for) is left alone, as a row already deleted before is.
                soft_delete_values = await RelatedRows.lock_soft_delete_values(self.model, pks, transaction_db)
                pks = [pk for pk in pks if pk in soft_delete_values and soft_delete_values[pk] is None]
                if not pks:
                    return 0
            cascade_deletion = CascadeDeletion(
                self.model,
                transaction_db,
                only_unconstrained=not soft_deletes,
                persist_as_hard_delete=not soft_deletes,
            )
            if soft_deletes:
                await cascade_deletion.run(pks, persist_roots=True)
            else:
                async with ProtectConstraintDeferral.defer(self.model, transaction_db):
                    await cascade_deletion.run(pks, persist_roots=True)
        return len(pks)

    async def _delete_each_matching_instance(self, *, live_only: bool = False) -> int:
        """Fetches every matched row and deletes each through ``Model.delete()`` - for a model
        overriding ``delete()``, and for the retry of a native cascade stopped at its depth limit.
        Several rows share one transaction.

        Args:
            live_only: Skip rows already soft-deleted.
        """
        pks = await self._get_matching_pks(live_only=live_only)
        if not pks:
            return 0
        await self._check_protected(pks)
        from hare.query.queryset.queryset import QuerySet

        instances_query = QuerySet(self.model).filter(pk__in=pks)
        instances_query._apply_db(self._db)
        # Exactly the matched rows - of every tenant this query sees, soft-deleted ones included.
        instances_query._visibility = replace(self._visibility, include_deleted=True, only_deleted=False)
        instances = await instances_query
        if len(instances) <= 1:
            for instance in instances:
                await self._delete_matching_instance(instance, self._db)
            return len(instances)
        async with self._db._in_transaction() as transaction_db:
            if self._deletes_softly():
                await self._delete_matching_instances_once(instances, transaction_db)
            else:
                async with ProtectConstraintDeferral.defer(self.model, transaction_db):
                    await self._delete_matching_instances_once(instances, transaction_db)
        return len(instances)

    async def _delete_matching_instances_once(self, instances: list[Model], db: DatabaseClient) -> None:
        """Deletes each of ``instances`` through ``_delete_matching_instance()``, skipping one the
        cascade of an earlier one already removed.

        Args:
            instances: The matched rows.
            db: Connection the deletes and their cascades run through.
        """
        reached_keys: set[tuple[type[Model], Any]] = set()
        token = DeletionCollector.reached_keys_collector.set(reached_keys)
        try:
            for instance in instances:
                if (type(instance), instance.pk) in reached_keys:
                    continue
                await self._delete_matching_instance(instance, db)
        finally:
            DeletionCollector.reached_keys_collector.reset(token)

    async def _delete_matching_instance(self, instance: Model, db: DatabaseClient) -> None:
        """Deletes one row this DeleteQuery matched through ``Model.delete()`` - or, for an
        explicit ``.all_tenants()`` query, through the same cascade without the active-tenant-scope
        guard, so a row of another tenant is deleted exactly as the single-statement path would.

        Args:
            instance: The matched row.
            db: Connection the delete and its cascade run through.
        """
        if self.deletes_permanently:
            if self._visibility.all_tenants:
                await instance._delete_row_permanently(db, apply_active_tenant_scope_guard=False)
            else:
                await instance.hard_delete(using=db)
        elif self._visibility.all_tenants:
            await instance._delete_with_cascade(db, apply_active_tenant_scope_guard=False)
        else:
            await instance.delete(using=db)

    async def _execute_soft_delete(self) -> int:
        """The bulk delete of a soft-delete model: an UPDATE with the cascade ``Model.delete()`` runs -
        a single UPDATE when the model has no incoming relation. Rows already soft-deleted are left
        alone and not counted.
        """
        soft_delete_field = cast("str", self.model._meta.soft_delete_field)
        # Without include_deleted()/only_deleted() the default filter already excludes them.
        live_only = self._visibility.include_deleted
        # Many-to-many relations aren't among the backward relations - checked too.
        if not DeletionGraph.get_backward_relations(self.model) and not DeletionGraph.get_m2m_fields(self.model):
            # Fast path: no incoming relations at all, so no PROTECT/CASCADE/SET_NULL is possible
            # - one UPDATE with this DeleteQuery's own filters, no extra SELECT needed.
            if live_only:
                live_pks = await self._get_matching_pks(live_only=True)
                if not live_pks:
                    return 0
                live_row_filter: dict[str, Any] = {f"{soft_delete_field}__isnull": True}
                live_rows = MatchingRowsQuery(self)
                live_rows._q_objects = [Q(pk__in=live_pks), Q(**live_row_filter)]
                live_rows._annotations = {}
                live_rows._options = QueryOptions.DEFAULT
                live_rows._limit = None
                live_rows._offset = None
                live_rows._orderings = []
                live_rows._distinct = False
                # Exactly the rows found above - no default scope on top of them.
                live_rows._uses_default_scope = False
                live_rows._ambient_q_count = 0
                live_rows._visibility = replace(self._visibility, include_deleted=True, only_deleted=False)
                return await UpdateQuery(live_rows, {soft_delete_field: CascadeDeletion.get_deleted_at()})
            return await UpdateQuery(self, {soft_delete_field: CascadeDeletion.get_deleted_at()})
        return await self._delete_matching_rows_with_cascade(live_only=live_only)

    def __await__(self) -> Generator[Any, None, int]:
        # .limit(0) matches no rows - nothing to write.
        if self._is_none or self._limit == 0:
            return self._execute_none().__await__()
        query = self._get_execution_query(True)
        if self._deletes_softly():
            return query._report_delete(query._execute_soft_delete()).__await__()
        if DeletionGraph.has_unconstrained_relations(self.model):
            # A relation the database doesn't enforce: its on_delete is carried out in Python first,
            # in batches.
            return query._report_delete(query._delete_matching_rows_with_cascade()).__await__()
        query._make_query_to_run()
        return query._report_delete(query._execute()).__await__()

    async def _report_delete(self, delete: Awaitable[int]) -> int:
        """Runs the delete and reports the rows it removed and the rows their ``on_delete``
        reached (``ChangeEvents``).

        Args:
            delete: The delete.

        Returns:
            How many rows it deleted.
        """
        with ChangeEvents.reporting_as_a_whole():
            count = await delete
        if count and ChangeEvents.is_observed():
            await ChangeEvents.report_deletion(self._db, self.model, soft=self._deletes_softly())
        return count

    async def _execute_none(self) -> int:
        return 0

    async def execute_statement(self) -> int:
        """Runs this query's own ``DELETE`` statement alone - no PROTECT check, Python-side
        cascade or retry.

        Returns:
            The number of deleted rows.
        """
        query = self._get_execution_query(True)
        query._make_query_to_run()
        return (await query._db.execute(*query._get_parameterized_sql(), returns_rows=False))[0]

    async def _execute(self) -> int:
        if self._has_protected_relations():
            await self._check_protected(await self._get_matching_pks())
        if (
            self._db.features.cascade_depth_limit is not None
            and DeletionGraph.has_self_cascading_constrained_relations(self.model)
        ):
            # Only a model with a cascade cycle can reach the native cascade's depth limit - any
            # other keeps the plain path.
            try:
                # In a transaction: a DELETE stopped at the depth limit is half-done, and is rolled
                # back before the retry.
                async with self._db._in_transaction() as transaction_db:
                    return (await transaction_db.execute(*self._get_parameterized_sql(), returns_rows=False))[0]
            except CascadeDepthLimitError:
                # Row by row: each row's Model.delete() retries and falls back to a leaves-first
                # cascade of its own chain.
                return await self._delete_each_matching_instance()
        if ProtectConstraintDeferral.is_needed(self.model, self._db):
            async with (
                self._db._in_transaction() as transaction_db,
                ProtectConstraintDeferral.defer(self.model, transaction_db),
            ):
                return (await transaction_db.execute(*self._get_parameterized_sql(), returns_rows=False))[0]
        return (await self._db.execute(*self._get_parameterized_sql(), returns_rows=False))[0]


class HardDeleteQuery(DeleteQuery):
    """``QuerySet.hard_delete()``: deletes the matched rows for real, even when
    ``Meta.soft_delete_field`` is set - the related rows follow their ``on_delete`` as a
    ``delete()`` of a model without soft delete would."""

    __slots__ = ()

    deletes_permanently: ClassVar[bool] = True
