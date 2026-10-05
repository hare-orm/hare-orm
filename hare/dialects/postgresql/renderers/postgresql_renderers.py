from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.dialects.base.renderers.checked_term_renderers import CheckedTermRenderers
from hare.dialects.postgresql.renderers.postgresql_defaults import PostgresqlDefaults
from hare.dialects.postgresql.renderers.postgresql_json_renderers import PostgresqlJsonRenderers
from hare.dialects.postgresql.renderers.postgresql_number_renderers import PostgresqlNumberRenderers
from hare.dialects.postgresql.renderers.postgresql_temporal_renderers import PostgresqlTemporalRenderers
from hare.dialects.postgresql.renderers.postgresql_text_renderers import PostgresqlTextRenderers
from hare.sql.sql_context import SqlContext
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql.terms.term import Term


class PostgresqlRenderers(CheckedTermRenderers):
    """How PostgreSQL renders the terms whose SQL differs between dialects."""

    def add_own_renderers(self) -> None:
        PostgresqlJsonRenderers.register(self)
        PostgresqlTemporalRenderers.register(self)
        PostgresqlNumberRenderers.register(self)
        PostgresqlTextRenderers.register(self)
        PostgresqlDefaults.register(self)
        # Local import: the container renderers read the PostgreSQL array functions, which import the
        # query package, which imports this one.
        from hare.dialects.postgresql.renderers.postgresql_container_renderers import PostgresqlContainerRenderers

        PostgresqlContainerRenderers.register(self)
        # Local import: the function module imports the query package, which imports this one.
        from hare.dialects.postgresql.functions.array.array_subquery import ArraySubquery

        self.register(ArraySubquery, self.render_array_subquery)
        # Local import: the spatial terms import the SQL functions package, which imports this one.
        from hare.dialects.postgresql.spatial.postgresql_spatial_renderers import PostgresqlSpatialRenderers

        PostgresqlSpatialRenderers.register(self)
        # Local import: the vector terms import the SQL functions package, which imports this one.
        from hare.dialects.postgresql.vectors.postgresql_vector_renderers import PostgresqlVectorRenderers

        PostgresqlVectorRenderers.register(self)

    @staticmethod
    def render_array_subquery(subquery: Any, sql_context: SqlContext) -> str:
        """``ARRAY(SELECT ...)``."""
        return f"ARRAY({subquery.get_subquery_sql(sql_context)})"

    def get_never_null_column_count_argument(self, term: Term) -> Term:
        # Local import: hare.sql renders through the dialect. COUNT(*) reads no column.
        from hare.sql.terms.star import Star

        return Star()

    def get_integer_aggregate_as_float(self, term: Term) -> Term:
        # The average or a statistic of integers is a NUMERIC here.
        # Local import: hare.sql renders through the dialect.
        from hare.sql.functions.cast import Cast

        return Cast(term, "FLOAT")

    def get_concatenated_argument_sql(self, argument_sql: str, argument: Any) -> str:
        # Every CONCAT() argument needs a type: a number literal takes its own, everything else is
        # text.
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.constants import POSTGRESQL_NUMBER_LITERAL_TYPES
        from hare.sql.terms.field import Field as SqlField
        from hare.sql.terms.functions.function import Function

        if isinstance(argument, ValueWrapper):
            value = argument.value
            while isinstance(value, Enum):
                value = value.value
            number_type = POSTGRESQL_NUMBER_LITERAL_TYPES.get(type(value))
            if number_type is not None:
                return f"{argument_sql}::{number_type}"
        if not isinstance(argument, (ValueWrapper, SqlField, Function)):
            # `a*$1::text` would cast only the last operand.
            return f"({argument_sql})::text"
        return f"{argument_sql}::text"
