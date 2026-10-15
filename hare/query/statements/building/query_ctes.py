from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.expressions.constants import UNBINDABLE_VALUE_ORIGIN
from hare.query.expressions.subqueries.outer_query_state import outer_expression_context, outer_extra_joins
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.query_connection import QueryConnection
from hare.sql.builder.tables.selectable import Selectable

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.statements.awaitable_query import AwaitableQuery


class QueryCtes:
    """The CTEs of a query - each body built into the query under its name, its values recorded for the
    plan."""

    @staticmethod
    def apply_with_ctes(
        query: AwaitableQuery[Any], value_wrapper_references: RecordedValueReferences | None = None
    ) -> None:
        """Attaches every ``with_cte()`` CTE to ``self.query``. Runs after every other reassignment of
        ``self.query``. A CTE body is built with the enclosing query's correlation context cleared -
        a ``WITH`` body can't reference the enclosing FROM items.

        Args:
            query: The query.
            value_wrapper_references: The list this query records its value references into - each CTE
                body's go there too, in order; a body recording none adds an empty reference, so no
                plan is kept.
        """
        if not query._with_ctes:
            return
        # Local import: the query module imports this one.
        from hare.query.statements.awaitable_query import AwaitableQuery

        for name, cte_body in query._with_ctes:
            if isinstance(cte_body, AwaitableQuery):
                body_query = QueryConnection.get_bound_to(
                    cte_body, query._connection, query.model, f"the body of with_cte({name!r}, ...)"
                )
                # A relation's field-level lazy="joined"/"select" default counts like an explicit
                # select_related()/prefetch_related().
                if body_query._loads_relations():
                    # A CTE body is never run by itself - relations it would load afterwards are
                    # never loaded.
                    raise QueryError(
                        "CTE bodies do not support select_related()/prefetch_related() - the "
                        "loaded relation cannot survive being folded into a WITH clause"
                    )
                outer_token = outer_expression_context.set(None)
                joins_token = outer_extra_joins.set(None)
                try:
                    if value_wrapper_references is not None and body_query.plannable:
                        body_query._make_subquery(value_wrapper_references=value_wrapper_references)
                    else:
                        body_query._make_subquery()
                        if value_wrapper_references is not None:
                            value_wrapper_references.append((UNBINDABLE_VALUE_ORIGIN, None))
                finally:
                    outer_expression_context.reset(outer_token)
                    outer_extra_joins.reset(joins_token)
                cte_query: Selectable = body_query.query
            else:
                cte_query = cte_body
                if value_wrapper_references is not None:
                    value_wrapper_references.append((UNBINDABLE_VALUE_ORIGIN, None))
            query.query = query.query.with_(cte_query, name)
