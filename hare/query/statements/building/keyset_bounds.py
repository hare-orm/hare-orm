from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache
from hare.fields.data.numeric.float_field import FloatField
from hare.fields.generated_field import GeneratedField
from hare.query.enums import Lookup
from hare.query.expressions.expression_context import ExpressionContext
from hare.query.expressions.value_references.cursor_value_reference import CursorValueReference
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.filters.resolution.filter_kwargs import FilterKwargs
from hare.query.lookup_info.bound_lookup_path import BoundLookupPath
from hare.query.lookup_info.bound_ordering import BoundOrdering
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.queryset.concrete_field_paths import ConcreteFieldPaths
from hare.query.statements.building.query_joins import QueryJoins
from hare.query.statements.constants import KEYSET_FIELD_ORDER_CACHE_SIZE
from hare.sql import Order
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:
    from hare.fields.field import Field
    from hare.query.statements.awaitable_query import AwaitableQuery
    from hare.sql.terms.term import Term


class KeysetBounds:
    """The keyset boundaries of a query - the rows after or before a cursor's values in the query's
    ordering, a NULL compared as the dialect sorts it."""

    #: A bounded cache of field comparison support, cleared with model and lookup registrations.
    ordered_fields: ClassVar[Cache[bool]] = Cache(
        max_size=KEYSET_FIELD_ORDER_CACHE_SIZE,
        holds_sql=False,
        depends_on_other_models=True,
    )

    @staticmethod
    def get_float_bound(
        term: Term, value: float, bound: Any, order: Order, nulls_first: bool
    ) -> tuple[Criterion, Criterion]:
        """The boundary of a float column on a database whose comparisons with NaN are all false while its
        ORDER BY puts NaN between the numbers and the NULLs, on the side the NULLs go to in either direction -
        NaN read as ``x != x``, true for NaN alone.

        Args:
            term: The column.
            value: The boundary value.
            bound: The converted boundary value - a float, or a term casting it as the column holds it.
            order: The column's direction.
            nulls_first: Whether NULL sorts before the values in that direction.

        Returns:
            The comparison reading the rows after the boundary, and the equality of the rows tying with it.
        """
        is_nan = term != term  # noqa: PLR0124
        if math.isnan(value):
            # NULLs first, NaN comes before every number - each follows it; NULLs last, only NULLs do.
            ordinary: Criterion = term == term if nulls_first else BasicCriterion.always_false()  # noqa: PLR0124
            equality: Criterion = is_nan
        else:
            beyond_value = term > bound if order.is_ascending else term < bound
            # NULLs last, NaN comes after every number.
            ordinary = beyond_value if nulls_first else beyond_value | is_nan
            equality = term == bound
        return (ordinary if nulls_first else ordinary | term.isnull()), equality

    @staticmethod
    def compares_by_order(field: Field[Any]) -> bool:
        """Whether the values of a field are compared by ``<`` and ``>`` - a keyset boundary can follow it, a
        field without (an ``HStoreField``) is paged by ``OFFSET``. A ``GeneratedField`` compares as its
        output field.

        Args:
            field: The field ordered by.

        Returns:
            True when both comparisons exist.
        """
        key = (field,)
        ordered = KeysetBounds.ordered_fields.get(key)
        if ordered is None:
            lookups = GeneratedField.get_effective_field(field).get_lookups()
            ordered = Lookup.GT in lookups and Lookup.LT in lookups
            KeysetBounds.ordered_fields[key] = ordered
        return ordered

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

        ``value_wrapper_references``, when a plan is recorded, gets an entry per boundary value, keyed by
        its origin among the query's cursor values (``PlanOrigins.get_value_origin()``): a
        ``CursorValueReference``, or None where no plan can bind the value - one the field writes as NULL,
        or a term of another form. A boundary given as None adds no entry.

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
        context = ExpressionContext(
            model=query.model,
            table=query._effective_basetable(),
            annotations=query._annotations,
            dialect=query.dialect,
            connection=query._connection,
            visibility=query._visibility,
            select_related_extra_conditions=query._select_related_extra_conditions,
        )
        features = query._connection.features if query._connection is not None else query.dialect.features
        criterion: Criterion | None = None
        equalities: list[Criterion] = []
        for index, ((field_name, order), value) in enumerate(
            zip(orderings[: len(cursor_values)], cursor_values, strict=True)
        ):
            bound_path = BoundLookupPath.bind(context, ConcreteFieldPaths.get_path(query.model, field_name))
            for join in bound_path.joins:
                QueryJoins.join_table(query, join)
            ordering = BoundOrdering.bind(context, bound_path.term, bound_path.field, order)
            field = ordering.field
            term = ordering.term
            db_value = ordering.encode_value(value)
            nulls_first = ordering.nulls_first
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
                    comparison = BasicCriterion.always_false()
                # `term = NULL` is never true - a row tying with a NULL boundary needs IS NULL.
                equality: Criterion = term.isnull()
            elif (
                field is not None
                and not features.compares_nan_as_ordered
                and isinstance(GeneratedField.get_effective_field(field), FloatField)
            ):
                comparison, equality = KeysetBounds.get_float_bound(term, value, db_value, order, nulls_first)
                if field.sensitive:
                    FilterKwargs.hide_sensitive_values(comparison)
                    FilterKwargs.hide_sensitive_values(equality)
                if value_wrapper_references is not None:
                    # Whether the boundary is NaN shapes the SQL - its value isn't bound again into a plan.
                    value_wrapper_references.append(
                        (PlanOrigins.get_value_origin(query, cursor_attribute, index), None)
                    )
            else:
                boundary_term, wrapper = ordering.get_boundary_term(db_value)
                ordinary = term > boundary_term if order.is_ascending else term < boundary_term
                comparison = ordinary if nulls_first else ordinary | term.isnull()
                equality = term == boundary_term
                if field is not None and field.sensitive:
                    FilterKwargs.hide_sensitive_values(comparison)
                    FilterKwargs.hide_sensitive_values(equality)
                if value_wrapper_references is not None:
                    # One explicit source covers every occurrence in comparison and tie branches.
                    value_wrapper_references.append(
                        (
                            PlanOrigins.get_value_origin(query, cursor_attribute, index),
                            CursorValueReference(wrapper, field) if field is not None else None,
                        )
                    )
            step = comparison & Criterion.all(equalities) if equalities else comparison
            criterion = step if criterion is None else criterion | step
            equalities.append(equality)
        return criterion
