from __future__ import annotations

import datetime
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.core.hare_context import HareContext
from hare.fields.enums import OnDelete
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.deletion_plan import RowKey
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.deletion.cascade.tenant_schema_visits import TenantSchemaVisits
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.rules.enums import WriteType

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.queryset import QuerySet


class CascadeRestore:
    """Brings back the rows a soft delete removed together with its root rows: every row an
    ``on_delete=CASCADE`` relation leads to that carries the root's very deletion time. What
    ``SET_NULL``/``SET_DEFAULT`` overwrote is not brought back.

    Args:
        connection: The connection the restore runs on; a model on another connection uses its own.
        deleted_at: The deletion time of the root rows.
        writes_tenant_rows_only: Restore only the rows of ``Meta.tenant_schema`` models - the restore
            of rows every tenant shares in another tenant's schema.
    """

    def __init__(
        self,
        connection: DatabaseClient | None,
        deleted_at: datetime.datetime,
        *,
        writes_tenant_rows_only: bool = False,
    ) -> None:
        self.connection = connection
        self.deleted_at = deleted_at
        self.writes_tenant_rows_only = writes_tenant_rows_only

    async def run(self, binding: ModelBinding, pks: list[Any]) -> None:
        """Restores the rows below the ``model`` rows ``pks`` - already restored by the caller.

        Args:
            binding: The model of the root rows.
            pks: Their primary keys.

        Raises:
            StaleObjectError: A row's model overrides ``restore()`` and the row changed concurrently.
        """
        model = binding._model
        await self._run_in_other_tenant_schemas(binding, pks)
        reached: set[RowKey] = {(model, pk) for pk in pks}
        frontier: dict[type[Model], list[Any]] = {model: list(pks)}
        while frontier:
            next_frontier: dict[type[Model], list[Any]] = {}
            for current_model, current_pks in frontier.items():
                await self._collect_children(
                    binding.graph.get_binding(current_model), current_pks, reached, next_frontier, source_model=model
                )
            for child_model, child_pks in next_frontier.items():
                child_binding = binding.graph.get_binding(child_model)
                if not self.writes_tenant_rows_only or child_binding.tenant_schema:
                    await self._restore_rows(child_binding, child_pks, source_model=model)
            frontier = next_frontier

    async def _run_in_other_tenant_schemas(self, binding: ModelBinding, pks: list[Any]) -> None:
        """Restores the rows of ``Meta.tenant_schema`` models below rows every tenant shares in each
        tenant schema besides the one the restore runs in - before the shared rows below the roots
        are restored, while they still lead to them.

        Args:
            binding: The model of the root rows.
            pks: Their primary keys.
        """
        if self.writes_tenant_rows_only or not TenantSchemaVisits.visits_schemas(
            binding, self.connection, leaves_constrained=False
        ):
            return
        connection: DatabaseClient = self.connection  # type: ignore[assignment]
        for schema_name, tenant in await TenantSchemaVisits.get_other_schemas(connection):
            async with TenantSchemaVisits.visit(connection, schema_name, tenant) as scope:
                await CascadeRestore(scope.connection, self.deleted_at, writes_tenant_rows_only=True).run(binding, pks)

    async def _collect_children(
        self,
        binding: ModelBinding,
        pks: list[Any],
        reached: set[RowKey],
        next_frontier: dict[type[Model], list[Any]],
        *,
        source_model: type[Model],
    ) -> None:
        """Adds to ``next_frontier`` the rows an ``on_delete=CASCADE`` relation leads to from the
        ``model`` rows ``pks`` that carry the root's deletion time.

        Args:
            binding: The model of the rows.
            pks: Their primary keys.
            reached: ``(type, pk)`` of every row reached so far.
            next_frontier: The next wave being built.
            source_model: The root model, used to choose whether this operation shares the cascade's connection.
        """
        for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(binding):
            related_model = backward_field.related_model
            related_binding = backward_field.get_related_binding()
            soft_delete_field = related_binding.soft_delete_field
            if (
                foreign_key_field.on_delete != OnDelete.CASCADE
                or not soft_delete_field
                or not DeletionGraph.reaches_table(related_binding, self.connection, source_model=source_model)
            ):
                continue
            target_values = await RelatedRows.get_target_values(
                binding, foreign_key_field, pks, self.connection, source_model=source_model
            )
            if not target_values:
                continue
            primary_key_attribute_names = backward_field.get_related_binding().primary_key_attribute_names
            pk_column_count = len(primary_key_attribute_names)
            for queryset in RelatedRows.get_pointing_querysets(
                backward_field, target_values, self.connection, source_model=source_model
            ):
                deleted_together = queryset.filter(**{soft_delete_field: self.deleted_at})
                for values in await deleted_together.values_list(*primary_key_attribute_names):
                    related_pk = values[0] if pk_column_count == 1 else tuple(values)
                    if (related_model, related_pk) in reached:
                        continue
                    reached.add((related_model, related_pk))
                    next_frontier.setdefault(related_model, []).append(related_pk)

    async def _restore_rows(self, binding: ModelBinding, pks: list[Any], *, source_model: type[Model]) -> None:
        """Clears ``Meta.soft_delete_field`` of the ``model`` rows ``pks`` - through the model's
        own ``restore()`` when it overrides it.

        Args:
            binding: The model of the rows.
            pks: Their primary keys.
            source_model: The root model, used to choose whether this operation shares the cascade's connection.
        """
        model = binding._model
        from hare.models import Model
        from hare.query.statements.write.update_query import UpdateQuery

        soft_delete_field: str = binding.soft_delete_field  # type: ignore[assignment]
        model_connection = RelatedRows.get_connection_for(binding, self.connection, source_model=source_model)
        lookup_connection = model_connection or ModelConnectionChoice.get(HareContext.require_current(), binding, True)
        for batch in RelatedRows.split_into_batches(pks, lookup_connection, len(binding.primary_key_attribute_names)):
            queryset: QuerySet[Any] = RelatedRows.include_soft_deleted(
                RelatedRows.get_base_queryset(binding)
                .filter(pk__in=batch, **{soft_delete_field: self.deleted_at})
                .using(model_connection)
            )
            if model.restore is Model.restore:
                await UpdateQuery(queryset, {soft_delete_field: None}, WriteType.SOFT_DELETE)
                continue
            tenant_field = binding.tenant_field
            for row in await queryset:
                tenant = getattr(row, tenant_field) if tenant_field else None
                with Tenancy.scope(tenant) if tenant is not None else nullcontext():
                    await row.restore(using=model_connection)
