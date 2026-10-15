"""Changing the rows a request asks for: deleting, updating, locking one to change it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.request_query.enums import EachRowOnce
from hare.exceptions import QueryError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.request_query.request_query import RequestQuery
    from hare.models import Model


class RowChanges[ModelType: Model]:
    """Changes the rows of a request query. The request's parameters or the handler's ``where()``
    must filter a delete or an update - the access condition alone doesn't count, so a request
    filtering by nothing never changes every row the user may see.

    Args:
        request_query: The request query.
    """

    def __init__(self, request_query: RequestQuery[ModelType]) -> None:
        self.request_query = request_query

    async def delete(self) -> int:
        """Deletes the rows.

        Returns:
            How many rows were deleted.

        Raises:
            QueryError: Nothing filters the rows.
        """
        queryset = await self.request_query.build_queryset(
            "delete", each_row_once=EachRowOnce.NOT_NEEDED, needs_filter=True
        )
        return await queryset.delete()

    async def update(self, values: Mapping[str, Any]) -> int:
        """Updates the rows.

        Args:
            values: The new values by field, as for ``QuerySet.update()``.

        Returns:
            How many rows were updated.

        Raises:
            QueryError: Nothing filters the rows, or no value is given.
        """
        if not values:
            raise QueryError(f"{type(self.request_query).__qualname__}.update() needs the values to set")
        queryset = await self.request_query.build_queryset(
            "update", each_row_once=EachRowOnce.NOT_NEEDED, needs_filter=True
        )
        return await queryset.update(**values)

    async def get_for_update(self) -> ModelType:
        """The one row, locked until the surrounding transaction ends (``SELECT ... FOR UPDATE``
        where the database locks rows; elsewhere read without a lock, as ``get()`` does). The
        request's fields and include parameters don't apply: only the row itself is locked and loaded.

        Returns:
            The row, after ``after_fetch()``.

        Raises:
            DoesNotExist: No row matches.
            MultipleObjectsReturned: More than one row matches.
            QueryError: A filter crosses a relation to many rows on a model without a primary key.
        """
        request_query = self.request_query
        queryset = await request_query.build_queryset("get_for_update", each_row_once=EachRowOnce.BY_KEY)
        database = queryset.get_connection(for_write=True)
        queryset = queryset.using(database)
        if database.features.supports_select_for_update:
            queryset = queryset.select_for_update()
        item = await queryset.get()
        (item,) = await request_query.after_fetch([item])
        return item
