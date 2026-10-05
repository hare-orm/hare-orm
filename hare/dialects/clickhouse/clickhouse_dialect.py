from __future__ import annotations

from functools import cached_property
from typing import TYPE_CHECKING, Any

from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.clickhouse.clickhouse_table_options import ClickhouseTableOptions
from hare.dialects.clickhouse.cluster.constants import CLICKHOUSE_REPLICATED_JOURNAL_ENGINE
from hare.dialects.clickhouse.enums import ClickhouseDialectName
from hare.dialects.clickhouse.server_versions import (
    CLICKHOUSE_CORRELATED_SUBQUERIES_SERVER_VERSION,
    CLICKHOUSE_JSON_TYPE_SERVER_VERSION,
    CLICKHOUSE_KEY_SERIES_SERVER_VERSION,
    CLICKHOUSE_LIGHTWEIGHT_UPDATE_SERVER_VERSION,
    CLICKHOUSE_MINIMUM_SERVER_VERSION,
    CLICKHOUSE_PROJECTION_REBUILD_SERVER_VERSION,
    CLICKHOUSE_REFRESHABLE_VIEW_SERVER_VERSION,
    CLICKHOUSE_VARIANT_TYPES_SERVER_VERSION,
)
from hare.transactions.enums import IsolationLevel

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.clauses.query_clauses import QueryClauses
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.literals.sql_literals import SqlLiterals
    from hare.dialects.base.lookups.filter_operators import FilterOperators
    from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
    from hare.dialects.base.parameters.sql_parameters import SqlParameters
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.dialects.base.transactions.transaction_statements import TransactionStatements
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector
    from hare.sql.sql_context import SqlContext


