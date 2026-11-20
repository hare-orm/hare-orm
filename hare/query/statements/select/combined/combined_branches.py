from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.query.expressions.term_expression import TermExpression
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.query_connection import QueryConnection
from hare.query.queryset.row_multiplication import RowMultiplication
from hare.query.statements.constants import VALUES_SET_OPERATION_COLUMN_PREFIX
from hare.query.statements.projections.derived_projection import DerivedProjection
from hare.query.statements.select.model_rows.instance_hydration import InstanceHydration
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.select_query import SelectQuery
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.enums import SetOperation
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.query_specification import QuerySpecification
    from hare.query.queryset.queryset import QuerySet
    from hare.query.rows.model_rows.combined_model_rows import CombinedModelRows
    from hare.query.rows.values_rows.combined_values_rows import CombinedValuesRows
    from hare.query.statements.select.combined_query import CombinedQuery


class CombinedBranches:
    """The branches of a set operation as they are built into it: each prepared on the query's
    connection, tagged with the model of its instances, its CTEs merged with the other branches'
    ones, and a set operation built so far wrapped as a derived table for the next operator."""

    @staticmethod
    def get_branch_query(query: CombinedQuery, branch: QuerySet[Any, Any]) -> SelectQuery[Any] | CombinedQuery:
        """The query building a branch.

        Args:
            query: The set operation.
            branch: One branch of the set operation.
        """
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.combination.queryset_combination import QuerySetCombination
        from hare.query.queryset.pending_calls.calls_before_setup import CallsBeforeSetup
        from hare.query.queryset.selection.statement_selection import StatementSelection

        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        branch = CallsBeforeSetup.bind(branch, query._context, model_binding.graph)
        if QuerySetCombination.selects_values(branch) != query._combines_values:
            raise QueryError(
                "Cannot combine model instances with .values()/.values_list() rows - call .values()/"
                ".values_list() on every branch (the rows then take the first branch's shape)."
            )
        # Made again for each set operation made from the queryset - its values come from the branch.
        branch._build_conditions_for_copies()
        branch_query = StatementSelection.get_select_query(branch)
        branch_query._plan_origin = branch
        return branch_query

    @staticmethod
    def get_first_leaf(query: CombinedQuery) -> SelectQuery[Any]:
        """The first branch that isn't a set operation itself - the rows take its shape.

        Args:
            query: The set operation.
        """
        # Local import: the combined query imports this module.
        from hare.query.statements.select.combined_query import CombinedQuery

        first_branch = query._branches[0]
        return (
            CombinedBranches.get_first_leaf(first_branch) if isinstance(first_branch, CombinedQuery) else first_branch
        )

    @staticmethod
    def get_models(query: CombinedQuery) -> set[type[Model]]:
        """The models the combined rows can be instances of.

        Args:
            query: The set operation.
        """
        # Local import: the combined query imports this module.
        from hare.query.statements.select.combined_query import CombinedQuery

        models: set[type[Model]] = set()
        for branch in query._branches:
            models.update(CombinedBranches.get_models(branch) if isinstance(branch, CombinedQuery) else {branch.model})
        return models

    @staticmethod
    def get_prepared_branch(
        query: CombinedQuery, branch: SelectQuery[Any] | CombinedQuery
    ) -> SelectQuery[Any] | CombinedQuery:
        """A copy of a branch as it is built into the combined statement, tagged with its model
        when it returns instances. Ordering is retained when a slice, distinct selection, cursor
        boundary or row-multiplying join depends on it, including ordering inherited from Meta.
        Other branches are unordered: their row order doesn't change the combined rows.

        Args:
            query: The operation the branch is built into.
            branch: The branch.

        Returns:
            The copy.

        Raises:
            QueryError: The branch locks its rows, loads relations, or a nested set operation
                prefetches them.
            FieldError: An annotation of a branch of model instances is named after a field.
        """
        # Local import: the combined query imports this module.
        from hare.query.statements.select.combined_query import CombinedQuery

        branch._build_conditions_for_copies()
        prepared_branch = copy(branch)
        # Made for each description and build - its values come from the branch.
        prepared_branch._plan_origin = branch
        if isinstance(prepared_branch, CombinedQuery):
            if prepared_branch._prefetched_relations:
                raise QueryError(
                    "Union queries do not support prefetch_related() on a nested "
                    "union()/intersection()/difference() - apply it to the outer result instead"
                )
            return prepared_branch
        if prepared_branch._select_for_update:
            raise QueryError(
                "select_for_update() can't be used on a branch of union()/intersection()/difference() - SQL "
                "can't lock the rows of a set operation (FOR UPDATE is not allowed with UNION/INTERSECT/"
                "EXCEPT). Lock the rows in a separate query, e.g. "
                "Model.objects.filter(pk__in=...).select_for_update()."
            )
        is_sliced = prepared_branch._limit is not None or bool(prepared_branch._offset)
        # The ordering can pick distinct rows or add joins that change the branch's multiplicity.
        if (
            not is_sliced
            and not prepared_branch._distinct
            and not prepared_branch._distinct_on
            and not prepared_branch._cursor_values
            and not prepared_branch._before_cursor_values
            and not RowMultiplication.ordering_joins_multi_valued_relation(prepared_branch)
        ):
            prepared_branch._orderings = []
        else:
            prepared_branch._orderings = list(prepared_branch._apply_default_ordering(prepared_branch._orderings))
        prepared_branch._default_ordering_disabled = True
        if isinstance(prepared_branch, ModelRowsQuery):
            model_rows: CombinedModelRows = query._rows  # type: ignore[assignment]
            CombinedBranches.tag_branch_with_model(prepared_branch, (model_rows.app_column, model_rows.model_column))
        return prepared_branch

    @staticmethod
    def tag_branch_with_model(branch: ModelRowsQuery[Any], model_column_names: tuple[str, str]) -> None:
        """Checks a branch of model instances can be combined, and selects the two columns naming
        its model.

        Args:
            branch: The branch's query.
            model_column_names: The names of the two columns.

        Raises:
            QueryError: The branch loads relations - a loaded relation can't survive being merged
                into the combined rows.
            FieldError: An annotation is named after a field of the model - reading the instance
                would overwrite the field's value with the annotation's.
        """
        # A relation's field-level lazy="joined"/"select" default counts like an explicit
        # select_related()/prefetch_related().
        if branch._loads_relations():
            raise QueryError(
                "Union queries do not support select_related()/prefetch_related() on the "
                "individual querysets - the loaded relation cannot survive being merged into "
                "the combined result"
            )
        InstanceHydration.check_no_annotation_field_collision(branch)
        # SQL string literals, not bound values: constant for the branch's model, so the plan of
        # the combined statement binds none of them.
        app_column, model_column = model_column_names
        model_binding: ModelBinding = branch._binding  # type: ignore[assignment]
        branch._annotations = {
            **branch._annotations,
            app_column: TermExpression(ValueWrapper(model_binding.app, allow_parametrize=False)),
            model_column: TermExpression(ValueWrapper(model_binding._model.__name__, allow_parametrize=False)),
        }

    @staticmethod
    def get_branch_statement(
        query: CombinedQuery,
        branch: SelectQuery[Any] | CombinedQuery,
        branch_index: int,
        value_wrapper_references: RecordedValueReferences | None,
    ) -> tuple[QueryBuilder, dict[str, Any]]:
        """Builds one branch's SELECT on this query's connection.

        An ordered or sliced branch, and a nested set operation, is a derived table selecting its
        columns - SQL allows ORDER BY/LIMIT only on the whole combined statement.

        Args:
            query: The set operation.
            branch: The branch.
            branch_index: Its position among the branches.
            value_wrapper_references: The list the branch records its value references into, while the
                plan of the combined statement is recorded.

        Returns:
            The SELECT, and the ``with_cte()`` body of each CTE it carries, by name.

        Raises:
            QueryError: The branch runs on another connection.
        """
        # Local import: the combined query imports this module.
        from hare.query.statements.select.combined_query import CombinedQuery

        embedded_as = "a branch of union()/intersection()/difference()"
        declaration: QuerySpecification[Any] = branch
        pinned_client = declaration._connection
        branch_connection = pinned_client or branch.get_connection()
        if (
            pinned_client is None
            and query._connection is not None
            and branch_connection.connection_alias != query._connection.connection_alias
        ):
            # Every branch's rows live on the connection it runs on by itself - its model's own
            # connection, or the router's choice - and the combined statement runs on one.
            raise QueryError(
                f"{branch.model.__name__} query used as {embedded_as} runs on a different database "
                f"connection ({branch_connection.connection_alias!r}) than the {query.model.__name__} query it is "
                f"combined with ({query._connection.connection_alias!r}) - the combined statement runs on one "
                "connection. Query each connection separately instead."
            )
        model_binding: ModelBinding = query._binding  # type: ignore[assignment]
        # Binding an existing statement keeps its executor type.
        built_branch: SelectQuery[Any] | CombinedQuery = QueryConnection.get_bound_to(  # type: ignore[assignment]
            CombinedBranches.get_prepared_branch(query, branch),
            query._connection,
            model_binding,
            embedded_as,
            context=query._context,
        )
        built_branch._make_subquery(value_wrapper_references=value_wrapper_references)
        if query._combines_values:
            values_rows: CombinedValuesRows = query._rows  # type: ignore[assignment]
            values_rows.take_branch(built_branch, branch_index)
            branch_query = built_branch.query
            aliases = values_rows.get_branch_aliases(built_branch)
        else:
            model_rows: CombinedModelRows = query._rows  # type: ignore[assignment]
            public_columns = model_rows.take_branch(built_branch)
            branch_query, aliases = model_rows.get_branch_projection(built_branch, public_columns)
        branch_query = CombinedBranches.get_namespaced_branch(branch_query, aliases)
        aliases = [f"{VALUES_SET_OPERATION_COLUMN_PREFIX}{position}" for position in range(len(aliases))]
        if (
            isinstance(built_branch, CombinedQuery)
            or branch_query._orderbys
            or branch_query._limit is not None
            or branch_query._offset is not None
        ):
            inner_query = copy(branch_query)
            with_clauses, inner_query._with = inner_query._with, []
            branch_query = query._connection.query_class.from_(inner_query).select(
                *(inner_query.field(alias).as_(alias) for alias in aliases)
            )
            branch_query._with = with_clauses
        return branch_query, dict(built_branch._with_ctes)

    @staticmethod
    def get_namespaced_branch(branch_query: QueryBuilder, aliases: list[str]) -> QueryBuilder:
        """Give every branch positional internal column names before it becomes a derived table.

        Public field and annotation names are restored by the final SELECT, where the reader
        preserves case. Ordering and grouping references stay bound to their selected expressions.

        Args:
            branch_query: The built branch, including ordering and slicing.
            aliases: Its output names in result order.

        Returns:
            The branch with internal output names and updated references.
        """
        ordered_query = DerivedProjection.get_selects_in_output_order(branch_query, aliases)
        return DerivedProjection(ordered_query, column_prefix=VALUES_SET_OPERATION_COLUMN_PREFIX).query

    @staticmethod
    def hoist_with_clauses(
        branch_query: Any,
        cte_bodies_by_name: dict[str, Any],
        with_clauses: list[Any],
        with_clauses_by_name: dict[str, tuple[Any, Any]],
    ) -> None:
        """Takes a branch's ``with_cte()`` CTEs to the combined statement as a whole - a WITH clause
        rendered on a branch would land after UNION/INTERSECT/EXCEPT. A CTE of a name already taken
        must be the same as the first one.

        Args:
            branch_query: The branch's statement.
            cte_bodies_by_name: The ``with_cte()`` body each of its CTEs was built from.
            with_clauses: The combined statement's CTEs - added to.
            with_clauses_by_name: The first CTE of each name and its body - added to.

        Raises:
            QueryError: Branches define a CTE of one name differently.
        """
        for with_clause in branch_query._with:
            cte_body = cte_bodies_by_name.get(with_clause.alias, with_clause.query)
            existing = with_clauses_by_name.get(with_clause.alias)
            if existing is None:
                with_clauses_by_name[with_clause.alias] = (with_clause, cte_body)
                with_clauses.append(with_clause)
            elif not CombinedBranches.cte_bodies_match(*existing, with_clause, cte_body):
                raise QueryError(
                    f"with_cte({with_clause.alias!r}, ...) is defined differently across "
                    "union()/intersection()/difference() branches - give each branch's own "
                    "CTE body a distinct name, or reuse the exact same .with_cte() call on "
                    "every branch."
                )

    @staticmethod
    def combine(
        query: CombinedQuery,
        combined_query: QueryBuilder | SetOperationQuery,
        branch_query: QueryBuilder,
        branch_index: int,
    ) -> SetOperationQuery:
        """The branches so far combined with the next one by its set operation.

        Args:
            query: The set operation.
            combined_query: The statement of the branches so far.
            branch_query: The next branch's statement.
            branch_index: The next branch's position.

        Returns:
            The combined statement.

        Raises:
            QueryError: The set operation is unknown.
        """
        set_operation = query._set_operations[branch_index - 1]
        if set_operation == SetOperation.INTERSECT and any(
            previous_operation != SetOperation.INTERSECT
            for previous_operation in query._set_operations[: branch_index - 1]
        ):
            # INTERSECT binds tighter than UNION/EXCEPT in SQL - the chain so far is
            # combined as one derived table, so it applies to all of it, as chained.
            combined_query = CombinedBranches.get_derived_table_query(query, combined_query)
        match set_operation:
            case SetOperation.UNION:
                return combined_query.union(branch_query)
            case SetOperation.UNION_ALL:
                return combined_query.union_all(branch_query)
            case SetOperation.INTERSECT:
                return combined_query.intersect(branch_query)
            case SetOperation.EXCEPT_OF:
                return combined_query.except_of(branch_query)
            case _:
                raise QueryError(f"Unsupported set operation: {set_operation!r}")

    @staticmethod
    def cte_bodies_match(existing_with_clause: Any, existing_body: Any, with_clause: Any, body: Any) -> bool:
        """Whether two branches' same-named CTEs are one definition - the same ``with_cte()``
        body (each branch compiles it into its own query object), or bodies rendering the same
        SQL with the same parameters.

        Args:
            existing_with_clause: The first branch's compiled CTE.
            existing_body: The ``with_cte()`` body it was compiled from.
            with_clause: This branch's compiled CTE.
            body: The ``with_cte()`` body it was compiled from.

        Returns:
            True when both define the same CTE.
        """
        if existing_body is body or existing_with_clause.query is with_clause.query:
            return True
        if isinstance(existing_with_clause.query, QueryBuilder) and isinstance(with_clause.query, QueryBuilder):
            return existing_with_clause.query.get_parameterized_sql() == with_clause.query.get_parameterized_sql()
        return False

    @staticmethod
    def get_derived_table_query(
        query: CombinedQuery, combined_query: QueryBuilder | SetOperationQuery
    ) -> QueryBuilder:
        """Selects every column of the set operations built so far from a derived table of them,
        so a following operator applies to their whole result.

        Args:
            query: The set operation.
            combined_query: The set operations built so far.

        Returns:
            The query selecting from the derived table, in the same column order.
        """
        first_branch_query = (
            combined_query.base_query if isinstance(combined_query, SetOperationQuery) else combined_query
        )
        column_names = [select.alias or select.name for select in first_branch_query._selects]  # type: ignore[attr-defined]
        derived_table_query = query._connection.query_class.from_(combined_query).select(
            *(combined_query.field(column_name) for column_name in column_names)
        )
        derived_table_query.wrap_set_operation_queries = False
        return derived_table_query
