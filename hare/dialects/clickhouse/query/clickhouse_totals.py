from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, cast

from hare.dialects.clickhouse.enums import ClickhouseClause
from hare.dialects.clickhouse.query.totals_result import TotalsResult
from hare.sql.terms.select_reference import SelectReference
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.null_value import NullValue

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
    from hare.query.statements.select.values_query import ValuesQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.terms.field import Field
    from hare.sql.terms.functions.function import Function
    from hare.sql.terms.term import Term


class ClickhouseTotals:
    """The totals row of a ``with_totals()`` query - read by a statement of its own, as neither driver
    returns the row ``GROUP BY ... WITH TOTALS`` adds: the query's aggregates over all the rows it
    reads, of the groups its ``HAVING`` keeps, before ``ORDER BY``, ``LIMIT BY`` and ``LIMIT``."""

    @staticmethod
    async def read(extension_query: QuerySetExtensionQuery, rows: Any) -> TotalsResult:
        """Reads the totals row of a query that has run.

        Args:
            extension_query: The query.
            rows: Its rows.

        Returns:
            The rows with the totals.
        """
        query = cast("ValuesQuery", extension_query.query)
        builder = extension_query.get_built_builder()
        values_reading = query.get_values_reading()
        totals_rows = await query._fetch_rows(
            query._connection,
            *ClickhouseTotals.get_totals_builder(builder).get_parameterized_sql(),
            column_converters=values_reading.column_converters,
            value_fields=values_reading.value_fields,
        )
        return TotalsResult(list(rows), totals_rows[0] if totals_rows else None)

    @staticmethod
    def get_totals_builder(builder: QueryBuilder) -> QueryBuilder:
        """The statement of the totals row: the query's selected aggregates over its own rows,
        ungrouped, unordered and unlimited - its ``HAVING`` turned into a condition on the keys of the
        groups it keeps - selected again with its other columns NULL. The NULL columns are selected
        outside: an alias of the SELECT list hides a column of its name from ``WHERE`` in ClickHouse.

        Args:
            builder: The query, built.

        Returns:
            The statement.
        """
        key_terms = ClickhouseTotals.get_group_key_terms(builder)
        is_total = [select_term.contains_aggregate and not select_term.is_analytic for select_term in builder._selects]
        aggregates_builder = copy(builder)
        aggregates_builder._selects = [
            select_term for select_term, total in zip(builder._selects, is_total, strict=True) if total
        ]
        aggregates_builder._groupbys = []
        aggregates_builder._havings = None
        aggregates_builder._orderbys = []
        aggregates_builder._limit = None
        aggregates_builder._offset = None
        dialect_clauses = {
            name: value for name, value in builder._dialect_clauses.items() if name != ClickhouseClause.LIMIT_BY
        }
        aggregates_builder._dialect_clauses = dialect_clauses
        aggregates_builder._shared_containers = aggregates_builder._shared_containers - {
            "_selects",
            "_groupbys",
            "_orderbys",
            "_dialect_clauses",
        }
        if builder._havings:
            kept_groups_builder = copy(builder)
            kept_groups_builder._selects = cast("list[Field | Function]", list(key_terms))
            kept_groups_builder._orderbys = []
            kept_groups_builder._limit = None
            kept_groups_builder._offset = None
            kept_groups_builder._dialect_clauses = dict(dialect_clauses)
            kept_groups_builder._shared_containers = kept_groups_builder._shared_containers - {
                "_selects",
                "_orderbys",
                "_dialect_clauses",
            }
            aggregates_builder = aggregates_builder.where(Tuple(*key_terms).isin(kept_groups_builder))
        totals_builder = builder.query_class.from_(aggregates_builder)
        return totals_builder.select(
            *(
                totals_builder._from[0].field(cast("str", select_term.alias)).as_(cast("str", select_term.alias))
                if total
                else NullValue(select_term.alias)
                for select_term, total in zip(builder._selects, is_total, strict=True)
            )
        )

    @staticmethod
    def get_group_key_terms(builder: QueryBuilder) -> list[Term]:
        """The terms the query groups its rows by - a reference to a selected term, or a selected
        term's alias, is that term.

        Args:
            builder: The query.

        Returns:
            The terms.
        """
        selected_by_alias = {select_term.alias: select_term for select_term in builder._selects if select_term.alias}
        key_terms = []
        for group_term in builder._groupbys:
            if isinstance(group_term, SelectReference):
                key_terms.append(group_term.term)
            elif group_term.alias and group_term.alias in selected_by_alias:
                key_terms.append(selected_by_alias[group_term.alias])
            else:
                key_terms.append(group_term)
        return key_terms
