from __future__ import annotations

import datetime
from contextlib import nullcontext
from typing import TYPE_CHECKING, Any, cast

from hare.fields.enums import OnDelete
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.deletion_plan import RowKey
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.tenancy.tenancy import Tenancy

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


class CascadeRestore:
    """Brings back the rows a soft delete removed together with its root rows: every row an
    ``on_delete=CASCADE`` relation leads to that carries the root's very deletion time. What
    ``SET_NULL``/``SET_DEFAULT`` overwrote is not brought back.

    Args:
        connection: The connection the restore runs on; a model on another connection uses its own.
        deleted_at: The deletion time of the root rows.
    """

    def __init__(self, connection: DatabaseClient | None, deleted_at: datetime.datetime) -> None:
        self.connection = connection
        self.deleted_at = deleted_at

    async def run(self, model: type[Model], pks: list[Any]) -> None:
        """Restores the rows below the ``model`` rows ``pks`` - already restored by the caller.

        Args:
            model: The model of the root rows.
            pks: Their primary keys.

        Raises:
            StaleObjectError: A row's model overrides ``restore()`` and the row changed concurrently.
        """
        reached: set[RowKey] = {(model, pk) for pk in pks}
        frontier: dict[type[Model], list[Any]] = {model: list(pks)}
        while frontier:
            next_frontier: dict[type[Model], list[Any]] = {}
            for current_model, current_pks in frontier.items():
                await self._collect_children(current_model, current_pks, reached, next_frontier)
            for child_model, child_pks in next_frontier.items():
                await self._restore_rows(child_model, child_pks)
            frontier = next_frontier

    async def _collect_children(
        self,
        model: type[Model],
        pks: list[Any],
        reached: set[RowKey],
        next_frontier: dict[type[Model], list[Any]],
    ) -> None:
        """Adds to ``next_frontier`` the rows an ``on_delete=CASCADE`` relation leads to from the
        ``model`` rows ``pks`` that carry the root's deletion time.

        Args:
            model: The model of the rows.
            pks: Their primary keys.
            reached: ``(type, pk)`` of every row reached so far.
            next_frontier: The next wave being built.
        """
        for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(model):
            related_model = backward_field.related_model
            soft_delete_field = related_model._meta.soft_delete_field
            if foreign_key_field.on_delete != OnDelete.CASCADE or not soft_delete_field:
                continue
            target_values = await RelatedRows.get_target_values(model, foreign_key_field, pks, self.connection)
            if not target_values:
                continue
            primary_key_attribute_names = related_model._meta.primary_key_attribute_names
            pk_column_count = len(primary_key_attribute_names)
            for queryset in RelatedRows.get_pointing_querysets(backward_field, target_values, self.connection):
                deleted_together = queryset.filter(**{soft_delete_field: self.deleted_at})
                for values in await deleted_together.values_list(*primary_key_attribute_names):
                    related_pk = values[0] if pk_column_count == 1 else tuple(values)
                    if (related_model, related_pk) in reached:
                        continue
                    reached.add((related_model, related_pk))
                    next_frontier.setdefault(related_model, []).append(related_pk)

    async def _restore_rows(self, model: type[Model], pks: list[Any]) -> None:
        """Clears ``Meta.soft_delete_field`` of the ``model`` rows ``pks`` - through the model's
        own ``restore()`` when it overrides it.

        Args:
            model: The model of the rows.
            pks: Their primary keys.
        """
        from hare.models import Model
        from hare.query.statements.write.update_query import UpdateQuery

        soft_delete_field = cast("str", model._meta.soft_delete_field)
        model_connection = RelatedRows.get_connection_for(model, self.connection)
        lookup_connection = model_connection or model.get_connection(for_write=True)
        for batch in RelatedRows.split_into_batches(
            pks, lookup_connection, len(model._meta.primary_key_attribute_names)
        ):
            queryset: QuerySet[Any] = RelatedRows.include_soft_deleted(
                RelatedRows.get_base_queryset(model).filter(pk__in=batch).using(model_connection)
            )
            if model.restore is Model.restore:
                await UpdateQuery(queryset, {soft_delete_field: None})
                continue
            tenant_field = model._meta.tenant_field
            for row in await queryset:
                tenant = getattr(row, tenant_field) if tenant_field else None
                with Tenancy.scope(tenant) if tenant is not None else nullcontext():
                    await row.restore(using=model_connection)
