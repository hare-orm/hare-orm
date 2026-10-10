from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from hare.query.expressions.value_references.cursor_value_reference import CursorValueReference
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.building.query_ordering import QueryOrdering
from hare.sql import Order
from hare.sql.enums import Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.awaitable_query import AwaitableQuery


class KeysetBounds:
    """The keyset boundaries of a query - the rows after or before a cursor's values in the query's
    ordering, a NULL compared as the dialect sorts it."""

    @staticmethod
    def get_cursor_criterion(
        query: AwaitableQuery[Any],
        *,
        value_wrapper_references: RecordedValueReferences | None = None,
    ) -> Criterion | None:
        """Builds the keyset criterion of ``.after_cursor(...)`` - the seek method: an OR of
        AND-chains, one per ordering field. A row-value comparison would assume one direction for
        every column.

        NULL-aware, by the placement of NULLs in each ordering: a NULL boundary is followed by every
        non-NULL row when NULLs sort first, and by nothing when they sort last; a non-NULL boundary
        is also followed by the NULL rows when NULLs sort after the ordinary values.

        ``value_wrapper_references``, when a plan is recorded, gets one ``("cursor", CursorValueReference |
        None)`` entry per ordering field - None for a NULL boundary, which no plan can bind.

        The upper boundary of a window (``_before_cursor_values``) is "strictly after" the same
        values in the reversed ordering - the same formula with every order reversed; its references
        follow the lower boundary's.

        Args:
            query: The query.
            value_wrapper_references: Receives the boundaries' value references while a plan is recorded.
        """
        if not query._cursor_values and not query._before_cursor_values:
            return None

        orderings = getattr(query, "_orderings", None)
        if not orderings:
            return None

        criterion: Criterion | None = None
        if query._cursor_values:
            criterion = KeysetBounds.get_cursor_bound_criterion(
                query, orderings, query._cursor_values, "_cursor_values", value_wrapper_references
            )
        if query._before_cursor_values:
            reversed_orderings = [(field_name, order.get_reversed()) for field_name, order in orderings]
            before_criterion = KeysetBounds.get_cursor_bound_criterion(
                query,
                reversed_orderings,
                query._before_cursor_values,
                "_before_cursor_values",
                value_wrapper_references,
            )
            criterion = before_criterion if criterion is None else criterion & before_criterion
        return criterion

    @staticmethod
    def get_cursor_bound_criterion(
        query: AwaitableQuery[Any],
        orderings: Sequence[tuple[str, Order]],
        cursor_values: tuple[Any, ...],
        cursor_attribute: str,
        value_wrapper_references: RecordedValueReferences | None,
    ) -> Criterion | None:
        """Builds the "strictly after ``cursor_values`` in ``orderings``" criterion.

        Args:
            query: The query.
            orderings: The ordering the boundary is relative to.
            cursor_values: One boundary value per leading ordering field - ``iterator()`` bounds a
                page only by the caller's own cursor fields while ordering by a primary key
                tie-breaker after them.
            cursor_attribute: The attribute of the query holding them.
            value_wrapper_references: See ``KeysetBounds.get_cursor_criterion()``.

        Returns:
            The criterion, ``None`` only for an empty ordering.
        """
        table = query._effective_basetable()
        criterion: Criterion | None = None
        equalities: list[Criterion] = []
        last_index = len(cursor_values) - 1
        for index, ((field_name, order), value) in enumerate(
            zip(orderings[: len(cursor_values)], cursor_values, strict=True)
        ):
            term, joins, field = LookupPaths.get_nested_field(
                query.model,
                table,
                field_name,
                visibility=query._visibility,
                select_related_extra_conditions=query._select_related_extra_conditions,
                dialect=query.dialect,
                connection=query._connection,
            )
            # An ordering across a relation compares a joined column - its JOINs are added to the
            # query.
            for join in joins:
                QueryJoins.join_table(query, join)
            # A None boundary is not converted: it may come from a LEFT JOIN miss, and the target
            # field's validation would reject it.
            db_value = (
                query.dialect.types.get_db_value(field, value, query.model)
                if field is not None and value is not None
                else value
            )
            nulls_first = QueryOrdering.nulls_sort_first(query, order)
            if db_value is None:
                # A None boundary holds no value - which boundaries are None is part of the plan
                # key (StatementPlanDescriptions.get_cursor_plan_description()); a value converting to
                # NULL keeps no plan.
                if value_wrapper_references is not None and value is not None:
                    value_wrapper_references.append(
                        (PlanOrigins.get_value_origin(query, cursor_attribute, index), None)
                    )
                if nulls_first:
                    comparison: Criterion = term.notnull()
                else:
                    comparison = BasicCriterion(
                        Equality.EQ,
                        ValueWrapper(1, allow_parametrize=False),
                        ValueWrapper(0, allow_parametrize=False),
                    )
                # `term = NULL` is never true - a row tying with a NULL boundary needs IS NULL.
                equality = term.isnull()
            else:
                ordinary = term > db_value if order.is_ascending else term < db_value
                comparison = ordinary if nulls_first else (ordinary | term.isnull())
                equality = term == db_value
                if value_wrapper_references is not None:
                    # The comparison and the equality hold two separate ValueWrappers of one value -
                    # both are referenced. A raw ordering column has no field to convert a new value
                    # by. The last field's equality is in no AND-chain.
                    comparison_wrapper = ordinary.right
                    equality_wrapper = equality.right if index != last_index else None
                    if (
                        field is not None
                        and isinstance(comparison_wrapper, ValueWrapper)
                        and (equality_wrapper is None or isinstance(equality_wrapper, ValueWrapper))
                    ):
                        value_wrapper_references.append(
                            (
                                PlanOrigins.get_value_origin(query, cursor_attribute, index),
                                CursorValueReference(comparison_wrapper, equality_wrapper, field),
                            )
                        )
                    else:
                        value_wrapper_references.append(
                            (PlanOrigins.get_value_origin(query, cursor_attribute, index), None)
                        )
            step = comparison & Criterion.all(equalities) if equalities else comparison
            criterion = step if criterion is None else criterion | step
            equalities.append(equality)
        return criterion