class ClickhouseDialect(Dialect):
    """ClickHouse - a columnar database keeping no unique constraint or foreign key, whose transactions,
    generated keys and row locks need a ClickHouse Keeper, and whose UPDATE and DELETE rewrite the stored
    parts or mark their rows."""

    name = ClickhouseDialectName.CLICKHOUSE
    otel_system_name = "clickhouse"
    features = Features(
        supports_transactions=False,
        supports_savepoints=False,
        # A transaction reads the snapshot it began with.
        isolation_levels=(IsolationLevel.REPEATABLE_READ,),
        # Taken from a series of numbers ClickHouse Keeper keeps - a server without them generates none.
        supports_generated_keys=True,
        takes_keys_before_insert=True,
        # The uniqueness and the relations a model declares are checked by hare before each write.
        checks_constraints_before_write=True,
        # The rows of a write's RETURNING are read by their keys.
        returns_rows_by_reading=True,
        # Run by servers of ClickHouse 25.4 on (the dialect of a connection to one), but an EXISTS, which
        # they compute wrongly, and a subquery ordering its own rows, which they refuse.
        supports_correlated_subqueries=False,
        rewrites_correlated_exists=True,
        supports_ordered_correlated_subqueries=False,
        orders_by_correlated_subqueries=False,
        can_rollback_ddl=False,
        supports_select_for_update=False,
        supports_update_limit_order_by=False,
        supports_returning=False,
        supports_foreign_keys=False,
        # A data skipping index blocks a MODIFY COLUMN of the columns it covers.
        alters_indexed_columns=False,
        supports_unique_constraints=False,
        # A database is a namespace of tables, not a schema of the connection's database.
        supports_schemas=False,
        supports_streaming=True,
        streams_without_transaction=True,
        # SAMPLE of a table declaring ClickhouseTableOptions(sample_by=...).
        supports_table_sample=True,
        # A MODIFY COLUMN wraps an integer around, cuts a decimal's digits, keeps a too long text.
        truncates_values_on_type_change=True,
        supports_asof_join=True,
        supports_array_join=True,
        # Points and areas of hare.gis, a geography measured on a sphere.
        supports_spatial=True,
        supports_geography=True,
        # An integer column wraps around on overflow - hare checks the ranges itself.
        enforces_numeric_ranges=False,
        supports_partial_indexes=False,
        supports_index_nulls_order=False,
        supports_statement_triggers=False,
        # A column computed on read is an ALIAS of its expression.
        supports_virtual_generated_columns=True,
        supports_views=True,
        supports_materialized_views=True,
        supports_dictionaries=True,
        inline_comments=True,
        # Rows are loaded in the binary Native format, parsed as no SQL text is.
        supports_copy=True,
        copies_bulk_inserts=True,
        # Every value is written into the statement - there is no limit of bound parameters.
        max_bind_parameters=1_000_000,
    )
    default_table_options: TableOptions | None = ClickhouseTableOptions()
    minimum_server_version = CLICKHOUSE_MINIMUM_SERVER_VERSION

    def __init__(
        self,
        stores_json_natively: bool = False,
        runs_correlated_subqueries: bool = False,
        rebuilds_projections: bool = False,
    ) -> None:
        """
        Args:
            stores_json_natively: Whether a ``JSONField`` is a ``JSON`` column - the dialect of a
                connection to a server with the JSON type (``Features.supports_json_type``); else the
                column holds the JSON text.
            runs_correlated_subqueries: Whether a subquery reads the columns of the query around it - the
                dialect of a connection to a server running them (ClickHouse 25.4); else an ``EXISTS`` is
                written without its correlation and any other correlated subquery is refused.
            rebuilds_projections: Whether a table with projections takes a lightweight ``DELETE``
                (``Features.rebuilds_projections``); else its rows are deleted by a mutation.
        """
        self.stores_json_natively = stores_json_natively
        self.rebuilds_projections = rebuilds_projections
        if runs_correlated_subqueries:
            self.features = type(self).features.replace(supports_correlated_subqueries=True)

    def get_journal_table_options(self, connection: DatabaseClient) -> TableOptions | None:
        """A journal every server of the connection's cluster holds a copy of - each of them reads
        what the others applied."""
        if getattr(connection, "cluster", None) is None:
            return None
        return ClickhouseTableOptions(engine=CLICKHOUSE_REPLICATED_JOURNAL_ENGINE)

    async def synchronize_table(self, connection: DatabaseClient, table_name: str) -> None:
        """Waits for the rows other replicas of the connection's cluster wrote to a replicated table."""
        cluster = getattr(connection, "cluster", None)
        if cluster is not None:
            await cluster.synchronize_table(table_name)

    def get_server_version_features(self, server_version: tuple[int, ...]) -> dict[str, Any]:
        has_key_series = server_version >= CLICKHOUSE_KEY_SERIES_SERVER_VERSION
        return {
            "supports_generated_keys": has_key_series,
            "takes_keys_before_insert": has_key_series,
            "supports_json_type": server_version >= CLICKHOUSE_JSON_TYPE_SERVER_VERSION,
            "supports_variant_types": server_version >= CLICKHOUSE_VARIANT_TYPES_SERVER_VERSION,
            "supports_correlated_subqueries": server_version >= CLICKHOUSE_CORRELATED_SUBQUERIES_SERVER_VERSION,
            "rebuilds_projections": server_version >= CLICKHOUSE_PROJECTION_REBUILD_SERVER_VERSION,
            "supports_lightweight_update": server_version >= CLICKHOUSE_LIGHTWEIGHT_UPDATE_SERVER_VERSION,
            "supports_refreshable_materialized_views": server_version >= CLICKHOUSE_REFRESHABLE_VIEW_SERVER_VERSION,
        }

    @cached_property
    def sql_context(self) -> SqlContext:
        # Local import: the context module imports hare's SQL, which imports the dialects.
        from hare.dialects.clickhouse.query.clickhouse_sql_context import ClickhouseSqlContext

        # ClickHouse names the result column of "t"."c" t.c, not c, when a query joins several tables,
        # and reads NOT (x) IS NULL as the function call NOT(x) compared with NULL.
        return ClickhouseSqlContext.from_context(super().sql_context).copy(
            names_qualified_columns=True, wraps_negated_criteria=True
        )

    def install(self) -> None:
        super().install()
        # Local import: the methods and lookups resolve hare's queries, whose modules import this one.
        from hare.dialects.clickhouse.lookups.clickhouse_global_lookups import ClickhouseGlobalLookups
        from hare.dialects.clickhouse.query.clickhouse_queryset_methods import ClickhouseQuerySetMethods

        ClickhouseQuerySetMethods.register(self.name)
        ClickhouseGlobalLookups.register()

    def build_literals(self) -> SqlLiterals:
        # Local import: the literals read the dialect's constants, which build this dialect.
        from hare.dialects.clickhouse.literals.clickhouse_literals import ClickhouseLiterals

        return ClickhouseLiterals(self)

    def build_parameters(self) -> SqlParameters:
        # Local import: the parameters module imports hare's SQL terms, which import the dialects.
        from hare.dialects.clickhouse.parameters.clickhouse_parameters import ClickhouseParameters

        return ClickhouseParameters(self)

    def build_transactions(self) -> TransactionStatements:
        # Local import: the statements read the client's constants, whose package imports this module.
        from hare.dialects.clickhouse.transactions.clickhouse_transaction_statements import (
            ClickhouseTransactionStatements,
        )

        return ClickhouseTransactionStatements(self)

    def build_clauses(self) -> QueryClauses:
        # Local import: the clauses render hare's queries, whose modules import this one.
        from hare.dialects.clickhouse.clauses.clickhouse_query_clauses import ClickhouseQueryClauses

        return ClickhouseQueryClauses(self)

    def build_types(self) -> TypeRegistry:
        # Local import: the registry names hare's field classes, whose modules import this one.
        from hare.dialects.clickhouse.types.clickhouse_types import ClickhouseTypes

        return ClickhouseTypes.build(self)

    def build_filter_operators(self) -> FilterOperators:
        # Local import: the operators are hare's lookups, whose modules import this one.
        from hare.dialects.clickhouse.lookups.clickhouse_filter_operators import ClickhouseFilterOperators
        from hare.dialects.clickhouse.lookups.clickhouse_json_lookups import ClickhouseJsonLookups
        from hare.dialects.clickhouse.lookups.clickhouse_json_path_lookups import ClickhouseJsonPathLookups
        from hare.dialects.clickhouse.lookups.in_list.clickhouse_large_in_list import ClickhouseLargeInList
        from hare.query.filters import JsonLookups
        from hare.query.filters.lookups.json.json_path_lookups import JsonPathLookups
        from hare.query.filters.lookups.lookups import Lookups

        return ClickhouseFilterOperators(
            self,
            {
                JsonLookups.contains: ClickhouseJsonLookups.contains,
                JsonLookups.contained_by: ClickhouseJsonLookups.contained_by,
                JsonLookups.has_key: ClickhouseJsonLookups.has_key,
                JsonLookups.has_keys: ClickhouseJsonLookups.has_keys,
                JsonLookups.has_any_keys: ClickhouseJsonLookups.has_any_keys,
                JsonPathLookups.greater_than: ClickhouseJsonPathLookups.greater_than,
                JsonPathLookups.greater_equal: ClickhouseJsonPathLookups.greater_equal,
                JsonPathLookups.less_than: ClickhouseJsonPathLookups.less_than,
                JsonPathLookups.less_equal: ClickhouseJsonPathLookups.less_equal,
                JsonPathLookups.between: ClickhouseJsonPathLookups.between,
                Lookups.is_in: ClickhouseLargeInList.is_in,
                Lookups.not_in: ClickhouseLargeInList.not_in,
                Lookups.row_is_in: ClickhouseLargeInList.row_is_in,
                Lookups.row_not_in: ClickhouseLargeInList.row_not_in,
            },
        )

    def build_renderers(self) -> TermRenderers:
        # Local import: the renderers name hare's terms, whose modules import this one.
        from hare.dialects.clickhouse.renderers.clickhouse_renderers import ClickhouseRenderers

        return ClickhouseRenderers(self)

    def build_schema_editor_class(self) -> type[BaseSchemaEditor]:
        # Local import: the schema editor works on hare's models, whose modules import this one.
        from hare.dialects.clickhouse.schema.declarations import ClickhouseSchemaEditor

        return ClickhouseSchemaEditor

    def build_migration_safety_rules(self) -> MigrationSafetyRules:
        # Local import: the rules check hare's migration operations, whose modules import this one.
        from hare.dialects.clickhouse.migration_safety import ClickhouseMigrationSafetyRules

        return ClickhouseMigrationSafetyRules(self)

    def build_table_options_class(self) -> type[TableOptions] | None:
        return ClickhouseTableOptions

    def build_introspector_class(self) -> type[SchemaIntrospector] | None:
        # Local import: the introspector reads into hare's models and fields, whose modules import this one.
        from hare.dialects.clickhouse.clickhouse_introspector import ClickhouseIntrospector

        return ClickhouseIntrospector
