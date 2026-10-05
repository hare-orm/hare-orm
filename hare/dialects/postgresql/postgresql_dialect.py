from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.dialects.base.clauses.query_clauses import QueryClauses
from hare.dialects.base.dialect import Dialect
from hare.dialects.base.features import Features
from hare.dialects.base.literals.sql_literals import SqlLiterals
from hare.dialects.base.parameters.sql_parameters import SqlParameters
from hare.dialects.base.transactions.transaction_statements import TransactionStatements
from hare.dialects.enums import DialectName
from hare.dialects.postgresql.clauses.constants import POSTGRESQL_EXPLAIN_OPTIONS
from hare.dialects.postgresql.clauses.postgresql_query_clauses import PostgresqlQueryClauses
from hare.dialects.postgresql.literals.postgresql_literals import PostgresqlLiterals
from hare.dialects.postgresql.parameters.constants import POSTGRESQL_MAX_BIND_PARAMETERS
from hare.dialects.postgresql.parameters.postgresql_parameters import PostgresqlParameters
from hare.dialects.postgresql.server_versions import (
    POSTGRESQL_EXPLAIN_OPTION_SERVER_VERSIONS,
    POSTGRESQL_JSON_TABLE_SERVER_VERSION,
    POSTGRESQL_MERGE_RETURNING_SERVER_VERSION,
    POSTGRESQL_MERGE_SERVER_VERSION,
    POSTGRESQL_MINIMUM_SERVER_VERSION,
    POSTGRESQL_NULLS_DISTINCT_SERVER_VERSION,
    POSTGRESQL_PARTITIONED_EXCLUSION_SERVER_VERSION,
    POSTGRESQL_RETURNING_OLD_NEW_SERVER_VERSION,
    POSTGRESQL_UUID_V7_SERVER_VERSION,
    POSTGRESQL_VIRTUAL_GENERATED_COLUMNS_SERVER_VERSION,
    POSTGRESQL_WITHOUT_OVERLAPS_SERVER_VERSION,
)
from hare.dialects.postgresql.transactions.postgresql_transaction_statements import PostgresqlTransactionStatements

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.ddl.table_options import TableOptions
    from hare.dialects.base.lookups.filter_operators import FilterOperators
    from hare.dialects.base.migration_safety.migration_safety_rules import MigrationSafetyRules
    from hare.dialects.base.renderers.term_renderers import TermRenderers
    from hare.dialects.base.schema.base_schema_editor import BaseSchemaEditor
    from hare.dialects.base.search.text_search import TextSearch
    from hare.dialects.base.transactions.two_phase_commit import TwoPhaseCommit
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.inspectdb.introspection.schema_introspector import SchemaIntrospector


