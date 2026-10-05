from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import FieldError, QueryError
from hare.query.expressions import F, Subquery, Window
from hare.query.functions.window import RowNumber
from hare.query.lookup_info.lookup_path import LookupPath
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.rewrites.constants import DISTINCT_ON_ROW_NUMBER_ANNOTATION
from hare.query.statements.building.query_ordering import QueryOrdering

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.queryset import QuerySet


class DistinctOnEmulation:
    """``distinct(*fields)`` on a database without ``DISTINCT ON`` (``features.supports_distinct_on``):
    the queryset restricted to the row of each combination of the fields that comes first in its
    ordering - ``ROW_NUMBER() OVER (PARTITION BY <the fields> ORDER BY <the ordering>) = 1`` in a
    ``pk IN`` subquery - with the ordering, slice and selection kept, as PostgreSQL's ``DISTINCT ON``
    gives them."""

    @staticmethod
    def get_emulated(queryset: QuerySet[Any, Any]) -> QuerySet[Any, Any]:
        """The queryset with its ``DISTINCT ON`` emulated.

        Args:
            queryset: A queryset with ``distinct(*fields)``.

        Returns:
            The emulating queryset - the queryset itself on a database with ``DISTINCT ON``.

        Raises:
            FieldError: A field name is unknown, as on PostgreSQL.
            QueryError: ``order_by()`` doesn't start with the fields, as on PostgreSQL.
        """
        connection = queryset._connection or queryset.get_connection()
        if connection.features.supports_distinct_on:
            return queryset
        model = queryset.model
        model._meta.raise_if_no_primary_key(
            "distinct(*fields) picking the first row of each combination by row number"
        )
        DistinctOnEmulation.raise_if_unknown_names(queryset)
        orderings = list(queryset._apply_default_ordering(queryset._orderings, queryset._annotations))
        ordering_names = [name for name, _order in orderings]
        distinct_on_names = [
            expanded_name
            for name in queryset._distinct_on
            for expanded_name in (
                QueryOrdering.get_relation_ordering_key_names(model, name)
                or (model._meta.primary_key_attribute_names if name == "pk" else (name,))
            )
        ]
        if ordering_names and ordering_names[: len(distinct_on_names)] != distinct_on_names:
            raise QueryError(
                "distinct(*fields) must match the leading order_by() fields. "
                f"Expected order_by() to start with {tuple(queryset._distinct_on)!r}."
            )
        numbered = queryset._clone()
        numbered._distinct = False
        numbered._distinct_on = []
        numbered._limit = None
        numbered._offset = None
        numbered._selection = None
        numbered._prefetch_map = {}
        numbered._prefetch_queries = {}
        numbered._select_related = set()
        # It selects the key alone - the queryset's only()/defer() concern the rows it returns.
        numbered._fields_for_select = ()
        numbered._deferred_fields = ()
        window_orderings = [
            (F(name).asc if order.is_ascending else F(name).desc)(
                nulls_first=order.nulls_first is True, nulls_last=order.nulls_first is False
            )
            for name, order in orderings
        ] or [F(name).asc() for name in model._meta.primary_key_attribute_names]
        key_names = model._meta.primary_key_attribute_names
        first_rows = (
            numbered.order_by()
            .annotate(
                **{
                    DISTINCT_ON_ROW_NUMBER_ANNOTATION: Window(
                        RowNumber(), partition_by=distinct_on_names, order_by=window_orderings
                    )
                }
            )
            .filter(**{DISTINCT_ON_ROW_NUMBER_ANNOTATION: 1})
            .values(*key_names)
        )
        # Made again for each rewrite of the queryset - the values of the numbered rows, the first
        # row's number among them, come from it.
        first_rows._plan_origin = queryset
        first_rows._build_conditions_for_copies()
        first_rows._q_objects[-1]._plan_origin = queryset
        emulated = queryset._clone()
        emulated._distinct = False
        emulated._distinct_on = []
        emulated._limit = None
        emulated._offset = None
        key_path = "pk" if len(key_names) > 1 else key_names[0]
        # The numbered rows carry the queryset's own filters one query deeper: their OuterRefs refer
        # to the query this queryset is nested in, as without the numbering.
        first_rows_subquery = Subquery(cast("Any", first_rows), shares_outer_scope=True)
        emulated._append_filters(False, (), {f"{key_path}__in": first_rows_subquery})
        emulated._limit = queryset._limit
        emulated._offset = queryset._offset
        return emulated

    @staticmethod
    def raise_if_unknown_names(queryset: QuerySet[Any, Any]) -> None:
        """Checks that every ``distinct(*fields)`` name is an annotation or a field path of the model.

        Args:
            queryset: A queryset with ``distinct(*fields)``.

        Raises:
            FieldError: A name ends at no field of the model it reaches.
        """
        model = queryset.model
        for name in queryset._distinct_on:
            if name in queryset._annotations:
                continue
            for field_path in ConcreteFieldPaths.get_paths(model, name):
                lookup_path = LookupPath.parse(model, field_path)
                for segment in lookup_path.rest:
                    if segment not in lookup_path.model._meta.fields_map:
                        raise FieldError(f"Unknown field {segment} for model {lookup_path.model.__name__}")
