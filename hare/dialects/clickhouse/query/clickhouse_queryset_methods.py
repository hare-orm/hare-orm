from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.enums import ClickhouseClause
from hare.dialects.clickhouse.query.clickhouse_totals import ClickhouseTotals
from hare.dialects.clickhouse.query.constants import (
    CLICKHOUSE_FINAL_ENGINES,
    CLICKHOUSE_MAX_ROW_COUNT,
    CLICKHOUSE_MAX_SETTING_INTEGER,
    CLICKHOUSE_MIN_SAMPLE_ROWS,
    CLICKHOUSE_MIN_SETTING_INTEGER,
)
from hare.exceptions import QueryError
from hare.query.expressions import Expression
from hare.query.queryset.constants import MAX_TABLE_SAMPLE_PERCENT
from hare.query.queryset.extensions.query_set_extensions import QuerySetExtensions

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.query.expressions.conditions.q import Q
    from hare.query.queryset.extensions.query_set_extension_query import QuerySetExtensionQuery
    from hare.sql.builder.queries.query_builder import QueryBuilder


class ClickhouseQuerySetMethods:
    """ClickHouse's own QuerySet methods - ``final()``, ``sample_rows()``, ``sample_offset()``,
    ``prewhere()``, ``limit_by()``, ``settings()`` and ``with_totals()`` - registered when the dialect
    is installed. Each sets a clause of the query (``ClickhouseClause``) its ``QueryClauses`` writes.
    """

    @classmethod
    def register(cls, dialect_name: str) -> None:
        """Registers the methods for a dialect.

        Args:
            dialect_name: The dialect's name.
        """
        QuerySetExtensions.register("final", dialect_name, cls.final, reads_query=True)
        QuerySetExtensions.register("sample_rows", dialect_name, cls.sample_rows, reads_query=True)
        QuerySetExtensions.register("sample_offset", dialect_name, cls.sample_offset, reads_query=True)
        QuerySetExtensions.register("prewhere", dialect_name, cls.prewhere, reads_query=True, takes_condition=True)
        QuerySetExtensions.register("limit_by", dialect_name, cls.limit_by, reads_query=True, changes_rows=True)
        QuerySetExtensions.register("settings", dialect_name, cls.settings)
        QuerySetExtensions.register(
            "with_totals", dialect_name, cls.with_totals, reads_query=True, read_result=ClickhouseTotals.read
        )

    @staticmethod
    def raise_if_write(extension_query: QuerySetExtensionQuery, method_name: str) -> None:
        """Rejects a method of the rows read on an ``UPDATE`` or ``DELETE``.

        Args:
            extension_query: The query.
            method_name: The method.

        Raises:
            QueryError: The query is a write.
        """
        if extension_query.is_write:
            raise QueryError(
                f"{method_name}() changes how rows are read - an UPDATE or DELETE mutation of ClickHouse matches "
                "its rows by its condition alone"
            )

    @staticmethod
    def get_table_options(extension_query: QuerySetExtensionQuery) -> ClickhouseTableOptions:
        """The ClickHouse table options of the query's model.

        Args:
            extension_query: The query.

        Returns:
            The options - the dialect's defaults when the model declares none.
        """
        table_options = extension_query.model._meta.get_table_options(extension_query.dialect)
        return table_options if isinstance(table_options, ClickhouseTableOptions) else ClickhouseTableOptions()

    @staticmethod
    def final(
        builder: QueryBuilder, extension_query: QuerySetExtensionQuery, all_tables: bool = False
    ) -> QueryBuilder:
        """``QuerySet.final()`` - reads the model's table with ``FINAL``: the versions of a row its
        engine keeps (``ReplacingMergeTree``, ``CollapsingMergeTree``, ...) merged as a merge of the
        parts would. ``final(all_tables=True)`` reads every table of the query so - the query's
        ``SETTINGS final = 1``.

        Args:
            builder: The query's builder.
            extension_query: The query.
            all_tables: Whether every table of the query is read with ``FINAL``, the joined ones too.

        Returns:
            The builder.

        Raises:
            QueryError: ``all_tables`` isn't a bool; the query is a write; the model's table engine
                keeps one version of a row.
        """
        if not isinstance(all_tables, bool):
            raise QueryError(f"final(all_tables=...) takes a bool, got {all_tables!r}")
        ClickhouseQuerySetMethods.raise_if_write(extension_query, "final")
        if all_tables:
            return ClickhouseQuerySetMethods.add_settings(builder, {"final": 1})
        table_options = ClickhouseQuerySetMethods.get_table_options(extension_query)
        engine_name = table_options.get_engine_name()
        if not table_options.keeps_row_versions():
            raise QueryError(
                f"{extension_query.model.__name__}.objects.final() reads a table whose engine keeps several versions "
                f"of a row ({', '.join(sorted(CLICKHOUSE_FINAL_ENGINES))}) - its engine is {engine_name}"
            )
        return builder.set_dialect_clause(ClickhouseClause.FINAL, True)

    @staticmethod
    def sample_rows(builder: QueryBuilder, extension_query: QuerySetExtensionQuery, rows: int) -> QueryBuilder:
        """``QuerySet.sample_rows(rows)`` - reads a sample of about ``rows`` rows of the model's
        table: ``SAMPLE <rows>``, picked by its ``ClickhouseTableOptions.sample_by`` key.

        Args:
            builder: The query's builder.
            extension_query: The query.
            rows: The rows, at least ``CLICKHOUSE_MIN_SAMPLE_ROWS``.

        Returns:
            The builder.

        Raises:
            QueryError: ``rows`` isn't an int in range; the query is a write or reads a ``sample()``
                too; the table declares no ``sample_by``.
        """
        if (
            isinstance(rows, bool)
            or not isinstance(rows, int)
            or not CLICKHOUSE_MIN_SAMPLE_ROWS <= rows <= CLICKHOUSE_MAX_ROW_COUNT
        ):
            raise QueryError(
                f"sample_rows() takes an int from {CLICKHOUSE_MIN_SAMPLE_ROWS} to {CLICKHOUSE_MAX_ROW_COUNT}, got "
                f"{rows!r}"
            )
        ClickhouseQuerySetMethods.raise_if_write(extension_query, "sample_rows")
        if builder._table_sample is not None:
            raise QueryError("sample_rows() reads a sample of a number of rows - the queryset reads a sample() too")
        ClickhouseQuerySetMethods.get_table_options(extension_query).raise_if_unsampled(extension_query.model)
        return builder.set_dialect_clause(ClickhouseClause.SAMPLE_ROWS, rows)

    @staticmethod
    def sample_offset(builder: QueryBuilder, extension_query: QuerySetExtensionQuery, percent: float) -> QueryBuilder:
        """``QuerySet.sample_offset(percent)`` - the sample of ``sample()``/``sample_rows()`` is read
        from the part of the sample key's range after ``percent`` of it (``SAMPLE ... OFFSET``):
        ``sample(50)`` and ``sample(50).sample_offset(50)`` read disjoint halves.

        Args:
            builder: The query's builder.
            extension_query: The query.
            percent: The part of the range skipped, in percent - an int or float from 0 below 100.

        Returns:
            The builder.

        Raises:
            QueryError: ``percent`` isn't a number in range; the query is a write.
        """
        if (
            isinstance(percent, bool)
            or not isinstance(percent, int | float)
            or not math.isfinite(percent)
            or not 0 <= percent < MAX_TABLE_SAMPLE_PERCENT
        ):
            raise QueryError(
                f"sample_offset() takes a percent from 0 below {MAX_TABLE_SAMPLE_PERCENT}, got {percent!r}"
            )
        ClickhouseQuerySetMethods.raise_if_write(extension_query, "sample_offset")
        return builder.set_dialect_clause(ClickhouseClause.SAMPLE_OFFSET, percent)

    @staticmethod
    def prewhere(builder: QueryBuilder, extension_query: QuerySetExtensionQuery, condition: Q) -> QueryBuilder:
        """``QuerySet.prewhere(*conditions, **filters)`` - a condition ClickHouse reads before the
        other columns (``PREWHERE``): the rows it drops have no other column read. Its arguments are
        ``filter()``'s, over the columns of the model's own table.

        Args:
            builder: The query's builder.
            extension_query: The query.
            condition: The condition.

        Returns:
            The builder - the condition joined with ``AND`` to an earlier ``prewhere()``.

        Raises:
            QueryError: The query is a write; the condition reads a related model or an aggregate.
        """
        ClickhouseQuerySetMethods.raise_if_write(extension_query, "prewhere")
        modifier = extension_query.get_condition(condition)
        if modifier.joins or modifier.having_criterion:
            raise QueryError(
                "prewhere() reads the columns of the model's own table - a related model's fields and aggregates "
                "are read by filter()"
            )
        criterion = modifier.where_criterion
        if not criterion:
            return builder
        earlier_criterion = builder._dialect_clauses.get(ClickhouseClause.PREWHERE)
        if earlier_criterion is not None:
            criterion = earlier_criterion & criterion
        return builder.set_dialect_clause(ClickhouseClause.PREWHERE, criterion)

    @staticmethod
    def limit_by(
        builder: QueryBuilder,
        extension_query: QuerySetExtensionQuery,
        limit: int,
        *expressions: str | Expression,
        offset: int = 0,
    ) -> QueryBuilder:
        """``QuerySet.limit_by(limit, *expressions, offset=0)`` - at most ``limit`` rows of each group
        of the expressions' values, after skipping ``offset`` of them (``LIMIT ... BY``), in the
        queryset's order. ``count()`` and ``exists()`` count the rows kept.

        Args:
            builder: The query's builder.
            extension_query: The query.
            limit: The rows kept per group.
            expressions: Field names, annotation names or expressions the rows are grouped by.
            offset: The rows of each group skipped first.

        Returns:
            The builder.

        Raises:
            QueryError: An argument is of another type or out of range; the query is a write or an
                ``aggregate()``.
        """
        for name, value in (("limit", limit), ("offset", offset)):
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= CLICKHOUSE_MAX_ROW_COUNT:
                raise QueryError(
                    f"limit_by() takes {name} as an int from 0 to {CLICKHOUSE_MAX_ROW_COUNT}, got {value!r}"
                )
        if not expressions or not all(isinstance(expression, str | Expression) for expression in expressions):
            raise QueryError(
                f"limit_by() groups the rows by field names or expressions - at least one, got {expressions!r}"
            )
        ClickhouseQuerySetMethods.raise_if_write(extension_query, "limit_by")
        if extension_query.is_summary:
            raise QueryError(
                "limit_by() limits the rows a query returns - aggregate() computes over the rows of the table; "
                "read the rows and compute over them, or count() them"
            )
        terms = []
        for expression in expressions:
            result = extension_query.get_expression(expression)
            builder = extension_query.join(builder, result.joins)
            terms.append(result.term)
        return builder.set_dialect_clause(ClickhouseClause.LIMIT_BY, (limit, offset, tuple(terms)))

    @staticmethod
    def settings(builder: QueryBuilder, **values: bool | int | float | str) -> QueryBuilder:
        """``QuerySet.settings(**values)`` - the statement's ``SETTINGS``: ClickHouse's settings for
        this query alone (``.settings(max_threads=4, distributed_product_mode="global")``), over
        those of an earlier call.

        Args:
            builder: The query's builder.
            values: The settings by name - a bool, an int, a float or a string each.

        Returns:
            The builder.

        Raises:
            QueryError: A name isn't an identifier, or a value is of another type or out of range.
        """
        if not values:
            raise QueryError("settings() takes at least one setting")
        for name, value in values.items():
            if not name.isascii() or not name.isidentifier():
                raise QueryError(f"settings() takes setting names that are identifiers, got {name!r}")
            if isinstance(value, (bool, str)):
                continue
            if isinstance(value, int):
                if not CLICKHOUSE_MIN_SETTING_INTEGER <= value <= CLICKHOUSE_MAX_SETTING_INTEGER:
                    raise QueryError(
                        f"settings({name}=...) takes an int from {CLICKHOUSE_MIN_SETTING_INTEGER} to "
                        f"{CLICKHOUSE_MAX_SETTING_INTEGER}, got {value!r}"
                    )
                continue
            if isinstance(value, float) and math.isfinite(value):
                continue
            raise QueryError(f"settings({name}=...) takes a bool, an int, a finite float or a string, got {value!r}")
        return ClickhouseQuerySetMethods.add_settings(builder, values)

    @staticmethod
    def add_settings(builder: QueryBuilder, values: dict[str, Any]) -> QueryBuilder:
        """Adds settings to the statement's ``SETTINGS``.

        Args:
            builder: The query's builder.
            values: The settings by name.

        Returns:
            The builder.
        """
        settings = {**builder._dialect_clauses.get(ClickhouseClause.SETTINGS, {}), **values}
        return builder.set_dialect_clause(ClickhouseClause.SETTINGS, settings)

    @staticmethod
    def with_totals(builder: QueryBuilder, extension_query: QuerySetExtensionQuery) -> QueryBuilder:
        """``QuerySet.with_totals()`` - a grouped ``values()`` query's rows come with the row of its
        aggregates over every group (``GROUP BY ... WITH TOTALS``): the query returns a
        ``TotalsResult``, its rows with ``.totals``, the aggregates over all the rows its conditions
        match - of the groups ``HAVING`` keeps - before ``LIMIT``; its grouped fields are None there.

        Args:
            builder: The query's builder.
            extension_query: The query.

        Returns:
            The builder.

        Raises:
            QueryError: The query isn't a ``values()`` query grouping its rows.
        """
        # Local import: the values query imports the queryset package, which loads this dialect.
        from hare.query.statements.select.values_query import ValuesQuery

        if not isinstance(extension_query.query, ValuesQuery) or not builder._groupbys:
            raise QueryError(
                "with_totals() adds the totals of a grouped values() query - "
                "values(<fields>).annotate(<aggregates>).with_totals()"
            )
        return builder
