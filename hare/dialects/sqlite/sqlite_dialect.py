from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.base.transactions.transaction_statements import TransactionStatements
from hare.dialects.enums import DialectName
from hare.dialects.sqlite.clauses.sqlite_query_clauses import SqliteQueryClauses
from hare.dialects.sqlite.literals.sqlite_literals import SqliteLiterals
from hare.dialects.sqlite.parameters.sqlite_parameters import SqliteParameters
from hare.dialects.sqlite.server_versions import (
    SQLITE_DROP_COLUMN_SERVER_VERSION,
    SQLITE_MINIMUM_SERVER_VERSION,
    SQLITE_ORDERED_AGGREGATES_SERVER_VERSION,
    SQLITE_STRICT_SERVER_VERSION,
    SQLITE_UNHEX_SERVER_VERSION,
)
from hare.dialects.sqlite.sqlite_table_options import SqliteTableOptions
from hare.dialects.sqlite.transactions.sqlite_transaction_statements import SqliteTransactionStatements
from hare.transactions.enums import IsolationLevel

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.lookups.filter_operators import FilterOperators
    from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.dialects.base.search.text_search import TextSearch
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector


class SqliteDialect(Dialect):
    """SQLite."""

    name = DialectName.SQLITE
    otel_system_name = "sqlite"
    features = Features(
        inline_comments=True,
        supports_ordered_aggregates=True,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        can_rollback_ddl=True,
        supports_returning=True,
        supports_schemas=False,
        sorts_nulls_first=True,
        enforces_numeric_ranges=False,
        guarantees_returning_order=False,
        # Every SQLite transaction is serializable - writers are serialized by the database lock.
        isolation_levels=(IsolationLevel.SERIALIZABLE,),
        supports_adding_constraints=False,
        supports_partial_indexes=True,
        # An index keeps NULLs first ascending and last descending.
        supports_index_nulls_order=False,
        # Every SQLite trigger fires per row.
        supports_statement_triggers=False,
        # stream() reads a cursor in batches inside a transaction.
        supports_streaming=True,
        supports_drop_column=False,
        # FTS5 - a build without it is read off the library when a client is created.
        supports_full_text_index=True,
    )
    minimum_server_version = SQLITE_MINIMUM_SERVER_VERSION

    def get_server_version_features(self, server_version: tuple[int, ...]) -> dict[str, Any]:
        return {
            "supports_unhex": server_version >= SQLITE_UNHEX_SERVER_VERSION,
            "supports_drop_column": server_version >= SQLITE_DROP_COLUMN_SERVER_VERSION,
            "supports_strict_tables": server_version >= SQLITE_STRICT_SERVER_VERSION,
            "supports_ordered_aggregates": server_version >= SQLITE_ORDERED_AGGREGATES_SERVER_VERSION,
        }

    def build_schema_editor_class(self) -> type[BaseSchemaEditor]:
        # Local import: the schema editor works on hare's models, whose modules import this one.
        from hare.dialects.sqlite.schema.sqlite_schema_editor import SqliteSchemaEditor

        return SqliteSchemaEditor

    def build_table_options_class(self) -> type[TableOptions] | None:
        return SqliteTableOptions

    def build_introspector_class(self) -> type[SchemaIntrospector] | None:
        # Local import: the introspector reads into hare's models and fields, whose modules import this one.
        from hare.dialects.sqlite.sqlite_introspector import SqliteIntrospector

        return SqliteIntrospector

    def build_literals(self) -> SqlLiterals:
        return SqliteLiterals(self)

    def build_migration_safety_rules(self) -> MigrationSafetyRules:
        # Local import: the rules check hare's migration operations, whose modules import this one.
        from hare.dialects.sqlite.migration_safety import SqliteMigrationSafetyRules

        return SqliteMigrationSafetyRules(self)

    def build_parameters(self) -> SqlParameters:
        return SqliteParameters(self)

    def build_clauses(self) -> QueryClauses:
        return SqliteQueryClauses(self)

    def build_transactions(self) -> TransactionStatements:
        return SqliteTransactionStatements(self)

    def build_types(self) -> TypeRegistry:
        # Local import: the registry names hare's field classes, whose modules import this one.
        from hare.dialects.sqlite.types.sqlite_types import SqliteTypes

        return SqliteTypes.build()

    def build_filter_operators(self) -> FilterOperators:
        # Local import: the operators are hare's lookups, whose modules import this one.
        from hare.dialects.sqlite.lookups.sqlite_filter_operators import SqliteFilterOperators

        return SqliteFilterOperators.build(self)

    def build_text_search(self) -> TextSearch:
        # Local import: the search resolves hare's expressions, whose modules import this one.
        from hare.dialects.sqlite.search.sqlite_text_search import SqliteTextSearch

        return SqliteTextSearch(self)

    def build_renderers(self) -> TermRenderers:
        # Local import: the renderers name hare's terms, whose modules import this one.
        from hare.dialects.sqlite.renderers import SqliteRenderers

        return SqliteRenderers(self)