class PostgresqlDialect(Dialect):
    """PostgreSQL."""

    name = DialectName.POSTGRESQL
    otel_system_name = "postgresql"
    features = Features(
        supports_nulls_distinct=True,
        supports_partitioned_exclusion_constraints=True,
        supports_update_limit_order_by=False,
        supports_posix_regex=True,
        supports_select_for_no_key_update=True,
        supports_select_for_share=True,
        supports_select_for_key_share=True,
        can_rollback_ddl=True,
        supports_returning=True,
        supports_streaming=True,
        supports_two_phase_commit=True,
        supports_listen_notify=True,
        matches_ordering_to_grouping_by_sql=True,
        supports_distinct_on=True,
        supports_grouping_sets=True,
        supports_lateral=True,
        supports_table_sample=True,
        supports_merge=True,
        supports_merge_returning=True,
        supports_merge_not_matched_by_source=True,
        supports_enum_types=True,
        supports_views=True,
        supports_materialized_views=True,
        supports_database_functions=True,
        supports_sequences=True,
        supports_row_level_security=True,
        supports_grants=True,
        supports_conflict_constraint_names=True,
        supports_conflict_where=True,
        supports_copy=True,
        supports_virtual_generated_columns=True,
        supports_uuid_v7=True,
        supports_without_overlaps=True,
        supports_returning_old_new=True,
        supports_json_table=True,
        checks_foreign_keys_per_cascade_step=True,
        checks_restrict_at_statement_end=True,
        # NAMEDATALEN - 1.
        max_identifier_length=63,
        supports_partial_indexes=True,
        supports_exclusion_constraints=True,
        supports_deferrable_constraints=True,
        supports_concurrent_indexes=True,
        supports_not_valid_constraints=True,
        supports_extensions=True,
        supports_collations=True,
        truncates_values_on_type_change=True,
        binds_array_parameters=True,
        # pgvector, created as an extension wherever a VectorField is used.
        supports_text_search_configurations=True,
        supports_vector_search=True,
        max_bind_parameters=POSTGRESQL_MAX_BIND_PARAMETERS,
        supports_tenant_schemas=True,
        supports_spatial=True,
        supports_geography=True,
        supports_ordered_aggregates=True,
        explain_options=POSTGRESQL_EXPLAIN_OPTIONS,
    )
    minimum_server_version = POSTGRESQL_MINIMUM_SERVER_VERSION

    def get_server_version_features(self, server_version: tuple[int, ...]) -> dict[str, Any]:
        return {
            "supports_nulls_distinct": server_version >= POSTGRESQL_NULLS_DISTINCT_SERVER_VERSION,
            "supports_partitioned_exclusion_constraints": (
                server_version >= POSTGRESQL_PARTITIONED_EXCLUSION_SERVER_VERSION
            ),
            "supports_merge": server_version >= POSTGRESQL_MERGE_SERVER_VERSION,
            "supports_merge_returning": server_version >= POSTGRESQL_MERGE_RETURNING_SERVER_VERSION,
            "supports_merge_not_matched_by_source": server_version >= POSTGRESQL_MERGE_RETURNING_SERVER_VERSION,
            "supports_virtual_generated_columns": server_version
            >= POSTGRESQL_VIRTUAL_GENERATED_COLUMNS_SERVER_VERSION,
            "supports_uuid_v7": server_version >= POSTGRESQL_UUID_V7_SERVER_VERSION,
            "supports_without_overlaps": server_version >= POSTGRESQL_WITHOUT_OVERLAPS_SERVER_VERSION,
            "supports_returning_old_new": server_version >= POSTGRESQL_RETURNING_OLD_NEW_SERVER_VERSION,
            "supports_json_table": server_version >= POSTGRESQL_JSON_TABLE_SERVER_VERSION,
            "explain_options": frozenset(
                option
                for option in POSTGRESQL_EXPLAIN_OPTIONS
                if server_version >= POSTGRESQL_EXPLAIN_OPTION_SERVER_VERSIONS.get(option, ())
            ),
        }

    def build_clauses(self) -> QueryClauses:
        return PostgresqlQueryClauses(self)

    def build_transactions(self) -> TransactionStatements:
        return PostgresqlTransactionStatements(self)

    def build_schema_editor_class(self) -> type[BaseSchemaEditor]:
        # Local import: the schema editor works on hare's models, whose modules import this one.
        from hare.dialects.postgresql.schema.postgresql_schema_editor import PostgresqlSchemaEditor

        return PostgresqlSchemaEditor

    def build_table_options_class(self) -> type[TableOptions] | None:
        # Local import: the table options module imports the constants module, which instantiates this class.
        from hare.dialects.postgresql.postgresql_table_options import PostgresqlTableOptions

        return PostgresqlTableOptions

    def build_introspector_class(self) -> type[SchemaIntrospector] | None:
        # Local import: the introspector reads into hare's models and fields, whose modules import this one.
        from hare.dialects.postgresql.postgresql_introspector import PostgresqlIntrospector

        return PostgresqlIntrospector

    def build_literals(self) -> SqlLiterals:
        return PostgresqlLiterals(self)

    def build_migration_safety_rules(self) -> MigrationSafetyRules:
        # Local import: the rules check hare's migration operations, whose modules import this one.
        from hare.dialects.postgresql.migration_safety import PostgresqlMigrationSafetyRules

        return PostgresqlMigrationSafetyRules(self)

    def build_parameters(self) -> SqlParameters:
        return PostgresqlParameters(self)

    def build_types(self) -> TypeRegistry:
        # Local import: the registry names hare's field classes, whose modules import this one.
        from hare.dialects.postgresql.types.postgresql_types import PostgresqlTypes

        return PostgresqlTypes.build()

    def install(self) -> None:
        # Local import: the transform names hare's fields, whose modules import this one.
        from hare.dialects.postgresql.lookups.trigram.postgresql_trigram_lookups import PostgresqlTrigramLookups
        from hare.dialects.postgresql.lookups.unaccent_transform import UnaccentTransform

        UnaccentTransform.register()
        PostgresqlTrigramLookups.register()

    def build_filter_operators(self) -> FilterOperators:
        # Local import: the operators are hare's lookups, whose modules import this one.
        from hare.dialects.postgresql.lookups.postgresql_filter_operators import PostgresqlFilterOperators

        return PostgresqlFilterOperators.build(self)

    def build_text_search(self) -> TextSearch:
        # Local import: the search resolves hare's expressions, whose modules import this one.
        from hare.dialects.postgresql.search.postgresql_text_search import PostgresqlTextSearch

        return PostgresqlTextSearch(self)

    def build_renderers(self) -> TermRenderers:
        # Local import: the renderers name hare's terms, whose modules import this one.
        from hare.dialects.postgresql.renderers import PostgresqlRenderers

        return PostgresqlRenderers(self)

    def build_two_phase_commit(self) -> TwoPhaseCommit | None:
        # Local import: the statements read the constants module, which instantiates this class.
        from hare.dialects.postgresql.transactions.postgresql_two_phase_commit import PostgresqlTwoPhaseCommit

        return PostgresqlTwoPhaseCommit()
