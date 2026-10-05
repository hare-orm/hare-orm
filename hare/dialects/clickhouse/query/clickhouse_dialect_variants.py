from __future__ import annotations

from typing import ClassVar

from hare.classes.declared_subclass import DeclaredSubclass
from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect
from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT, CLICKHOUSE_NATIVE_JSON_DIALECT
from hare.dialects.clickhouse.query.declarations import ClickhouseNativeJsonQuery, ClickhouseQuery
from hare.sql.builder.queries.query import Query


class ClickhouseDialectVariants:
    """The dialect a ClickHouse connection renders its SQL in, with its query class, by what the server
    runs - JSON stored natively, correlated subqueries, projections rebuilt by a lightweight ``DELETE``: one
    of each set of them, made as it is met."""

    #: The dialect and the query class of each set of capabilities met - ``(stores JSON natively, runs
    #: correlated subqueries, rebuilds projections)``.
    VARIANTS: ClassVar[dict[tuple[bool, bool, bool], tuple[ClickhouseDialect, type[Query]]]] = {
        (False, False, False): (CLICKHOUSE_DIALECT, ClickhouseQuery),
        (True, False, False): (CLICKHOUSE_NATIVE_JSON_DIALECT, ClickhouseNativeJsonQuery),
    }

    @classmethod
    def get(
        cls, stores_json_natively: bool, runs_correlated_subqueries: bool, rebuilds_projections: bool
    ) -> tuple[ClickhouseDialect, type[Query]]:
        """The dialect of a server's capabilities, and the query class rendering in it.

        Args:
            stores_json_natively: Whether a ``JSONField`` is a ``JSON`` column.
            runs_correlated_subqueries: Whether a subquery reads the columns of the query around it.
            rebuilds_projections: Whether a table with projections takes a lightweight ``DELETE``.

        Returns:
            The dialect and the query class.
        """
        key = (stores_json_natively, runs_correlated_subqueries, rebuilds_projections)
        variant = cls.VARIANTS.get(key)
        if variant is None:
            dialect = ClickhouseDialect(
                stores_json_natively=stores_json_natively,
                runs_correlated_subqueries=runs_correlated_subqueries,
                rebuilds_projections=rebuilds_projections,
            )
            name_parts = [
                part
                for part, holds in (
                    ("NativeJson", stores_json_natively),
                    ("Correlated", runs_correlated_subqueries),
                    ("ProjectionRebuilding", rebuilds_projections),
                )
                if holds
            ]
            query_class = DeclaredSubclass.make(
                Query,
                f"Clickhouse{''.join(name_parts)}Query",
                __package__,
                """A query rendered in ClickHouse's SQL for a server of the capabilities its name says.""",
                SQL_CONTEXT=dialect.sql_context,
            )
            variant = cls.VARIANTS[key] = (dialect, query_class)
        return variant
