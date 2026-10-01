from __future__ import annotations

import datetime
from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.dialects.base.dialect import Dialect
from hare.dialects.constants import (
    POSTGRES_IN_ARRAY_THRESHOLD,
    POSTGRESQL_MINIMUM_SERVER_VERSION,
    POSTGRESQL_NULLS_DISTINCT_SERVER_VERSION,
    POSTGRESQL_PARTITIONED_EXCLUSION_SERVER_VERSION,
)
from hare.dialects.enums import DialectName, ParameterPosition
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.operators import FilterOperators
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.dialects.base.schema.editor import BaseSchemaEditor
    from hare.dialects.base.two_phase_commit import TwoPhaseCommit
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.base.field import Field
    from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector
    from hare.models import Model
    from hare.sql.enums import JsonValueType
    from hare.sql.terms.base.term import Term


class PostgresqlDialect(Dialect):
    """PostgreSQL."""

    name = DialectName.POSTGRESQL
    otel_system_name = "postgresql"
    matches_ordering_to_grouping_by_sql = True
    supports_distinct_on = True
    placeholder_template = "${}"
    single_parameter_in_list_min_length = POSTGRES_IN_ARRAY_THRESHOLD
    alias_quote_char = '"'
    supports_conflict_constraint_names = True
    supports_conflict_where = True
    supports_copy = True
    supports_virtual_generated_columns = False
    checks_foreign_keys_per_cascade_step = True
    checks_restrict_at_statement_end = True
    # NAMEDATALEN - 1.
    max_identifier_length = 63
    supports_partial_indexes = True
    supports_exclusion_constraints = True
    supports_deferrable_constraints = True
    supports_concurrent_indexes = True
    supports_not_valid_constraints = True
    supports_extensions = True
    supports_collations = True
    truncates_values_on_type_change = True
    binds_array_parameters = True
    minimum_server_version = POSTGRESQL_MINIMUM_SERVER_VERSION

    def get_server_version_features(self, server_version: tuple[int, ...]) -> dict[str, Any]:
        return {
            "supports_nulls_distinct": server_version >= POSTGRESQL_NULLS_DISTINCT_SERVER_VERSION,
            "supports_partitioned_exclusion_constraints": (
                server_version >= POSTGRESQL_PARTITIONED_EXCLUSION_SERVER_VERSION
            ),
        }

    def build_schema_editor_class(self) -> type[BaseSchemaEditor]:
        # Local import: the schema editor works on hare's models, whose modules import this one.
        from hare.dialects.postgresql.schema.editor import PostgresqlSchemaEditor

        return PostgresqlSchemaEditor

    def build_table_options_class(self) -> type[TableOptions] | None:
        # Local import: the table options module imports the constants module, which instantiates this class.
        from hare.dialects.postgresql.table_options import PostgresqlTableOptions

        return PostgresqlTableOptions

    def build_introspector_class(self) -> type[SchemaIntrospector] | None:
        # Local import: the introspector reads into hare's models and fields, whose modules import this one.
        from hare.dialects.postgresql.introspection import PostgresqlIntrospector

        return PostgresqlIntrospector

    def build_types(self) -> TypeRegistry:
        # Local import: the registry names hare's field classes, whose modules import this one.
        from hare.dialects.postgresql.types import PostgresqlTypes

        return PostgresqlTypes.build()

    def install(self) -> None:
        # Local import: the transform names hare's fields, whose modules import this one.
        from hare.dialects.postgresql.lookups.trigram import PostgresqlTrigramLookups
        from hare.dialects.postgresql.lookups.unaccent import UnaccentTransform

        UnaccentTransform.register()
        PostgresqlTrigramLookups.register()

    def build_filter_operators(self) -> FilterOperators:
        # Local import: the operators are hare's lookups, whose modules import this one.
        from hare.dialects.postgresql.operators import PostgresqlFilterOperators

        return PostgresqlFilterOperators.build()

    def build_renderers(self) -> TermRenderers:
        # Local import: the renderers name hare's terms, whose modules import this one.
        from hare.dialects.postgresql.renderers import PostgresqlRenderers

        return PostgresqlRenderers.build()

    def build_two_phase_commit(self) -> TwoPhaseCommit | None:
        # Local import: the statements read the constants module, which instantiates this class.
        from hare.dialects.postgresql.two_phase_commit import PostgresqlTwoPhaseCommit

        return PostgresqlTwoPhaseCommit()

    def get_default_rows_source_sql(self, row_count: int) -> str | None:
        return f"SELECT FROM generate_series(1, {int(row_count)})"  # nosec B608 - an int, not input

    def get_bytes_literal_sql(self, value: bytes) -> str:
        return f"'\\x{value.hex()}'::bytea"

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        return f"ARRAY[{','.join(element_sqls)}]" if element_sqls else "'{}'"

    def get_integer_aggregate_as_float(self, term: Term) -> Term:
        # The average or a statistic of integers is a NUMERIC here.
        # Local import: hare.sql renders through the dialect.
        from hare.sql.functions.cast import Cast

        return Cast(term, "FLOAT")

    def get_parameter_cast_type(self, value: Any, position: ParameterPosition) -> str | None:
        # PostgreSQL types a parameter only from what's around it: a bare one is text, which the
        # driver then refuses to bind a real value against, or has no type at all.
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import (
            POSTGRESQL_PARAMETER_TYPES_BY_POSITION,
            POSTGRESQL_SELECTED_LITERAL_POSITIONS,
            POSTGRESQL_SELECTED_LITERAL_TYPES,
        )

        if position in POSTGRESQL_SELECTED_LITERAL_POSITIONS:
            for value_type, type_name in POSTGRESQL_SELECTED_LITERAL_TYPES:
                if isinstance(value, value_type):
                    return type_name
        return POSTGRESQL_PARAMETER_TYPES_BY_POSITION[position].get(type(value))

    def get_json_object_value_cast_type(self, value: Any, value_type: JsonValueType) -> str | None:
        # Local imports: the constants module instantiates this class; hare.sql renders through
        # the dialect.
        from hare.dialects.postgresql.constants import POSTGRESQL_JSON_OBJECT_VALUE_TYPES
        from hare.sql.enums import JsonValueType

        if value is None or value_type in (JsonValueType.JSON, JsonValueType.BINARY):
            return POSTGRESQL_JSON_OBJECT_VALUE_TYPES[value_type]
        return self.get_parameter_cast_type(value, ParameterPosition.FUNCTION_ARGUMENT)

    def get_field_parameter_cast_type(self, field: Field[Any]) -> str | None:
        return field.get_column_type(self)

    def get_concatenated_argument_sql(self, argument_sql: str, argument: Any) -> str:
        # Every CONCAT() argument needs a type: a number literal takes its own, everything else is
        # text.
        # Imported here: the modules import each other.
        from hare.dialects.postgresql.constants import POSTGRESQL_NUMBER_LITERAL_TYPES
        from hare.sql.terms.base.value_wrapper import ValueWrapper
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

    def get_cast_parameter_sql(self, parameter_sql: str, value: Any) -> str:
        # PostgreSQL types a parameter only from what's around it - a CASE of bare parameters
        # would otherwise be text, which the driver then refuses to bind a real value against.
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import POSTGRESQL_PARAMETER_CASTS

        for value_type, type_name in POSTGRESQL_PARAMETER_CASTS:
            if isinstance(value, value_type):
                return f"{parameter_sql}::{type_name}"
        return parameter_sql

    def defer_cascade_foreign_keys(self, model: type[Model], db: DatabaseClient) -> AbstractAsyncContextManager[bool]:
        # Local import: the deferral walks hare's models, whose modules import this one.
        from hare.dialects.postgresql.deletion import PostgresqlForeignKeyDeferral

        return PostgresqlForeignKeyDeferral.defer(model, db)

    def get_string_literal_sql(self, text: str) -> str:
        """A string literal PostgreSQL reads the same whatever ``standard_conforming_strings`` is
        set to: ``'...'`` without a backslash, an escape string ``E'...'`` with one."""
        literal = super().get_string_literal_sql(text)
        if "\\" not in literal:
            return literal
        return "E" + literal.replace("\\", "\\\\")

    def get_literal_sql(self, value: Any) -> str:
        """A boolean is ``TRUE``/``FALSE``; a naive datetime is read in local system time and a
        naive time in UTC, as both drivers bind them; a list, tuple or set is an array literal."""
        if isinstance(value, bool):
            return "TRUE" if value else "FALSE"
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                value = Timezone.make_system_local_aware(value)
        elif isinstance(value, datetime.time) and value.tzinfo is None:
            value = value.replace(tzinfo=datetime.UTC)
        elif isinstance(value, list | tuple | set | frozenset):
            return "'{" + ",".join(self.get_array_element_literal_sql(item) for item in value) + "}'"
        return super().get_literal_sql(value)

    def get_array_element_literal_sql(self, value: Any) -> str:
        """One element of an array literal, which is itself inside a single-quoted literal.

        Args:
            value: The element.

        Returns:
            The element's text.
        """
        if isinstance(value, str):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("'", "''")
            return f'"{escaped}"'
        if isinstance(value, list | tuple | set | frozenset):
            return "{" + ",".join(self.get_array_element_literal_sql(item) for item in value) + "}"
        if isinstance(value, bool):
            return "true" if value else "false"
        if value is None:
            return "NULL"
        return str(value)

    def get_index_include_sql(self, quoted_columns: Sequence[str]) -> str:
        return f" INCLUDE ({', '.join(quoted_columns)})"

    def get_nulls_distinct_sql(self, nulls_distinct: bool) -> str:
        return " NULLS DISTINCT" if nulls_distinct else " NULLS NOT DISTINCT"

    def get_exclusion_constraint_extension(
        self, constraint: ExclusionConstraint, fields_by_name: Mapping[str, Field[Any]]
    ) -> str | None:
        """``btree_gist`` for a GiST constraint over a plain scalar column (a relation's key, a
        number, text, date, uuid, ...), which core GiST has no operator class for."""
        # Local import: the constants module instantiates this class.
        from hare.ddl.enums import ExclusionConstraintUsing
        from hare.dialects.postgresql.constants import BTREE_GIST_EXTENSION

        if constraint.using != ExclusionConstraintUsing.GIST:
            return None
        for expression, _operator in constraint.expressions:
            if isinstance(expression, str) and self.is_btree_gist_field(fields_by_name.get(expression)):
                return BTREE_GIST_EXTENSION
        return None

    def is_btree_gist_field(self, field: Field[Any] | None) -> bool:
        """Whether a field's column is a scalar type only ``btree_gist`` makes GiST-indexable.

        Args:
            field: The field, or None for an unknown name.

        Returns:
            True for a relation's key column or a column of a ``btree_gist`` type.
        """
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import BTREE_GIST_SQL_TYPES
        from hare.fields.relations.fields.relational_field import RelationalField

        if field is None:
            return False
        if isinstance(field, RelationalField):
            return True
        try:
            sql_type = field.get_column_type(self)
        except AttributeError, ConfigurationError:
            return False
        return isinstance(sql_type, str) and sql_type.lower().split("(")[0].strip() in BTREE_GIST_SQL_TYPES

    def supports_copy_column_type(self, column_type: str) -> bool:
        # The types both drivers' binary COPY encodes - not an array, a range or an extension's type.
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import COPY_SUPPORTED_SQL_TYPES

        return column_type.upper() in COPY_SUPPORTED_SQL_TYPES

    async def clear_tables(self, db: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import POSTGRESQL_CLEAR_TABLES_PROBE_SIZE

        # Only the tables holding rows are truncated - TRUNCATE costs the server per table, an empty
        # one as much as a filled one, and a test usually leaves rows in a few.
        filled_tables: list[str] = []
        for start in range(0, len(quoted_tables), POSTGRESQL_CLEAR_TABLES_PROBE_SIZE):
            probed_tables = quoted_tables[start : start + POSTGRESQL_CLEAR_TABLES_PROBE_SIZE]
            probe_sql = " UNION ALL ".join(
                f"SELECT {table_index} AS table_index WHERE EXISTS (SELECT 1 FROM {quoted_table})"  # nosec B608
                for table_index, quoted_table in enumerate(probed_tables)
            )
            filled_tables.extend(probed_tables[row["table_index"]] for row in await db.execute_dicts(probe_sql))
        # One TRUNCATE ... CASCADE empties them all at once, whatever references what.
        if filled_tables:
            await db.execute_script(f"TRUNCATE {', '.join(filled_tables)} CASCADE")

    def get_migration_lock_sql(self) -> str | None:
        # A transaction-level advisory lock - released with the transaction, or the connection if
        # the process dies.
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import POSTGRESQL_MIGRATION_LOCK_KEY

        return f"SELECT pg_advisory_xact_lock({POSTGRESQL_MIGRATION_LOCK_KEY})"

    def get_lock_table_sql(self, qualified_table: str) -> str | None:
        return f"LOCK TABLE {qualified_table} IN SHARE ROW EXCLUSIVE MODE"

    def get_explain_sql(self, sql: str, output_format: str | None, options: Mapping[str, bool]) -> str:
        """``EXPLAIN (options, FORMAT ...)`` - JSON and ``VERBOSE`` unless given."""
        # Local import: the constants module instantiates this class.
        from hare.dialects.postgresql.constants import POSTGRESQL_EXPLAIN_FORMATS, POSTGRESQL_EXPLAIN_OPTIONS

        output_format = (output_format or "JSON").upper()
        if output_format not in POSTGRESQL_EXPLAIN_FORMATS:
            raise UnSupportedError(f"Unsupported explain format: {output_format}")
        required_options = sorted(
            option.upper() for option, required in (options or {"verbose": True}).items() if required
        )
        if unsupported_options := set(required_options) - POSTGRESQL_EXPLAIN_OPTIONS:
            raise UnSupportedError(f"Unsupported options: {unsupported_options}")
        return f"EXPLAIN ({', '.join([*required_options, f'FORMAT {output_format}'])}) {sql}"

    def get_upsert_inserted_flag_sql(self) -> str | None:
        # A row ON CONFLICT DO UPDATE wrote carries the updating transaction's id in xmax.
        return "(xmax = 0)"
