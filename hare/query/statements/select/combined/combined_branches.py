from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any

from hare.exceptions import FieldError, QueryError
from hare.query.constants import COMBINED_QUERY_APP_COLUMN, COMBINED_QUERY_MODEL_COLUMN
from hare.query.expressions.raw_sql import RawSQL
from hare.query.expressions.value_references.value_reference_types import RecordedValueReferences
from hare.query.query_connection import QueryConnection
from hare.query.queryset.options.query_options import QueryOptions
from hare.query.rewrites.query_rewrites import QueryRewrites
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.select_query import SelectQuery
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.queries.set_operation_query import SetOperationQuery
from hare.sql.enums import SetOperation

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
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
        # Local import: the combined query imports this module.
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.combination.queryset_combination import QuerySetCombination
        from hare.query.queryset.selection.statement_selection import StatementSelection
        from hare.query.statements.select.combined_query import CombinedQuery

        if QuerySetCombination.selects_values(branch) != query._combines_values:
            raise QueryError(
                "Cannot combine model instances with .values()/.values_list() rows - call .values()/"
                ".values_list() on every branch (the rows then take the first branch's shape)."
            )
        # Made again for each set operation made from the queryset - its values come from the branch.
        branch._build_conditions_for_copies()
        source_branch = branch
        branch_query: SelectQuery[Any] | CombinedQuery
        if branch._combination is not None:
            branch_query = CombinedQuery(branch)
        else:
            if branch._options is not QueryOptions.DEFAULT:
                branch = QueryRewrites.get_rewritten(branch)
            branch_query = (
                StatementSelection.get_values_query(branch) if query._combines_values else ModelRowsQuery(branch)
            )
        branch_query._plan_origin = source_branch
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
    def get_prepared_branch(branch: SelectQuery[Any] | CombinedQuery) -> SelectQuery[Any] | CombinedQuery:
        """A copy of a branch as it is built into the combined statement: an unsliced branch
        unordered - the order of its rows doesn't change the combined rows - unless it is a
        ``distinct(*fields)`` one, whose ordering picks the rows it keeps; never ordered by
        ``Meta.ordering``; a branch of model instances tagged with its model.

        Args:
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
        # A distinct(*fields) branch keeps its ordering - the ordering picks the row it keeps.
        if (
            not is_sliced
            and not prepared_branch._distinct_on
            and not prepared_branch._cursor_values
            and not prepared_branch._before_cursor_values
        ):
            prepared_branch._orderings = []
        prepared_branch._default_ordering_disabled = True
        if isinstance(prepared_branch, ModelRowsQuery):
            CombinedBranches.tag_branch_with_model(prepared_branch)
        return prepared_branch

    @staticmethod
    def tag_branch_with_model(branch: ModelRowsQuery[Any]) -> None:
        """Checks a branch of model instances can be combined, and selects the two columns naming
        its model.

        Args:
            branch: The branch's query.

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
        # An .alias() key is never selected, so it can't collide.
        colliding_keys = branch.model._meta.fields_map.keys() & (branch._annotations.keys() - branch._alias_keys)
        if colliding_keys:
            raise FieldError(
                f"annotate() key(s) {sorted(colliding_keys)} collide with existing field(s) on "
                f"model {branch.model.__name__} - fetching full model instances would silently "
                "corrupt hydration. Use a different annotation name, or .values()/.values_list()."
            )
        # SQL string literals, not bound values: constant for the branch's model, so the plan of
        # the combined statement binds none of them.
        branch._annotations = {
            **branch._annotations,
            COMBINED_QUERY_APP_COLUMN: RawSQL(CombinedBranches.get_string_literal(branch.model._meta.app)),
            COMBINED_QUERY_MODEL_COLUMN: RawSQL(
                CombinedBranches.get_string_literal(branch.model._meta._model.__name__)
            ),
        }

    @staticmethod
    def get_string_literal(text: str | None) -> str:
        """An SQL string literal of a text.

        Args:
            text: The text, None for none.

        Returns:
            The literal, its quotes doubled - ``NULL`` for None.
        """
        if text is None:
            return "NULL"
        # Local import: the dialect constants import the query layer through the dialect.
        from hare.dialects.base.constants import SQL_DIALECT

        return SQL_DIALECT.literals.get_string_literal_sql(text)

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
        branch_connection = branch._connection or branch.get_connection()
        if query._connection is not None and branch_connection.connection_alias != query._connection.connection_alias:
            # Every branch's rows live on the connection it runs on by itself - its model's own
            # connection, or the router's choice - and the combined statement runs on one.
            raise QueryError(
                f"{branch.model.__name__} query used as {embedded_as} runs on a different database "
                f"connection ({branch_connection.connection_alias!r}) than the {query.model.__name__} query it is "
                f"combined with ({query._connection.connection_alias!r}) - the combined statement runs on one "
                "connection. Query each connection separately instead."
            )
        built_branch = QueryConnection.get_bound_to(
            CombinedBranches.get_prepared_branch(branch), query._connection, query.model, embedded_as
        )
        if value_wrapper_references is not None:
            built_branch._make_subquery(value_wrapper_references=value_wrapper_references)
        else:
            built_branch._make_subquery()
        query._rows.take_branch(built_branch, branch_index)
        branch_query = built_branch.query
        if (
            isinstance(built_branch, CombinedQuery)
            or branch_query._orderbys
            or branch_query._limit is not None
            or branch_query._offset is not None
        ):
            inner_query = copy(branch_query)
            with_clauses, inner_query._with = inner_query._with, []
            branch_query = query._connection.query_class.from_(inner_query).select(
                *(inner_query.field(alias).as_(alias) for alias in query._rows.get_branch_aliases(built_branch))
            )
            branch_query._with = with_clauses
        return branch_query, dict(built_branch._with_ctes)

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
            # INTERSECT binds tighter than UNION/EXCEPT on Postgres - the chain so far is
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
            case _:  # pragma: nocoverage
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
        column_names = [select.alias or select.name for select in first_branch_query._selects]
        derived_table_query = query._connection.query_class.from_(combined_query).select(
            *(combined_query.field(column_name) for column_name in column_names)
        )
        derived_table_query.wrap_set_operation_queries = False
        return derived_table_query
