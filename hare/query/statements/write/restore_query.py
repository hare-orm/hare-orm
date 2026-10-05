from __future__ import annotations

from collections.abc import Generator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError
from hare.models.deletion.cascade.cascade_restore import CascadeRestore
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.query.statements.write.update_query import UpdateQuery

if TYPE_CHECKING:  # pragma: nocoverage
    import datetime

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.query.queryset.queryset import QuerySet


class RestoreQuery:
    """``QuerySet.restore(cascade=False)``: reverses the soft delete of the queryset's rows - of those
    among them that are soft-deleted, whatever visibility the queryset has. Awaits to the number of
    rows restored.

    Args:
        source: The queryset.
        cascade: Also restore the rows the soft delete of each removed along with it - those still
            carrying its deletion time.

    Raises:
        QueryError: The model has no ``Meta.soft_delete_field``; ``cascade`` isn't a bool.
    """

    __slots__ = ("source", "cascade")

    def __init__(self, source: QuerySet[Any, Any], cascade: bool) -> None:
        if not source.model._meta.soft_delete_field:
            raise QueryError(f"{source.model.__name__} has no Meta.soft_delete_field configured")
        if not isinstance(cascade, bool):
            raise QueryError(f"restore(cascade=...) takes a bool, got {cascade!r}")
        # The soft-deleted rows among the queryset's - the default scope hides them.
        self.source = source._with_deleted_rows(only_deleted=True)
        self.cascade = cascade

    def __await__(self) -> Generator[Any, None, int]:
        return self._execute().__await__()

    async def _execute(self) -> int:
        """Restores the rows - one ``UPDATE`` without ``cascade``; with it, or for a model overriding
        ``restore()``, in a transaction.

        Returns:
            The number of rows restored.
        """
        # Local import: the queryset package imports the query statements.
        from hare.models import Model
        from hare.query.queryset.selection.statement_selection import StatementSelection

        model = self.source.model
        soft_delete_field = cast("str", model._meta.soft_delete_field)
        overrides_restore = model.restore is not Model.restore
        if not self.cascade and not overrides_restore:
            return await UpdateQuery(self.source, {soft_delete_field: None})
        connection = self.source._get_execution_query(for_write=True)._connection
        async with connection._in_transaction() as transaction_connection:
            matching_rows = self.get_rows_on(self.source, transaction_connection)
            keys = list(dict.fromkeys(await StatementSelection.get_primary_key_values_query(matching_rows)))
            if not keys:
                return 0
            if overrides_restore:
                instances = await self.get_rows_on(
                    model.objects.filter(pk__in=keys).only_deleted(), transaction_connection
                )
                for instance in instances:
                    await instance.restore(using=transaction_connection, cascade=self.cascade)
                return len(instances)
            deleted_at_by_key = await RelatedRows.lock_soft_delete_values(model, keys, transaction_connection)
            keys = [key for key in keys if deleted_at_by_key.get(key) is not None]
            if not keys:
                return 0
            restored_rows = self.get_rows_on(model.objects.filter(pk__in=keys).only_deleted(), transaction_connection)
            count = await UpdateQuery(restored_rows, {soft_delete_field: None})
            keys_by_deleted_at: dict[datetime.datetime, list[Any]] = {}
            for key in keys:
                keys_by_deleted_at.setdefault(cast("datetime.datetime", deleted_at_by_key[key]), []).append(key)
            for deleted_at, deleted_keys in keys_by_deleted_at.items():
                await CascadeRestore(transaction_connection, deleted_at).run(model, deleted_keys)
        return count

    def get_rows_on(self, queryset: QuerySet[Any, Any], connection: DatabaseClient) -> QuerySet[Any, Any]:
        """``queryset`` run on ``connection`` - with this restore's tenant visibility.

        Args:
            queryset: The rows.
            connection: The transaction's connection.

        Returns:
            A copy of the queryset.
        """
        rows = queryset._clone()
        rows._apply_connection(connection)
        rows._connection_explicitly_chosen = True
        rows._visibility = replace(self.source._visibility, only_deleted=rows._visibility.only_deleted)
        return rows
