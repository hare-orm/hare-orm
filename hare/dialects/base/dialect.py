from __future__ import annotations

import datetime
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from decimal import Decimal
from functools import cached_property
from typing import TYPE_CHECKING, Any

from hare.dialects.base.operators import FilterOperators
from hare.dialects.base.renderers.term_renderers import TermRenderers
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.dialects.enums import ParameterPosition
from hare.exceptions import UnSupportedError, ValidationError
from hare.transactions.enums import IsolationLevel

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.schema.editor import BaseSchemaEditor
    from hare.dialects.base.two_phase_commit import TwoPhaseCommit
    from hare.fields.base.field import Field
    from hare.inspectdb.introspector.schema_introspector import SchemaIntrospector
    from hare.models import Model
    from hare.query.lookup_info.lookup_info import LookupInfo
    from hare.sql.context import SqlContext
    from hare.sql.enums import JsonValueType
    from hare.sql.terms.base.term import Term


class Dialect:
    """A SQL dialect: the language, column types, DDL and catalog of one type of database.

    One instance per dialect, shared by every driver and connection that speaks it. The syntax
    hooks default to ISO SQL.

    Attributes:
        name: The dialect's name.
        otel_system_name: The OpenTelemetry ``db.system.name`` value for the database.
        supports_schemas: Whether a table can be qualified by a schema; without, a schema-qualified
            model's table is used unqualified.
        is_distinct_from_operator: The NULL-safe inequality operator.
        supports_distinct_on: Whether ``SELECT DISTINCT ON (...)`` exists.
        sorts_nulls_first: Whether NULL sorts before every value in ascending order by default.
        placeholder_template: A bound parameter's placeholder, ``{}`` standing for its index from 1.
        enforces_numeric_ranges: Whether integer and decimal columns reject values out of their
            range or scale.
        single_parameter_in_list_min_length: The length from which an ``__in``/``__not_in`` list
            binds as one parameter - a plan key holds no length for such a list. None for a dialect
            binding every value as a parameter of its own.
        supports_conflict_constraint_names: Whether ``ON CONFLICT ON CONSTRAINT name`` exists.
        supports_conflict_where: Whether an ``ON CONFLICT`` target takes a ``WHERE``.
        guarantees_returning_order: Whether a multi-row ``INSERT ... RETURNING`` returns its rows
            in the order they were written.
        supports_copy: Whether the bulk ``COPY`` protocol exists.
        supports_virtual_generated_columns: Whether a generated column can be computed on read.
        matches_ordering_to_grouping_by_sql: Whether an ordering term that is also grouped by has
            to be written exactly as in ``GROUP BY``.
        checks_foreign_keys_per_cascade_step: Whether a ``NO ACTION`` foreign key is checked after
            every nested step of an ``ON DELETE CASCADE`` rather than once at the end of the
            statement - then even a single ``DELETE`` fails on a row guarded by an
            ``on_delete=PROTECT`` relation that the same cascade removes the guarding row of later,
            unless the PROTECT constraints are deferred.
        checks_restrict_at_statement_end: Whether a ``RESTRICT`` foreign key is checked at the end
            of the statement like ``NO ACTION``, rather than at once, before the statement's own
            cascade goes on.
        isolation_levels: The isolation levels the database runs a transaction at, weakest first.
        max_identifier_length: The most bytes a table, column, index, constraint or alias name
            may take before the database truncates or rejects it, None for no limit. hare keeps
            the names it generates within ``IDENTIFIER_LENGTH_LIMIT`` (63) bytes; a dialect
            with a lower limit is refused on registration.
        supports_adding_constraints: Whether ``ALTER TABLE ... ADD CONSTRAINT`` exists; without,
            a new table's CHECK constraints go into ``CREATE TABLE`` and its unique constraints
            become unique indexes, and a later change rebuilds the table.
        supports_partial_indexes: Whether an index takes a ``WHERE`` condition.
        supports_exclusion_constraints: Whether exclusion constraints exist.
        supports_deferrable_constraints: Whether a constraint or constraint trigger can be
            ``DEFERRABLE`` and deferred with ``SET CONSTRAINTS``.
        supports_index_nulls_order: Whether an index key sets where NULLs sort.
        supports_concurrent_indexes: Whether an index is built or dropped without blocking
            writes, outside a transaction.
        supports_not_valid_constraints: Whether a constraint is added without checking the
            existing rows, which are checked later.
        supports_statement_triggers: Whether a trigger fires ``FOR EACH STATEMENT``.
        supports_extensions: Whether the database installs extensions.
        supports_collations: Whether the database creates collations.
        truncates_values_on_type_change: Whether changing a column's type with an explicit cast
            silently cuts a too-long string or rounds an over-precise number instead of failing,
            so a narrowing change is checked against the table's data first.
        binds_array_parameters: Whether a list is bound as one array parameter (a ``RawSQL``
            parameter such as ``= ANY(%s)``).
        supports_foreign_keys: Whether the database enforces foreign keys. Without, a relation's
            table gets no FOREIGN KEY constraint, and hare runs every ``on_delete`` action and
            ``PROTECT`` check itself, as for a ``db_constraint=False`` relation.
        identifier_quote_char: The character a table, column, index or constraint name is quoted
            with, a doubled one inside the name standing for itself.
        alias_quote_char: The character a SELECT alias is quoted with, empty for a bare alias.
        supports_unique_constraints: Whether the database enforces uniqueness. Without, a unique
            field, a ``UniqueConstraint`` and a unique through table get no
            constraint, a unique ``Index`` is a plain index, and an upsert can target the
            primary key only.
        minimum_server_version: The oldest server version hare runs on, as ``(major, minor,
            ...)``; empty for any version. A connection to an older server is refused when it
            connects (``check_server_version()``).
    """

    name: str
    otel_system_name: str
    supports_schemas: bool = True
    is_distinct_from_operator: str = "IS DISTINCT FROM"
    supports_distinct_on: bool = False
    sorts_nulls_first: bool = False
    placeholder_template: str = "?"
    enforces_numeric_ranges: bool = True
    single_parameter_in_list_min_length: int | None = None
    supports_conflict_constraint_names: bool = False
    supports_conflict_where: bool = False
    guarantees_returning_order: bool = True
    supports_copy: bool = False
    supports_virtual_generated_columns: bool = True
    matches_ordering_to_grouping_by_sql: bool = False
    checks_foreign_keys_per_cascade_step: bool = False
    checks_restrict_at_statement_end: bool = False
    isolation_levels: tuple[IsolationLevel, ...] = tuple(IsolationLevel)
    max_identifier_length: int | None = None
    supports_adding_constraints: bool = True
    supports_partial_indexes: bool = False
    supports_exclusion_constraints: bool = False
    supports_deferrable_constraints: bool = False
    supports_index_nulls_order: bool = True
    supports_concurrent_indexes: bool = False
    supports_not_valid_constraints: bool = False
    supports_statement_triggers: bool = True
    supports_extensions: bool = False
    supports_collations: bool = False
    truncates_values_on_type_change: bool = False
    binds_array_parameters: bool = False
    supports_foreign_keys: bool = True
    supports_unique_constraints: bool = True
    identifier_quote_char: str = '"'
    alias_quote_char: str = ""
    minimum_server_version: tuple[int, ...] = ()

    def check_server_version(self, server_version: tuple[int, ...]) -> None:
        """Refuses a server older than ``minimum_server_version``.

        Args:
            server_version: The server's version, as ``(major, minor, ...)``.

        Raises:
            UnSupportedError: The server is older than ``minimum_server_version``.
        """
        if server_version < self.minimum_server_version:
            raise UnSupportedError(
                f"The {self} server is version {'.'.join(map(str, server_version))}, older than "
                f"{'.'.join(map(str, self.minimum_server_version))}, the oldest hare runs on"
            )

    def get_server_version_features(self, server_version: tuple[int, ...]) -> dict[str, Any]:
        """Returns the connection features that depend on the server's version - applied over a
        connection's ``Features`` once it has connected. None by default.

        Args:
            server_version: The server's version, as ``(major, minor, ...)``.

        Returns:
            ``Features`` field values by name.
        """
        return {}

    def install(self) -> None:
        """Adds what the dialect brings to hare's own field types - a path transform reading
        inside a core field's value (PostgreSQL's ``name__unaccent``) - once, when the dialect is
        registered. Does nothing by default.
        """

    @cached_property
    def sql_context(self) -> SqlContext:
        """The context the dialect's SQL renders in - its quoting, its renderers - built on first use."""
        # Local import: the SQL context module imports the dialects package.
        from hare.sql.context import DEFAULT_SQL_CONTEXT

        return DEFAULT_SQL_CONTEXT.copy(
            dialect=self, quote_char=self.identifier_quote_char, alias_quote_char=self.alias_quote_char
        )

    def quote_identifier(self, name: str) -> str:
        """Quotes a table, column, index or constraint name for the dialect's SQL.

        Args:
            name: The name.

        Returns:
            The quoted name.
        """
        escaped = name.replace(self.identifier_quote_char, self.identifier_quote_char * 2)
        return f"{self.identifier_quote_char}{escaped}{self.identifier_quote_char}"

    @cached_property
    def types(self) -> TypeRegistry:
        """How the dialect stores hare's fields - built on first use."""
        types = self.build_types()
        types.in_use = True
        return types

    def build_types(self) -> TypeRegistry:
        """Builds the dialect's type registry.

        Returns:
            The registry.
        """
        raise NotImplementedError

    @cached_property
    def filter_operators(self) -> FilterOperators:
        """The operators the dialect runs lookups with - built on first use."""
        return self.build_filter_operators()

    def build_filter_operators(self) -> FilterOperators:
        """Builds the dialect's lookup operators.

        Returns:
            The operators.
        """
        raise NotImplementedError

    @cached_property
    def schema_editor_class(self) -> type[BaseSchemaEditor]:
        """The schema editor running the dialect's DDL, for ``generate_schemas()`` and migrations
        alike - built on first use."""
        return self.build_schema_editor_class()

    def build_schema_editor_class(self) -> type[BaseSchemaEditor]:
        """Returns the dialect's schema editor class.

        Returns:
            The class.
        """
        # Local import: the schema editor works on hare's models, whose modules import this one.
        from hare.dialects.base.schema.editor import BaseSchemaEditor

        return BaseSchemaEditor

    @cached_property
    def introspector_class(self) -> type[SchemaIntrospector] | None:
        """How the dialect reads an existing database's schema, for inspectdb and drift - built on
        first use, None for a dialect without one."""
        return self.build_introspector_class()

    def build_introspector_class(self) -> type[SchemaIntrospector] | None:
        """Returns the dialect's schema introspector.

        Returns:
            The ``SchemaIntrospector`` subclass, None when the dialect has none.
        """
        return None

    @cached_property
    def table_options_class(self) -> type[TableOptions] | None:
        """The ``TableOptions`` subclass models declare the dialect's table storage with, which its
        introspector reads back - built on first use, None for a dialect without table options."""
        return self.build_table_options_class()

    def build_table_options_class(self) -> type[TableOptions] | None:
        """Returns the dialect's table options class.

        Returns:
            The ``TableOptions`` subclass, None when the dialect has none.
        """
        return None

    @cached_property
    def two_phase_commit(self) -> TwoPhaseCommit | None:
        """The statements of ``Transactions.distributed()``'s two-phase commit - built on first
        use, None for a dialect without two-phase commit."""
        return self.build_two_phase_commit()

    def build_two_phase_commit(self) -> TwoPhaseCommit | None:
        """Builds the dialect's two-phase commit statements.

        Returns:
            The statements, None when the dialect has no two-phase commit.
        """
        return None

    @cached_property
    def renderers(self) -> TermRenderers:
        """How the dialect renders the terms whose SQL differs between dialects - built on first use."""
        return self.build_renderers()

    def build_renderers(self) -> TermRenderers:
        """Builds the dialect's term renderers.

        Returns:
            The renderers.
        """
        raise NotImplementedError

    def get_placeholder(self, index: int) -> str:
        """The placeholder of the ``index``-th (from 1) bound parameter."""
        return self.placeholder_template.format(index)

    @property
    def numbers_parameters(self) -> bool:
        """Whether a placeholder names its parameter's number (``$1``) - a value rendered twice
        then binds once, and the database sees both places as the same expression.

        Returns:
            True for numbered placeholders.
        """
        return "{}" in self.placeholder_template

    def get_bindable_number(self, value: int | float | Decimal) -> int | float | Decimal:
        """A number as bound where it stands for a JSON number.

        Args:
            value: The number.

        Returns:
            The value to bind.
        """
        return value

    def get_default_rows_source_sql(self, row_count: int) -> str | None:
        """The source an ``INSERT INTO table <source>`` writes ``row_count`` rows of column
        defaults from in one statement, None when each row needs a statement of its own.

        Args:
            row_count: How many rows.

        Returns:
            The SQL, or None.
        """
        return None

    def qualify_table_name(self, table_name: str, schema: str | None = None) -> str:
        """Quotes a table name, with its schema where the dialect has schemas.

        Args:
            table_name: The table.
            schema: Its schema; None for the connection's current one.

        Returns:
            The quoted name.
        """
        if schema and self.supports_schemas:
            return f"{self.quote_identifier(schema)}.{self.quote_identifier(table_name)}"
        return self.quote_identifier(table_name)

    def get_string_literal_sql(self, text: str) -> str:
        """A string written into the SQL text as a literal.

        Args:
            text: The string.

        Returns:
            The literal, its quotes doubled.

        Raises:
            ValidationError: The string holds a null byte.
        """
        # Local import: hare.sql imports the dialect constants, which import this module.
        from hare.sql.constants import SQL_NULL_BYTE, SQL_NULL_BYTE_MESSAGE

        if SQL_NULL_BYTE in text:
            raise ValidationError(SQL_NULL_BYTE_MESSAGE.format(text=text))
        return "'" + text.replace("'", "''") + "'"

    def get_literal_sql(self, value: Any) -> str:
        """A value written into the SQL text as a literal - a column default.

        Args:
            value: The value, already in its database form (``Field.to_db_value()``).

        Returns:
            The literal; ``repr(value)`` for a value of no known type.
        """
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, int | Decimal):
            return str(value)
        if isinstance(value, float):
            return f"{value:.15g}"
        if isinstance(value, str):
            return self.get_string_literal_sql(value)
        if isinstance(value, datetime.datetime | datetime.time):
            return self.get_string_literal_sql(value.isoformat())
        if isinstance(value, datetime.date):
            return self.get_string_literal_sql(f"{value.year:04}-{value.month:02}-{value.day:02}")
        if isinstance(value, datetime.timedelta):
            return self.get_string_literal_sql(self.get_duration_text(value))
        if isinstance(value, bytes):
            return self.get_bytes_literal_sql(value)
        return repr(value)

    @staticmethod
    def get_duration_text(duration: datetime.timedelta) -> str:
        """A duration as ``[-]HH:MM:SS[.ffffff]``.

        Args:
            duration: The duration.

        Returns:
            The text.
        """
        total_microseconds = (duration.days * 86400 + duration.seconds) * 1_000_000 + duration.microseconds
        sign = "-" if total_microseconds < 0 else ""
        total_microseconds = abs(total_microseconds)
        microseconds = total_microseconds % 1_000_000
        total_seconds = total_microseconds // 1_000_000
        text = f"{sign}{total_seconds // 3600:02d}:{(total_seconds // 60) % 60:02d}:{total_seconds % 60:02d}"
        return f"{text}.{microseconds:06d}" if microseconds else text

    def get_bytes_literal_sql(self, value: bytes) -> str:
        """A bytes value written into the SQL text."""
        return f"X'{value.hex()}'"

    def get_array_literal_sql(self, element_sqls: Sequence[str]) -> str:
        """An array written into the SQL text from its elements' SQL."""
        return f"ARRAY[{','.join(element_sqls)}]"

    def get_cast_parameter_sql(self, parameter_sql: str, value: Any) -> str:
        """A bare parameter nothing around it types (a ``CASE`` branch, a boolean), cast to the
        type of ``value`` where the dialect needs it.

        Args:
            parameter_sql: The parameter's placeholder.
            value: The bound value.

        Returns:
            The parameter SQL.
        """
        return parameter_sql

    def get_parameter_cast_type(self, value: Any, position: ParameterPosition) -> str | None:
        """The SQL type a bound literal is cast to where nothing around it types the parameter.

        Args:
            value: The value the literal binds as.
            position: Where the literal stands in the statement.

        Returns:
            The type, None when the database types the parameter itself - always, by default.
        """
        return None

    def get_json_object_value_cast_type(self, value: Any, value_type: JsonValueType) -> str | None:
        """The SQL type a bound literal written into a JSON object is cast to.

        Args:
            value: The value the literal binds as.
            value_type: How the value is written into the object.

        Returns:
            The type, None when the database types the parameter itself - always, by default.
        """
        return None

    def get_upsert_inserted_flag_sql(self) -> str | None:
        """A boolean expression a ``RETURNING`` row of ``INSERT ... ON CONFLICT DO UPDATE`` holds
        - true for a row the statement inserted, false for one it updated.

        Returns:
            The expression, None when the database has none - an upsert then reads which rows
            exist before it writes, when a listener needs to know.
        """
        return None

    def get_field_parameter_cast_type(self, field: Field[Any]) -> str | None:
        """The SQL type a parameter holding a value of a field's column is cast to where nothing
        around it types the parameter (the operand of ``IS NULL``, a ``VALUES`` row).

        Args:
            field: The field.

        Returns:
            The type, None when the database types the parameter itself - always, by default.
        """
        return None

    def get_concatenated_argument_sql(self, argument_sql: str, argument: Any) -> str:
        """An argument of a text concatenation, as the database takes it.

        Args:
            argument_sql: The argument's SQL.
            argument: The argument's term.

        Returns:
            The SQL - unchanged by default; a database that types a parameter only from what's
            around it casts the argument.
        """
        return argument_sql

    def get_json_path_comparand(
        self, value: Any, encode_json_text: Callable[[Any], str], column_type: str | None, *, as_parameter: bool
    ) -> Any:
        """A value compared with the JSON value at a path.

        Args:
            value: A JSON-compatible Python value; ``None`` is the JSON ``null``.
            encode_json_text: Gives a value's JSON text, checking it.
            column_type: The SQL type of the JSON column.
            as_parameter: The value is an item of a list bound as one array parameter.

        Returns:
            The JSON text cast to the JSON type by default - the JSON text alone for an item of an
            array parameter.
        """
        # Local import: hare.sql renders through the dialect.
        from hare.sql.functions.cast import Cast
        from hare.sql.terms.base.value_wrapper import ValueWrapper

        json_text = encode_json_text(value)
        if as_parameter or column_type is None:
            return json_text
        return Cast(ValueWrapper(json_text), column_type)

    def get_integer_aggregate_as_float(self, term: Term) -> Term:
        """The average or a statistic of integers as a float - the term itself by default.

        Args:
            term: The aggregate.

        Returns:
            The term the float is read from.
        """
        return term

    def get_composite_distinct_key(self, terms: Sequence[Term]) -> Term:
        """The value ``COUNT(DISTINCT ...)`` counts a key of several columns by.

        Args:
            terms: The key's columns.

        Returns:
            A row value of them.
        """
        # Local import: hare.sql's terms render through the dialect.
        from hare.sql.terms.tuple import Tuple

        return Tuple(*terms)

    def get_connection_only_function(self, sql: str) -> str | None:
        """A function in ``sql`` that exists only on hare's own connections - none can be part of
        DDL, which the database runs on its own.

        Args:
            sql: The SQL text.

        Returns:
            The function's name, None when there's none.
        """
        return None

    def get_unbounded_limit_sql(self) -> str | None:
        """The ``LIMIT`` an ``OFFSET`` needs when no limit is set, None when it needs none."""
        return None

    def get_isolation_level(self, requested: IsolationLevel) -> IsolationLevel:
        """Returns the isolation level a transaction asking for ``requested`` runs at: the
        weakest of ``isolation_levels`` at least as strong as ``requested``. The SQL standard lets
        a database run a transaction at a stronger level than the one asked for, never a weaker
        one.

        Args:
            requested: The level asked for.

        Returns:
            The level the transaction runs at.

        Raises:
            UnSupportedError: The database has no level at least as strong as ``requested``.
        """
        strength_order = list(IsolationLevel)
        for level in self.isolation_levels:
            if strength_order.index(level) >= strength_order.index(requested):
                return level
        raise UnSupportedError(
            f"The {self} dialect runs no transaction at the {requested!s} isolation level or a stronger one"
        )

    def get_isolation_level_sql(self, level: IsolationLevel) -> str | None:
        """Returns the statement that makes a just-begun transaction run at ``level``, one of
        ``isolation_levels``.

        Args:
            level: The isolation level.

        Returns:
            The statement, None when every transaction already runs at that level.
        """
        return f"SET TRANSACTION ISOLATION LEVEL {level.upper()}"

    async def clear_tables(self, db: DatabaseClient, quoted_tables: Sequence[str]) -> None:
        """Deletes every row of the tables - a test database is reset between tests with it.

        Args:
            db: The connection the tables are on.
            quoted_tables: The quoted, schema-qualified where the dialect has schemas, table names,
                a table referencing another before it.
        """
        if quoted_tables:
            # One script - a statement per table would be a round trip per table.
            await db.execute_script("".join(f"DELETE FROM {quoted_table};\n" for quoted_table in quoted_tables))  # nosec

    def defer_cascade_foreign_keys(self, model: type[Model], db: DatabaseClient) -> AbstractAsyncContextManager[bool]:
        """Defers, for the block, the foreign keys a hard delete of ``model`` rows made of several
        statements (or of one, see ``checks_foreign_keys_per_cascade_step``) can run into, so a row
        guarded by an ``on_delete=PROTECT`` relation doesn't fail the delete when the same cascade
        removes the guarding row too. The deferred constraints are checked again when the block
        ends.

        Args:
            model: The model rows are deleted from.
            db: A client inside the transaction the delete runs in.

        Returns:
            An async context manager yielding whether anything was deferred.

        Raises:
            UnSupportedError: The dialect has no way to defer them.
        """
        raise UnSupportedError(f"on_delete=PROTECT inside a cascade has no deferral on the {self} dialect")

    def get_lock_table_sql(self, qualified_table: str) -> str | None:
        """Returns the statement locking a table against concurrent writes until the end of the
        transaction, while still letting it be read.

        Args:
            qualified_table: The quoted, schema-qualified table.

        Returns:
            The statement, None where a transaction's own writes already keep other writers out.
        """
        return None

    def get_index_include_sql(self, quoted_columns: Sequence[str]) -> str:
        """The clause storing non-key columns in an index, after its keys.

        Args:
            quoted_columns: The non-key columns, quoted.

        Returns:
            The clause with its leading space; ``""`` by default - a dialect without non-key index
            columns leaves them out, as they only make the index cover more queries.
        """
        return ""

    def get_nulls_distinct_sql(self, nulls_distinct: bool) -> str:
        """The clause of a unique constraint stating whether NULLs collide.

        Args:
            nulls_distinct: Whether rows with NULLs stay distinct.

        Returns:
            The clause with its leading space; ``""`` by default.
        """
        return ""

    def get_exclusion_constraint_extension(
        self, constraint: ExclusionConstraint, fields_by_name: Mapping[str, Field[Any]]
    ) -> str | None:
        """The database extension an exclusion constraint needs on this dialect beyond the
        constraint support itself - the schema editor and the migration autodetector create it
        with the constraint's model.

        Args:
            constraint: The constraint.
            fields_by_name: The fields of the constraint's model, by name.

        Returns:
            The extension's name, None by default.
        """
        return None

    def supports_copy_column_type(self, column_type: str) -> bool:
        """Whether the bulk ``COPY`` protocol loads a column type - ``bulk_create(use_copy=True)``
        checks every column before loading any row.

        Args:
            column_type: The column type, without a length or precision (``VARCHAR``).

        Returns:
            True when ``supports_copy`` is set and the type is one ``COPY`` loads; False by default.
        """
        return False

    def get_migration_lock_sql(self) -> str | None:
        """Returns the statement taking hare's migration lock inside a transaction, held until the
        transaction ends - ``migrate`` holds it on a connection of its own for the whole run, so two
        processes migrating one database at once apply their migrations one after the other.

        Returns:
            The statement, None where the database has no such lock.
        """
        return None

    def get_explain_sql(self, sql: str, output_format: str | None, options: Mapping[str, bool]) -> str:
        """The statement showing the plan of ``sql``.

        Args:
            sql: The explained statement.
            output_format: The plan's output format, None for the dialect's default.
            options: The EXPLAIN options, each turned on or off.

        Returns:
            The EXPLAIN statement.

        Raises:
            UnSupportedError: The dialect has no such format or option.
        """
        raise NotImplementedError

    def get_decimal_compared_term(self, term: Term, *, only_decimals: bool) -> Term:
        """What an annotation compared with Decimal values is compared as - an aggregate or
        expression result carries no column type or collation of its own.

        Args:
            term: The annotation's term.
            only_decimals: Whether every compared value is a Decimal.

        Returns:
            The term itself by default - a decimal is a number to the database.
        """
        return term

    def get_decimal_value_term(self, term: Any) -> Any:
        """A Decimal literal, or a DecimalField column, where it meets other numbers - a CASE or
        COALESCE result.

        Args:
            term: A resolved term, or a raw Python value not wrapped into a term yet.

        Returns:
            The term itself by default.
        """
        return term

    def get_ordering_term(self, term: Term, ctx: SqlContext) -> Term:
        """What ``ORDER BY`` sorts by for a term.

        Args:
            term: The ordered term.
            ctx: The context the clause renders in.

        Returns:
            The term itself by default.
        """
        return term

    def get_assigned_decimal_term(self, term: Term, max_digits: int, decimal_places: int) -> Term:
        """What an UPDATE sets a decimal column to for an expression.

        Args:
            term: The expression.
            max_digits: The column's ``max_digits``.
            decimal_places: The column's ``decimal_places``.

        Returns:
            The term itself by default - the column type rounds it to its scale.
        """
        return term

    def get_decimal_dividend(self, term: Term) -> Term:
        """The dividend of a division of Decimals.

        Args:
            term: The dividend.

        Returns:
            The term itself by default.
        """
        return term

    def get_datetime_part_comparand(self, value: datetime.date | datetime.time) -> Any:
        """What a datetime's date or time of day (``created__date=``, ``created__time__lt=``) is
        compared with.

        Args:
            value: The date, or the naive time of day.

        Returns:
            The value itself by default.
        """
        return value

    def supports_lookup(self, lookup_info: LookupInfo) -> bool:
        """Whether a query on this dialect can run a lookup.

        Args:
            lookup_info: The lookup, as ``Model._meta.get_lookup_info()`` describes it.

        Returns:
            True when the field and the lookup exist on this dialect, the dialect implements the
            lookup's operator (an operator only dialects implement has to be replaced in
            ``filter_operators``), and the dialect has extensions where the lookup needs one.
        """
        if lookup_info.dialects is not None and self.name not in lookup_info.dialects:
            return False
        if lookup_info.requires_extension is not None and not self.supports_extensions:
            return False
        field_lookup = lookup_info.field_lookup
        return field_lookup is None or self.filter_operators.supports_operator(field_lookup)

    def __str__(self) -> str:
        return str(self.name)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"
