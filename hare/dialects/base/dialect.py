from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.dialects.base.features import Features
from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.dialects.base.lookups.filter_operators import FilterOperators
from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.base.renderers.term_renderers import TermRenderers
from hare.dialects.base.search.text_search import TextSearch
from hare.dialects.base.transactions.transaction_statements import TransactionStatements
from hare.dialects.base.types.type_registry import TypeRegistry
from hare.exceptions import UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.dialects.base.transactions.two_phase_commit import TwoPhaseCommit
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
    from hare.sql.sql_context import SqlContext


class Dialect:
    """A SQL dialect: the language, column types, DDL and catalog of one type of database.

    One instance per dialect, shared by every driver and connection that speaks it. It builds
    nothing itself: each part - literals, parameters, renderers, clauses, transaction statements,
    types, lookup operators, full-text search, schema editor, migration safety rules - is built on
    first use by its ``build_*()`` method, ISO SQL's by default.

    Attributes:
        name: The dialect's name.
        otel_system_name: The OpenTelemetry ``db.system.name`` value for the database.
        features: What the database supports - a driver's client starts from them.
        default_table_options: The table options of a model that declares none of the dialect's
            (``Meta.table_options``) - None for a plain ``CREATE TABLE``.
        minimum_server_version: The oldest server version hare runs on, as ``(major, minor,
            ...)``; empty for any version. A connection to an older server is refused when it
            connects (``check_server_version()``).
    """

    name: str
    otel_system_name: str
    features: Features = Features()
    default_table_options: TableOptions | None = None
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
        from hare.sql.sql_context import DEFAULT_SQL_CONTEXT

        return DEFAULT_SQL_CONTEXT.copy(
            dialect=self,
            quote_char=self.literals.identifier_quote_char,
            alias_quote_char=self.literals.alias_quote_char,
        )

    @cached_property
    def literals(self) -> SqlLiterals:
        """How the dialect writes names and values into the SQL text - built on first use."""
        return self.build_literals()

    def build_literals(self) -> SqlLiterals:
        """Builds the dialect's names and literals.

        Returns:
            The literals - ISO SQL's by default.
        """
        return SqlLiterals(self)

    @cached_property
    def parameters(self) -> SqlParameters:
        """How the dialect binds values - built on first use."""
        return self.build_parameters()

    def build_parameters(self) -> SqlParameters:
        """Builds the dialect's parameters.

        Returns:
            The parameters - ``?`` placeholders, no casts by default.
        """
        return SqlParameters(self)

    @cached_property
    def clauses(self) -> QueryClauses:
        """How the dialect writes the clauses of a statement that differ between databases - built
        on first use."""
        return self.build_clauses()

    def build_clauses(self) -> QueryClauses:
        """Builds the dialect's clauses.

        Returns:
            The clauses - ISO SQL's by default.
        """
        return QueryClauses(self)

    @cached_property
    def transactions(self) -> TransactionStatements:
        """How the dialect sets a transaction up right after ``BEGIN`` - built on first use."""
        return self.build_transactions()

    def build_transactions(self) -> TransactionStatements:
        """Builds the dialect's transaction statements.

        Returns:
            The statements - ISO SQL's by default.
        """
        return TransactionStatements(self)

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
    def text_search(self) -> TextSearch:
        """How the dialect runs ``hare.search``'s full-text search - built on first use."""
        return self.build_text_search()

    def build_text_search(self) -> TextSearch:
        """Builds the dialect's full-text search.

        Returns:
            The search - none by default: each search expression raises ``UnSupportedError``.
        """
        return TextSearch(self)

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
        from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor

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

    def get_journal_table_options(self, connection: DatabaseClient) -> TableOptions | None:
        """Returns how the journal of applied migrations is stored on a connection.

        Args:
            connection: The connection.

        Returns:
            The table's options; None - the default - for a table of the dialect's defaults.
        """
        return None

    async def synchronize_table(self, connection: DatabaseClient, table_name: str) -> None:
        """Waits until a connection's server holds every row written to a table through the other
        servers of its database - nothing by default: a database of one server holds them all.

        Args:
            connection: The connection.
            table_name: The table.
        """

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
    def migration_safety_rules(self) -> MigrationSafetyRules:
        """The rules the migration safety check applies on the dialect's databases - built on first
        use."""
        return self.build_migration_safety_rules()

    def build_migration_safety_rules(self) -> MigrationSafetyRules:
        """Builds the dialect's migration safety rules.

        Returns:
            The rules every database shares by default.
        """
        # Local import: the rules check hare's migration operations, whose modules import this one.
        from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules

        return MigrationSafetyRules(self)

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

    def __str__(self) -> str:
        return str(self.name)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}>"
