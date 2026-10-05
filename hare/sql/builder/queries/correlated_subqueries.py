from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import UnSupportedError
from hare.sql.builder.joins.join_on import JoinOn
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.tables.selectable import Selectable
from hare.sql.builder.tables.table import Table
from hare.sql.constants import CORRELATED_CONDITION_PREFIX, CORRELATED_SORT_KEY_PREFIX
from hare.sql.enums import Boolean, Equality
from hare.sql.terms.criteria.basic_criterion import BasicCriterion
from hare.sql.terms.criteria.complex_criterion import ComplexCriterion
from hare.sql.terms.criteria.empty_criterion import EmptyCriterion
from hare.sql.terms.field import Field
from hare.sql.terms.qualified_outer_field import QualifiedOuterField
from hare.sql.terms.tuple import Tuple

if TYPE_CHECKING:  # pragma: nocoverage
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
        """Every term of one query - selected, filtered, grouped, ordered and joined on - without
        the terms of the queries nested in them.

        Args:
            query: The query.

        Yields:
            The terms.
        """
        yield from query._selects
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
                node.inner_query for node in nodes if getattr(type(node), "rewrites_own_correlation", False)
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
            elif getattr(type(node), "rewrites_own_correlation", False):
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
        own_names = declared_names | cls.get_declared_table_names(query)
        return any(cls.reads_other_tables(term, own_names) for term in cls.iterate_query_terms(query))

    @classmethod
    def check_statement(cls, query: QueryBuilder, sql_context: SqlContext) -> None:
        """Refuses a statement holding a correlated subquery the database can't run - an ``EXISTS``
        writes its own correlation away, or refuses it itself; where correlated subqueries run, one
        ordering or limiting its own rows the database runs none of.

        Args:
            query: The statement.
            sql_context: The context it renders in.

        Raises:
            UnSupportedError: A subquery reads the columns of the query around it.
        """
        features = sql_context.dialect.features
        runs_correlation = features.supports_correlated_subqueries
        writes_rows = query._update_table is not None or bool(query._delete_from)
        for nested_query, rewrites in cls.iterate_nested_queries(query):
            if not rewrites and cls.query_reads_other_tables(nested_query, set()):
                if not runs_correlation or (writes_rows and not features.orders_by_correlated_subqueries):
                    raise cls.get_error(sql_context)
                if nested_query._orderbys or nested_query._limit is not None or nested_query._offset is not None:
                    raise UnSupportedError(
                        f"The {sql_context.dialect.name} database runs no correlated subquery ordering or "
                        "limiting its own rows - a subquery reading the columns of the query around it takes no "
                        "order_by() or slice there; an aggregate (Max, Min) picks one row instead"
                    )
            cls.check_statement(nested_query, sql_context)

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
        return query._wheres is not None and cls.holds_correlation(query._selects, query)

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
                f"{quoted_name} {direction}" if direction is not None else quoted_name
                for quoted_name, (_term, direction) in zip(quoted_sort_keys, query._orderbys, strict=True)
            )
        sql += query._limit_offset_sql(statement_context)
        if sql_context.subquery:
            sql = f"({sql})"
        if sql_context.with_alias and query.alias:
            return sql_context.format_alias_sql(sql, query.alias)
        return sql

    @staticmethod
    def get_error(sql_context: SqlContext) -> UnSupportedError:
        """The refusal of a correlated subquery.

        Args:
            sql_context: The context the statement renders in.

        Returns:
            The error.
        """
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
    def get_correlation_pair(cls, criterion: Term, declared_names: set[str]) -> tuple[Term, Term] | None:
        """The inner and the outer column of an equality correlating a subquery.

        Args:
            criterion: A condition of the subquery reading the query around it.
            declared_names: The tables of the subquery.

        Returns:
            ``(inner column, outer column)``, None for any other condition.
        """
        if type(criterion) is not BasicCriterion or criterion.comparator != Equality.EQ:
            return None
        sides = (criterion.left, criterion.right)
        for inner, outer in (sides, sides[::-1]):
            if (
                isinstance(inner, Field)
                and cls.get_table_name(inner.table) in declared_names
                and isinstance(outer, (Field, QualifiedOuterField))
                and cls.get_table_name(outer.table) not in declared_names
            ):
                return inner, outer
        return None

    @classmethod
    def get_exists_sql(cls, inner_query: QueryBuilder, sql_context: SqlContext) -> str:
        """``EXISTS (<inner_query>)`` without correlation: the outer columns the query is correlated
        with tested for membership in the rows of its inner columns, NULLs ruled out on both sides
        so the test is true or false exactly where the ``EXISTS`` is.

        Args:
            inner_query: The subquery.
            sql_context: The context the outer query renders in.

        Returns:
            The condition.

        Raises:
            UnSupportedError: The subquery is correlated other than by equalities of columns in
                its ``WHERE``, or it limits, groups or aggregates its rows - an aggregate gives a row even
                for no rows.
        """
        declared_names = cls.get_declared_table_names(inner_query)
        pairs: list[tuple[Term, Term]] = []
        kept_conditions: list[Term] = []
        conditions = cls.iterate_conjuncts(inner_query._wheres) if inner_query._wheres is not None else ()
        for condition in conditions:
            if isinstance(condition, EmptyCriterion):
                continue
            if not cls.reads_other_tables(condition, declared_names):
                kept_conditions.append(condition)
            elif (pair := cls.get_correlation_pair(condition, declared_names)) is not None:
                pairs.append(pair)
            else:
                raise cls.get_error(sql_context)
        terms_outside_where = [
            term for term in cls.iterate_query_terms(inner_query) if term is not inner_query._wheres
        ]
        if any(cls.reads_other_tables(term, declared_names) for term in terms_outside_where):
            raise cls.get_error(sql_context)
        if not pairs:
            return f"EXISTS {inner_query.get_sql(sql_context)}"
        # A limit of one row or more doesn't change whether a row exists - it is left out of the
        # rows the outer columns are looked up in, where it would cut the rows of every outer row.
        limit = inner_query._limit
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
        return f"({not_null_sql} AND {outer_sql} IN {membership_query.get_sql(sql_context)})"
