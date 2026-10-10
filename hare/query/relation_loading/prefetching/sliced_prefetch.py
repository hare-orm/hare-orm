from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.query.expressions import F, Q, Window
from hare.query.functions.window import RowNumber
from hare.query.relation_loading.constants import PREFETCH_ROW_NUMBER_ANNOTATION
from hare.sql.enums import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset import QuerySet


class SlicedPrefetch:
    """A sliced Prefetch() queryset - the first rows of each parent in one query: the rows numbered
    within each parent's partition, the slice applied to the numbers."""

    @staticmethod
    def is_sliced(related_queryset: QuerySet[Any]) -> bool:
        """Whether a ``Prefetch`` queryset is sliced - its slice is taken of each parent's rows."""
        return related_queryset._limit is not None or related_queryset._offset is not None

    @staticmethod
    def keeps_the_only_row(related_queryset: QuerySet[Any]) -> bool:
        """Whether the slice of a ``Prefetch`` queryset keeps the one row a parent has through a
        forward relation or a reverse one-to-one - a slice starting at it and not empty.

        Args:
            related_queryset: The sliced queryset.

        Returns:
            True when the row is kept.
        """
        limit = related_queryset._limit
        return not related_queryset._offset and (limit is None or limit > 0)

    @staticmethod
    def get_unsliced(related_queryset: QuerySet[Any]) -> QuerySet[Any]:
        """A ``Prefetch`` queryset without its slice."""
        unsliced = related_queryset._clone()
        unsliced._limit = None
        unsliced._offset = None
        return unsliced

    @staticmethod
    def get_numbered_rows(
        related_queryset: QuerySet[Any], partition_paths: Sequence[str], parent_condition: Q
    ) -> QuerySet[Any]:
        """The rows of a sliced ``Prefetch`` queryset that fall into its slice within each parent's
        rows: numbered by ``ROW_NUMBER() OVER (PARTITION BY <the parent's key> ORDER BY <the
        queryset's ordering>)``, the number compared with the slice's bounds.

        Args:
            related_queryset: The sliced queryset.
            partition_paths: The paths holding each row's parent key.
            parent_condition: The condition taking the rows of the parents prefetched.

        Returns:
            The rows of the slice - numbered, the number in ``PREFETCH_ROW_NUMBER_ANNOTATION``.
        """
        offset = related_queryset._offset or 0
        limit = related_queryset._limit
        unsliced = SlicedPrefetch.get_unsliced(related_queryset)
        orderings = list(unsliced._apply_default_ordering(unsliced._orderings, unsliced._annotations))
        if not orderings:
            # Without an ordering the rows of a parent come in the order of their keys.
            orderings = [(name, Order.ASC) for name in unsliced.model._meta.primary_key_attribute_names]
        window_orderings = [
            (F(name).asc if order.is_ascending else F(name).desc)(
                nulls_first=order.nulls_first is True, nulls_last=order.nulls_first is False
            )
            for name, order in orderings
        ]
        # The numbered rows are read as keys - what the queryset loads with each row is loaded
        # with the rows of the slice.
        unsliced._prefetch_map = {}
        unsliced._prefetch_queries = {}
        unsliced._select_related = set()
        row_filter: dict[str, int] = {f"{PREFETCH_ROW_NUMBER_ANNOTATION}__gt": offset}
        if limit is not None:
            row_filter[f"{PREFETCH_ROW_NUMBER_ANNOTATION}__lte"] = offset + limit
        return (
            unsliced.filter(parent_condition)
            .annotate(
                **{
                    PREFETCH_ROW_NUMBER_ANNOTATION: Window(
                        RowNumber(), partition_by=list(partition_paths), order_by=window_orderings
                    )
                }
            )
            .filter(**row_filter)
        )
