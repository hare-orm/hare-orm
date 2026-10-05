from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from hare.fields.relations.relation_accessors import RelationAccessors
from hare.query.expressions import Expression, F, Ordering, Q, Window
from hare.query.functions.window import RowNumber
from hare.query.relation_loading.constants import PREFETCH_ROW_NUMBER_ANNOTATION
from hare.sql.enums import Order

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
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
    def reads_window(related_queryset: QuerySet[Any]) -> bool:
        """Whether an annotation of a ``Prefetch`` queryset reads a window - computed over other rows
        than its own, the rows of every parent, which a slice of each parent's rows would narrow."""
        annotations = related_queryset._annotations
        seen_ids: set[int] = set()
        return any(
            SlicedPrefetch.contains_window(annotation, annotations, seen_ids) for annotation in annotations.values()
        )

    @staticmethod
    def contains_window(value: Any, annotations: Mapping[str, Any], seen_ids: set[int]) -> bool:
        """Whether a value of an expression is, or holds, a ``Window`` - an ``F()`` naming another
        annotation reads that one.

        Args:
            value: The expression, or a value of one.
            annotations: The queryset's annotations by name.
            seen_ids: The ids of the values looked into already.

        Returns:
            Whether it does.
        """
        if id(value) in seen_ids:
            return False
        seen_ids.add(id(value))
        if isinstance(value, Window):
            return True
        if isinstance(value, F):
            referenced = annotations.get(value.name)
            return referenced is not None and SlicedPrefetch.contains_window(referenced, annotations, seen_ids)
        if isinstance(value, (list, tuple)):
            return any(SlicedPrefetch.contains_window(item, annotations, seen_ids) for item in value)
        if isinstance(value, Expression):
            attributes = getattr(value, "__dict__", {})
            return any(SlicedPrefetch.contains_window(item, annotations, seen_ids) for item in attributes.values())
        return False

    @staticmethod
    def get_row_slice(related_queryset: QuerySet[Any]) -> slice:
        """The slice of a ``Prefetch`` queryset, taken of each parent's rows."""
        offset = related_queryset._offset or 0
        limit = related_queryset._limit
        return slice(offset, None if limit is None else offset + limit)

    @staticmethod
    def get_ordered_unsliced(related_queryset: QuerySet[Any]) -> QuerySet[Any]:
        """A ``Prefetch`` queryset without its slice, its rows in the order the slice is taken in - by
        their keys for a queryset of no ordering."""
        unsliced = SlicedPrefetch.get_unsliced(related_queryset)
        if list(unsliced._apply_default_ordering(unsliced._orderings, unsliced._annotations)):
            return unsliced
        return unsliced.order_by(*unsliced.model._meta.primary_key_attribute_names)

    @staticmethod
    def apply_row_slice(objs: Iterable[Model], field: str, to_attribute: str | None, row_slice: slice) -> None:
        """Narrows the prefetched rows of each object to the slice of its rows.

        Args:
            objs: The objects the rows were prefetched for.
            field: The relation.
            to_attribute: The attribute holding the rows, None for the relation itself.
            row_slice: The slice.
        """
        for obj in objs:
            if to_attribute:
                setattr(obj, to_attribute, getattr(obj, to_attribute)[row_slice])
            else:
                relation_rows = RelationAccessors.get_relation(obj, field).relation_rows
                relation_rows.related_objects = relation_rows.related_objects[row_slice]

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
            # An expression of Meta.ordering is ordered by itself - the window reads no alias of it.
            Ordering(unsliced.model._meta.ordering_expressions.get(name, name), order)
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
