from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import UnSupportedError
from hare.sql.builder.joins.join_on import JoinOn
from hare.sql.builder.queries.constants import ROW_COUNT_FREE_AGGREGATE_NAMES
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.builder.tables.table import Table
from hare.sql.constants import CORRELATED_CONDITION_PREFIX, CORRELATED_SORT_KEY_PREFIX
from hare.sql.enums import Boolean, Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.complex_criterion import ComplexCriterion
from hare.sql.terms.criteria.empty_criterion import EmptyCriterion
from hare.sql.terms.field import Field
from hare.sql.terms.functions.aggregate_function import AggregateFunction
from hare.sql.terms.qualified_outer_field import QualifiedOuterField
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:
    from collections.abc import Iterator

    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


class CorrelatedSubqueries:
    """Subqueries reading the columns of the query around them, on a database that has none
    (``Features.supports_correlated_subqueries``). An ``EXISTS`` correlated by equalities of its
    columns with the outer query's is written as a membership test of the outer columns in its
    rows; any other correlated subquery is refused before the statement is sent."""

    @staticmethod
    def get_table_name(table: Any) -> str | None:
        """The name a column's table is referenced by - its alias, else its name.

        Args:
            table: A ``Table``, an aliased subquery or a plain name.

        Returns:
            The name, None for a column of no table.
        """
        if table is None:
            return None
        if isinstance(table, str):
            return table
        if isinstance(table, Table):
            return table.get_table_name()
        if isinstance(table, Selectable):
            return table.alias
        return None

    @staticmethod
    def iterate_query_terms(query: QueryBuilder) -> Iterator[Term]:
        """Every term of one query - selected, assigned by an UPDATE, filtered, grouped, ordered and
        joined on - without the terms of the queries nested in them.

        Args:
            query: The query.

        Yields:
            The terms.
        """
        yield from query._selects
        for _column, assigned_term in query._updates:
            yield assigned_term
        if query._wheres is not None:
            yield query._wheres
        yield from query._groupbys
        if query._havings is not None:
            yield query._havings
        for term, _order in query._orderbys:
            yield term
        for join in query._joins:
            if isinstance(join, JoinOn):
                yield join.criterion

    @classmethod
    def iterate_nested_queries(cls, query: QueryBuilder) -> Iterator[tuple[QueryBuilder, bool]]:
        """The queries nested one level in ``query`` - in its FROM, its joins and its terms.

        Args:
            query: The query.

        Yields:
            Each nested query, and whether a term rewriting its own correlation holds it.
        """
        for table in query._from:
            if isinstance(table, QueryBuilder):
                yield table, False
        for join in query._joins:
            if isinstance(join.item, QueryBuilder):
                yield join.item, False
        for cte in query._with:
            if isinstance(cte.query, QueryBuilder):
                yield cte.query, False
        for term in cls.iterate_query_terms(query):
            nodes: list[Any] = list(term.nodes_())
            # A rewriting term's nodes hold its query too - yielded once, as the term's.
            rewritten_queries = [
                node.inner_query for node in nodes if getattr(type(node), "rewrites_inner_correlation", False)
            ]
            for rewritten_query in rewritten_queries:
                yield rewritten_query, True
            for node in nodes:
                if (
                    getattr(type(node), "holds_nested_query", False)
                    and (nested_query := node.get_nested_query()) is not None
                ):
                    yield nested_query, False
                elif (
                    isinstance(node, QueryBuilder)
                    and node is not query
                    and not any(node is rewritten_query for rewritten_query in rewritten_queries)
                ):
                    yield node, False

    @classmethod
    def get_declared_table_names(cls, query: QueryBuilder) -> set[str]:
        """The names of every table ``query`` and the queries nested in it read from.

        Args:
            query: The query.

        Returns:
            The names.
        """
        names = {
            name
            for table in (*query._from, *(join.item for join in query._joins))
            if (name := cls.get_table_name(table)) is not None
        }
        names.update(cte.name for cte in query._with)
        for nested_query, _rewrites in cls.iterate_nested_queries(query):
            names |= cls.get_declared_table_names(nested_query)
        return names

    @classmethod
    def reads_other_tables(cls, term: Term, declared_names: set[str]) -> bool:
        """Whether ``term`` reads a column of a table outside ``declared_names``.

        Args:
            term: The term.
            declared_names: The tables the term's own query reads from.

        Returns:
            True for a term reading the query around its own.
        """
        node: Any
        for node in term.nodes_():
            if isinstance(node, QueryBuilder):
                if cls.query_reads_other_tables(node, declared_names):
                    return True
            elif getattr(type(node), "rewrites_inner_correlation", False):
                if cls.query_reads_other_tables(node.inner_query, declared_names):
                    return True
            elif getattr(type(node), "holds_nested_query", False):
                nested_query = node.get_nested_query()
                if nested_query is not None and cls.query_reads_other_tables(nested_query, declared_names):
                    return True
            elif isinstance(node, (Field, QualifiedOuterField)):
                name = cls.get_table_name(node.table)
                if name is not None and name not in declared_names:
                    return True
        return False

    @classmethod
    def query_reads_other_tables(cls, query: QueryBuilder, declared_names: set[str]) -> bool:
        """Whether ``query`` reads a column of a table it doesn't read from itself.

        Args:
            query: The query.
            declared_names: The tables of the query around it that count as its own.

        Returns:
            True for a correlated query.
        """
        method_parameter_names = declared_names | cls.get_declared_table_names(query)
        return any(cls.reads_other_tables(term, method_parameter_names) for term in cls.iterate_query_terms(query))

    @classmethod
    def check_statement(cls, query: QueryBuilder, sql_context: SqlContext) -> None:
        """Refuses a statement holding a correlated subquery the database can't run - an ``EXISTS``
        writes its own correlation away, or refuses it itself; where correlated subqueries run, one
        ordering or limiting its own rows the database runs none of.

        Args:
            query: The statement.
            sql_context: The context it renders in.

        Raises:
            UnSupportedError: A subquery reads the columns of the query around it, or the rows are grouped
                by one where the database groups by none.
        """
        features = sql_context.dialect.features
        runs_correlation = features.supports_correlated_subqueries
        writes_rows = query._update_table is not None or bool(query._delete_from)
        if (
            not features.orders_by_correlated_subqueries
            and query._groupbys
            and cls.holds_correlation(query._groupbys, query)
        ):
            raise UnSupportedError(
                f"The {sql_context.dialect.name} database groups rows by no correlated subquery - a subquery "
                "reading the columns of the query around it can't be a key of values().annotate() or group_by() "
                "there"
            )
        for nested_query, rewrites in cls.iterate_nested_queries(query):
            if (
                rewrites
                and writes_rows
                and not features.orders_by_correlated_subqueries
                and cls.split_conditions(nested_query, cls.get_declared_table_names(nested_query))[2]
            ):
                # An EXISTS no membership test says runs as the database's own.
                raise cls.get_mutation_error(sql_context)
            if not rewrites and cls.query_reads_other_tables(nested_query, set()):
                if not runs_correlation:
                    raise cls.get_error(sql_context)
                if writes_rows and not features.orders_by_correlated_subqueries:
                    raise cls.get_mutation_error(sql_context)
                if nested_query._orderbys or nested_query._limit is not None or nested_query._offset is not None:
                    raise UnSupportedError(
                        f"The {sql_context.dialect.name} database runs no correlated subquery ordering or "
                        "limiting its own rows - a subquery reading the columns of the query around it takes no "
                        "order_by() or slice there; an aggregate (Max, Min) picks one row instead"
                    )
                if not features.supports_correlated_aggregates:
                    cls.raise_if_aggregate_counts_rows(nested_query, sql_context)
            cls.check_statement(nested_query, sql_context)

    @staticmethod
    def raise_if_aggregate_counts_rows(query: QueryBuilder, sql_context: SqlContext) -> None:
        """Refuses a correlated subquery selecting an aggregate a row read more than once changes,
        where the database reads each row once per outer row of its key
        (``Features.supports_correlated_aggregates``).

        Args:
            query: The subquery.
            sql_context: The context it renders in.

        Raises:
            UnSupportedError: It selects such an aggregate, correlated by a column of the query around
                it that two of its rows may share.
        """
        outer_fields: list[QualifiedOuterField] = []
        for condition in (query._wheres, query._havings):
            if condition is not None:
                nodes: list[Any] = list(condition.nodes_())
                outer_fields.extend(node for node in nodes if isinstance(node, QualifiedOuterField))
        # Correlated by a key of the rows around it - one of them reads each row of the subquery.
        if outer_fields and all(outer_field.reads_unique_column for outer_field in outer_fields):
            return
        for select in query._selects:
            selected_nodes: list[Any] = list(select.nodes_())
            for node in selected_nodes:
                if isinstance(node, AggregateFunction) and node.name.upper() not in ROW_COUNT_FREE_AGGREGATE_NAMES:
                    raise UnSupportedError(
                        f"The {sql_context.dialect.name} database computes a {node.name.upper()} of a correlated "
                        "subquery once per row of the query around it holding its key - aggregate the rows with "
                        "annotate() over the relation instead (Count('books')), or use Max, Min or Avg"
                    )

    @classmethod
    def holds_correlation(cls, terms: Any, query: QueryBuilder) -> bool:
        """Whether terms hold a subquery reading the columns of the query around it.

        Args:
            terms: The terms.
            query: The query they belong to.

        Returns:
            Whether one does.
        """
        for term in terms:
            for node in term.nodes_():
                nested_query = (
                    node.get_nested_query()
                    if getattr(type(node), "holds_nested_query", False)
                    else node
                    if isinstance(node, QueryBuilder) and node is not query
                    else None
                )
                if nested_query is not None and cls.query_reads_other_tables(nested_query, set()):
                    return True
        return False

    @classmethod
    def needs_derived_table(cls, query: QueryBuilder) -> bool:
        """Whether a query runs only as a derived table - sorted by a correlated subquery, or selecting
        one beside a condition.

        Args:
            query: The query.

        Returns:
            Whether it does.
        """
        if query._orderbys and cls.holds_correlation([term for term, _direction in query._orderbys], query):
            return True
        # An empty condition is no condition - the query runs as it is.
        has_condition = not isinstance(query._wheres, (type(None), EmptyCriterion))
        return has_condition and cls.holds_correlation(query._selects, query)

    @classmethod
    def get_derived_table_sql(cls, query: QueryBuilder, sql_context: SqlContext) -> str:
        """A query of correlated subqueries the database runs only in a derived table: its rows selected
        there without condition, each of its conditions and sort keys a hidden column of them; the rows
        of the derived table filtered, sorted and sliced, the hidden columns left out.

        Args:
            query: The query.
            sql_context: The context it renders in.

        Returns:
            The SQL.

        Raises:
            UnSupportedError: The query groups or aggregates its rows, or picks distinct ones.
        """
        if (
            query._groupbys
            or not isinstance(query._havings, (type(None), EmptyCriterion))
            or query._distinct
            or any(select.is_aggregate for select in query._selects)
            or query._dialect_clauses
        ):
            raise UnSupportedError(
                f"The {sql_context.dialect.name} database runs a correlated subquery selected beside a condition, "
                "or sorted by, only in a derived table - which a query grouping, aggregating or picking distinct "
                "rows can't be split into"
            )
        conditions = [
            condition
            for condition in (cls.iterate_conjuncts(query._wheres) if query._wheres is not None else ())
            if not isinstance(condition, EmptyCriterion)
        ]
        condition_names = [f"{CORRELATED_CONDITION_PREFIX}{position}" for position in range(len(conditions))]
        sort_key_names = [f"{CORRELATED_SORT_KEY_PREFIX}{position}" for position in range(len(query._orderbys))]
        inner_query = copy(query)
        inner_query._selects = [
            *query._selects,
            *(
                cast("Field", copy(condition).as_(name))
                for condition, name in zip(conditions, condition_names, strict=True)
            ),
            *(
                cast("Field", copy(term).as_(name))
                for (term, _direction), name in zip(query._orderbys, sort_key_names, strict=True)
            ),
        ]
        inner_query._wheres = None
        inner_query._orderbys = []
        inner_query._limit = None
        inner_query._offset = None
        statement_context = sql_context.copy(subquery=False, with_alias=False)
        inner_sql = inner_query.get_sql(statement_context.copy(subquery=True))
        quoted_conditions = [statement_context.quote_alias(name) for name in condition_names]
        quoted_sort_keys = [statement_context.quote_alias(name) for name in sort_key_names]
        sql = f"SELECT * EXCEPT ({','.join([*quoted_conditions, *quoted_sort_keys])}) FROM {inner_sql}"  # nosec B608
        if quoted_conditions:
            sql += f" WHERE {' AND '.join(quoted_conditions)}"
        if quoted_sort_keys:
            sql += " ORDER BY " + ",".join(
                f"{quoted_name} {sql_context.dialect.renderers.get_order_sql(direction)}"
                if direction is not None
                else quoted_name
                for quoted_name, (_term, direction) in zip(quoted_sort_keys, query._orderbys, strict=True)
            )
        sql += query._limit_offset_sql(statement_context)
        if sql_context.subquery:
            sql = f"({sql})"
        if sql_context.with_alias and query.alias:
            return sql_context.format_alias_sql(sql, query.alias)
        return sql

    @staticmethod
    def get_mutation_error(sql_context: SqlContext) -> UnSupportedError:
        """The refusal of a correlated subquery in an ``UPDATE`` or a ``DELETE``.

        Args:
            sql_context: The context the statement renders in.

        Returns:
            The error.
        """
        return UnSupportedError(
            f"The {sql_context.dialect.name} database runs no correlated subquery in an UPDATE or a "
            "DELETE - a subquery reading the columns of the rows written (OuterReference) can't run "
            "there; read the values with a query and write them with bulk_update()"
        )

    @staticmethod
    def get_error(sql_context: SqlContext) -> UnSupportedError:
        """The refusal of a correlated subquery.

        Args:
            sql_context: The context the statement renders in.

        Returns:
            The error.
        """
        if sql_context.dialect.features.supports_correlated_subqueries:
            return UnSupportedError(
                f"The {sql_context.dialect.name} database runs a correlated EXISTS correlated in its WHERE alone, "
                "and by equal columns alone when it groups or aggregates its rows - this subquery reads the "
                "columns of the query around it otherwise"
            )
        return UnSupportedError(
            f"The {sql_context.dialect.name} database has no correlated subqueries - a subquery reading the "
            "columns of the query around it (OuterReference, a filter across a to-many relation other than by its "
            "key) can't run there"
        )

    @staticmethod
    def iterate_conjuncts(criterion: Term) -> Iterator[Term]:
        """The parts of an ``AND`` of conditions.

        Args:
            criterion: The condition.

        Yields:
            Each part.
        """
        if isinstance(criterion, ComplexCriterion) and criterion.comparator == Boolean.AND:
            yield from CorrelatedSubqueries.iterate_conjuncts(criterion.left)
            yield from CorrelatedSubqueries.iterate_conjuncts(criterion.right)
        else:
            yield criterion

    @classmethod
    def get_correlation_pairs(cls, criterion: Term, declared_names: set[str]) -> list[tuple[Term, Term]] | None:
        """The inner and the outer columns of an equality correlating a subquery - of a column, or of
        a row of them (a composite key).

        Args:
            criterion: A condition of the subquery reading the query around it.
            declared_names: The tables of the subquery.

        Returns:
            ``(inner column, outer column)`` per column, None for any other condition.
        """
        if type(criterion) is not BasicCriterion or criterion.comparator != Equality.EQ:
            return None
        left, right = criterion.left, criterion.right
        if isinstance(left, Tuple) and isinstance(right, Tuple):
            if len(left.values) != len(right.values):
                return None
            pairs = [
                cls.get_column_pair(*sides, declared_names) for sides in zip(left.values, right.values, strict=True)
            ]
            return None if any(pair is None for pair in pairs) else cast("list[tuple[Term, Term]]", pairs)
        pair = cls.get_column_pair(left, right, declared_names)
        return None if pair is None else [pair]

    @classmethod
    def get_column_pair(cls, left: Term, right: Term, declared_names: set[str]) -> tuple[Term, Term] | None:
        """The inner and the outer column of two sides of an equality.

        Args:
            left: One side.
            right: The other side.
            declared_names: The tables of the subquery.

        Returns:
            ``(inner column, outer column)``, None unless one side is a column of the subquery and the
            other one of the query around it.
        """
        for inner, outer in ((left, right), (right, left)):
            if (
                isinstance(inner, Field)
                and cls.get_table_name(inner.table) in declared_names
                and isinstance(outer, (Field, QualifiedOuterField))
                and cls.get_table_name(outer.table) not in declared_names
            ):
                return inner, outer
        return None

    @classmethod
    def split_conditions(
        cls, inner_query: QueryBuilder, declared_names: set[str]
    ) -> tuple[list[tuple[Term, Term]], list[Term], bool]:
        """The conditions of a subquery's ``WHERE`` by how they read the query around it.

        Args:
            inner_query: The subquery.
            declared_names: The names of the tables it reads.

        Returns:
            The pairs of its own and outer columns it equals, its conditions reading no outer column,
            and whether a condition reads one otherwise.
        """
        pairs: list[tuple[Term, Term]] = []
        kept_conditions: list[Term] = []
        correlated_otherwise = False
        conditions = cls.iterate_conjuncts(inner_query._wheres) if inner_query._wheres is not None else ()
        for condition in conditions:
            if isinstance(condition, EmptyCriterion):
                continue
            if not cls.reads_other_tables(condition, declared_names):
                kept_conditions.append(condition)
            elif (condition_pairs := cls.get_correlation_pairs(condition, declared_names)) is not None:
                pairs.extend(condition_pairs)
            else:
                correlated_otherwise = True
        return pairs, kept_conditions, correlated_otherwise

    @classmethod
    def get_exists_sql(cls, inner_query: QueryBuilder, sql_context: SqlContext) -> str:
        """``EXISTS (<inner_query>)`` without correlation: the outer columns the query is correlated
        with tested for membership in the rows of its inner columns, NULLs ruled out on both sides
        so the test is true or false exactly where the ``EXISTS`` is. A subquery correlated in its
        ``WHERE`` by more than equalities (``version__gt=OuterReference("version")``) is the
        database's own ``EXISTS`` where correlated subqueries run - no membership test says it.

        Args:
            inner_query: The subquery.
            sql_context: The context the outer query renders in.

        Returns:
            The condition.

        Raises:
            UnSupportedError: The subquery is correlated outside its ``WHERE``, or other than by
                equalities of columns on a database running no correlated subqueries, or by
                equalities of rows it limits, groups or aggregates - an aggregate gives a row even for
                no rows.
        """
        declared_names = cls.get_declared_table_names(inner_query)
        pairs, kept_conditions, correlated_otherwise = cls.split_conditions(inner_query, declared_names)
        terms_outside_where = [
            term for term in cls.iterate_query_terms(inner_query) if term is not inner_query._wheres
        ]
        if any(cls.reads_other_tables(term, declared_names) for term in terms_outside_where):
            raise cls.get_error(sql_context)
        limit = inner_query._limit
        if correlated_otherwise:
            if not sql_context.dialect.features.supports_correlated_subqueries:
                raise cls.get_error(sql_context)
            # A limit of one row or more doesn't change whether a row exists - left out, as the
            # database limits no correlated subquery.
            if limit is not None and isinstance(limit.value, int) and limit.value >= 1:
                inner_query = copy(inner_query)
                inner_query._limit = None
            return f"EXISTS {inner_query.get_sql(sql_context)}"
        if not pairs:
            return f"EXISTS {inner_query.get_sql(sql_context)}"
        # A limit of one row or more doesn't change whether a row exists - it is left out of the
        # rows the outer columns are looked up in, where it would cut the rows of every outer row.
        if (
            (limit is not None and not (isinstance(limit.value, int) and limit.value >= 1))
            or inner_query._offset is not None
            or inner_query._groupbys
            or not isinstance(inner_query._havings, (type(None), EmptyCriterion))
            or any(select.is_aggregate for select in inner_query._selects)
        ):
            raise cls.get_error(sql_context)
        inner_columns = [inner for inner, _outer in pairs]
        outer_columns = [outer for _inner, outer in pairs]
        membership_query = copy(inner_query)
        membership_query._selects = list(inner_columns)  # type: ignore[arg-type]
        membership_query._limit = None
        where: Term | None = None
        for condition in [*kept_conditions, *(column.notnull() for column in inner_columns)]:
            where = condition if where is None else ComplexCriterion(Boolean.AND, where, condition)
        membership_query._wheres = where
        outer_context = sql_context.copy(with_alias=False)
        if not sql_context.with_namespace:
            # The query around reads one table and names its columns alone - an UPDATE or DELETE
            # mutation knows its table by no name.
            outer_columns = [
                Field(column.column) if isinstance(column, QualifiedOuterField) else column for column in outer_columns
            ]
        outer_sql = (
            outer_columns[0].get_sql(outer_context)
            if len(outer_columns) == 1
            else Tuple(*outer_columns).get_sql(outer_context)
        )
        not_null_sql = " AND ".join(f"{column.get_sql(outer_context)} IS NOT NULL" for column in outer_columns)
        prefix = sql_context.dialect.clauses.get_distributed_read_prefix_sql(sql_context)
        return f"({not_null_sql} AND {outer_sql} {prefix}IN {membership_query.get_sql(sql_context)})"
