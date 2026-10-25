from __future__ import annotations

import dataclasses
import itertools
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, ClassVar, Self, cast

from hare.core.caching.cache import Cache
from hare.dialects.constants import DEFAULT_MAX_BIND_PARAMETERS, REPLACED_FEATURES_CACHE_SIZE
from hare.transactions.enums import IsolationLevel


@dataclass(frozen=True, slots=True)
class Features:
    """What a database and its driver support - the one place every "can it" of hare is answered.

    A dialect declares what its database supports (``Dialect.features``); a driver's client starts
    from them, sets what the driver adds, and applies what depends on the server's version
    (``Dialect.get_server_version_features()``) once it has connected.

    Attributes:
        supports_transactions: Transactions.
        supports_savepoints: Savepoints inside a transaction - a nested ``atomic()`` undone apart from
            the transaction; without them a nested block joins the transaction, and an error leaving
            it rolls the whole transaction back.
        takes_keys_before_insert: The database hands out the generated keys of the rows before they are
            written - a series of numbers: hare takes them and writes
            each row with its key.
        supports_recursive_queries: The database runs a recursive ``WITH`` query - ``with_recursive()``;
            without, it is refused.
        supports_recursive_union_distinct: A recursive ``WITH`` query adds its rows by ``UNION`` - each
            row once, so a cycle ends; without, a cascade down a tree of rows of one model is walked
            level by level.
        table_alias_hides_name: A table read under an alias answers to the alias alone; without, its
            own name names it too, and a subquery over the table its enclosing query reads takes its
            rows from a derived table - the name is the enclosing query's then.
        supports_generated_keys: The database generates a key for a row written without one (an
            identity or auto-increment column) and tells it back. Without, a model whose primary key
            the database generates can't be written on the connection.
        supports_row_updates: ``UPDATE`` changes stored rows. Without, ``save()`` of a stored row,
            ``QuerySet.update()``, ``bulk_update()`` and every write that changes a row in place are
            refused before any SQL is sent.
        supports_row_deletes: ``DELETE`` removes stored rows. Without, ``delete()`` and every write
            that removes rows are refused before any SQL is sent.
        rewrites_correlated_exists: An ``EXISTS`` correlated by equalities of columns is written as a
            membership test where correlated subqueries run too - the database refuses its own in a
            join's ``ON`` and over a ``UNION DISTINCT``; one correlated by more than equalities is the
            database's own where correlated subqueries run.
        orders_by_correlated_subqueries: A correlated subquery runs in ``ORDER BY``, and selected beside
            a ``WHERE`` - without, such a query selects its rows in a derived table of no ``WHERE``, its
            conditions and sort keys hidden columns of it, and filters, sorts and slices the rows of that
            (the database runs neither); an ``UPDATE`` or a ``DELETE`` runs no correlated subquery there.
        supports_ordered_correlated_subqueries: A correlated subquery orders and limits its own rows
            (``Subquery(... .order_by(...)[:1])``).
        supports_correlated_aggregates: A correlated subquery selecting a ``COUNT``, ``SUM`` or another
            aggregate of every row counts each row once - without, one correlated by a column rows of
            the query around it share raises ``UnSupportedError``; correlated by its primary key or a
            unique field it runs, as do ``MAX``, ``MIN`` and ``AVG``, which rows read more than once
            don't change.
        supports_correlated_subqueries: A subquery reads the columns of the query around it. Without,
            a filter across a to-many relation is written as a key ``IN (SELECT ...)``, and a query
            needing a correlated subquery (``Exists`` or ``Subquery`` with ``OuterReference``) is refused
            before any SQL is sent.
        can_rollback_ddl: DDL runs inside a transaction and rolls back with it.
        supports_select_for_update: ``SELECT ... FOR UPDATE``.
        supports_select_for_no_key_update: ``SELECT ... FOR NO KEY UPDATE``.
        supports_select_for_share: ``SELECT ... FOR SHARE``.
        supports_select_for_key_share: ``SELECT ... FOR KEY SHARE``.
        supports_update_limit_order_by: ``UPDATE``/``DELETE`` with ``ORDER BY`` and ``LIMIT``.
        supports_posix_regex: POSIX regular expression lookups.
        cuts_like_pattern_at_null_byte: ``LIKE`` reads a pattern up to its first null byte - a pattern
            holding one is refused, as it would match what it doesn't hold.
        supports_returning: ``INSERT ... RETURNING``.
        supports_two_phase_commit: ``PREPARE TRANSACTION``/``COMMIT PREPARED``.
        supports_listen_notify: ``LISTEN``/``NOTIFY``.
        inline_comments: Table and column comments go inside ``CREATE TABLE``.
        supports_positional_rows: The rows of ``execute(rows_by_position=True)`` read by position,
            their column names in ``StatementResult.column_names`` - a reader by position asks for
            them so; the rows of ``execute()`` read by column name.
        supports_streaming: ``stream()`` pages rows off a cursor inside a transaction.
        streams_without_transaction: ``stream()`` reads rows outside a transaction too - the database
            sends a query's rows as it computes them, no cursor of a transaction holds them.
        execute_many_scales_poorly: ``executemany()`` is slower than one multi-row statement.
        binds_written_parameters: The driver binds the parameters ``rust.native.rows.ModelWriter``
            writes (``write_parameters()``) without reading each value from a Python object.
        max_bind_parameters: Most bind parameters one statement may carry.
        max_expression_depth: The deepest expression a statement may hold, None for no limit - a
            list of OR-ed key groups is as deep as it is long.
        max_bulk_update_rows: Most rows one ``bulk_update()`` statement writes, None for no limit
            but ``max_bind_parameters`` - a statement finding each row in the list of rows costs
            more per row the longer the list.
        cascade_depth_limit: The recursion depth a native ``ON DELETE CASCADE`` stops at, None for
            none. The client then raises ``CascadeDepthLimitError`` (or a subclass) when it stops,
            and ``delete()`` of a model with a cascade cycle retries such a DELETE with a
            Python-side cascade walk, deleting fewer levels than this at a time.
        supports_nulls_distinct: A unique constraint takes ``NULLS [NOT] DISTINCT``.
        explain_options: The options ``EXPLAIN`` takes, upper-case.
        supports_schemas: Migrations create and drop the schemas of ``Meta.schema`` (``CREATE SCHEMA``);
            without, a model's schema - another attached database, another database of the server - is
            written as it is and has to exist.
        references_other_schemas: A foreign key references a table of another schema - without, only one of
            its own.
        supports_distinct_on: ``SELECT DISTINCT ON (...)``.
        supports_grouping_sets: ``GROUP BY`` takes ``ROLLUP``, ``CUBE`` and ``GROUPING SETS``, and ``GROUPING()``
            tells their groups apart.
        supports_lateral: A subquery in ``FROM`` joined ``LATERAL`` reads the columns of the tables
            before it.
        supports_table_sample: A table in ``FROM`` is read in a sample - ``TABLESAMPLE``.
        supports_asof_join: A table is joined ``ASOF`` - each row with the row of the other table
            closest to it by an inequality (``AsofJoin``).
        supports_array_join: Each row is repeated with each element of an array of it - ``ARRAY JOIN``
            (``ArrayJoin``).
        orders_json_values_in_aggregates: ``MAX``/``MIN`` of a JSON value compare it as the database orders
            it - without, the database orders a typed JSON value but takes none in an aggregate.
        reads_trailing_comment_of_insert: A comment ending an ``INSERT ... VALUES`` statement is a
            comment - else the database reads it as data, and query tags go before such a statement.
        supports_merge: ``MERGE INTO ... USING ... WHEN ...``.
        supports_merge_returning: ``MERGE ... RETURNING``.
        supports_merge_not_matched_by_source: ``MERGE ... WHEN NOT MATCHED BY SOURCE``.
        supports_uuid_v7: A ``db_default`` generating a time-ordered version 7 UUID (``UuidV7()``).
        supports_without_overlaps: A unique constraint or primary key whose last part is a range taking
            ``WITHOUT OVERLAPS``.
        supports_returning_old_new: ``RETURNING`` reads a row as it was before (``OLD``) and after (``NEW``)
            a write.
        supports_json_table: ``JSON_TABLE`` - rows read out of a JSON document.
        supports_enum_types: ``CREATE TYPE ... AS ENUM`` - a column of a type of labels.
        sorts_nulls_first: NULL sorts before every value in ascending order by default.
        divides_integers_exactly: ``/`` of two integers gives their exact quotient, a fraction - the result
            is read as a float; without, the database truncates it to an integer.
        computes_integer_statistics_exactly: The average, standard deviation and variance of integers are
            exact decimals the database computes - read as a ``Decimal``; without, the database computes them
            as floats.
        keeps_enum_columns: An ``IntEnumField``'s and a ``CharEnumField``'s column is of an enum type holding
            only the members. Without, the column holds any integer or text, and a ``CHECK``
            (``EnumMembersCondition``) keeps it to the members' values.
        computes_decimal_statistics_exactly: The average, standard deviation and variance of Decimals are
            exact decimals the database computes - read as a ``Decimal``; without, the database computes
            them as floats.
        reports_written_row_count: The database reports how many rows an ``UPDATE`` or a ``DELETE``
            changed. Without, ``update()``, ``delete()`` and ``bulk_update()`` return None, a write
            matching no row goes unnoticed, and a model with ``Meta.optimistic_lock_field`` is refused
            when bound.
        supports_insert_conflicts: ``INSERT ... ON CONFLICT`` - ``bulk_create()`` with ``ignore_conflicts``,
            ``update_fields`` or ``on_conflict``; without, those are refused.
        supports_conflict_constraint_names: ``ON CONFLICT ON CONSTRAINT name``.
        supports_conflict_where: An ``ON CONFLICT`` target takes a ``WHERE``.
        guarantees_returning_order: A multi-row ``INSERT ... RETURNING`` returns its rows in the
            order they were written.
        supports_copy: The bulk ``COPY`` protocol, or another bulk load of rows the client runs
            through ``copy()``.
        copies_bulk_inserts: ``bulk_create()`` loads its rows through ``copy()`` without being
            asked to (``use_copy=True``) - whenever it handles no conflict and reads no row back.
        supports_pool_status: The client reports what its pool of connections holds and has done
            (``DatabaseClient.get_pool_status()``) - a feature of the client, not of the database.
        supports_virtual_generated_columns: A generated column can be computed on read.
        matches_ordering_to_grouping_by_sql: An ordering term that is also grouped by has to be
            written exactly as in ``GROUP BY``.
        checks_foreign_keys_per_cascade_step: A ``NO ACTION`` foreign key is checked after every
            nested step of an ``ON DELETE CASCADE`` rather than once at the end of the statement -
            then even a single ``DELETE`` fails on a row guarded by an ``on_delete=PROTECT``
            relation that the same cascade removes the guarding row of later, unless the PROTECT
            constraints are deferred.
        checks_restrict_at_statement_end: A ``RESTRICT`` foreign key is checked at the end of the
            statement like ``NO ACTION``, rather than at once, before the statement's own cascade
            goes on.
        isolation_levels: The isolation levels a transaction runs at, weakest first.
        max_identifier_length: The most bytes a table, column, index, constraint or alias name may
            take before the database truncates or rejects it, None for no limit. hare keeps the
            names it generates within ``IDENTIFIER_LENGTH_LIMIT`` (63) bytes; a dialect with a lower
            limit is refused on registration.
        creates_tables_with_forward_references: A table is created with a ``FOREIGN KEY`` to a table
            not created yet - the reference is checked when a row is written - so a cycle of foreign
            keys is created table by table; without, a table of the cycle is created without the
            foreign keys to the tables after it, added once they exist.
        supports_partial_indexes: An index takes a ``WHERE`` condition.
        supports_deferrable_constraints: A constraint or constraint trigger can be ``DEFERRABLE``
            and deferred with ``SET CONSTRAINTS``.
        supports_index_key_order: An index key sorts ascending or descending.
        collates_in_ordering_only: A collation a user names is taken in an ``ORDER BY`` key alone, not
            in a selected expression - a ``DISTINCT`` selects the value and orders it collated.
        supports_index_nulls_order: An index key sets where NULLs sort.
        supports_concurrent_indexes: An index is built or dropped without blocking writes, outside
            a transaction.
        supports_not_valid_constraints: A constraint is added without checking the existing rows,
            which are checked later.
        supports_extensions: The database installs extensions.
        supports_collations: The database creates collations.
        truncates_values_on_type_change: Changing a column's type with an explicit cast silently
            cuts a too-long string or rounds an over-precise number instead of failing, so a
            narrowing change is checked against the table's data first.
        alters_indexed_columns: A column an index covers takes a change of its type or
            nullability. Without, the indexes covering it are dropped around the change and created
            again.
        binds_array_parameters: A list is bound as one array parameter (a ``RawSQL`` parameter
            such as ``= ANY(%s)``).
        supports_foreign_keys: The database enforces foreign keys. Without, a model's relations are
            declared ``db_constraint=False`` - a model declaring one of ``db_constraint=True``, a
            many-to-many relation's automatic through table included, is refused when bound - and hare
            runs their ``on_delete`` actions and ``PROTECT`` checks.
        supports_unique_constraints: The database enforces uniqueness. Without, a model declaring a
            ``unique=True`` field, a ``UniqueConstraint`` or an ``Index(unique=True)`` is refused when
            bound, a migration declaring one before its first statement, an automatic through table
            keeps no unique pair, and an upsert can target the primary key only.
        supports_views: A model's ``Meta.views`` - views created, replaced and dropped by migrations.
        supports_materialized_views: ``Meta.materialized_views`` - materialized views and their
            refresh, concurrent too.
        supports_dictionaries: ``Meta.dictionaries`` - rows of a table kept loaded for lookups by key.
        supports_database_functions: ``Meta.functions`` - functions stored in the database.
        supports_sequences: ``Meta.sequences`` - sequences and reading their next value.
        supports_row_level_security: ``Meta.row_level_security`` and ``Meta.policies``.
        supports_grants: ``Meta.grants`` - privileges granted to and revoked from roles.
        supports_text_search_configurations: Text search beyond matching a model's fields: text search
            configurations, ``SearchVector`` values, lexemes, rank weights by label, rank length
            normalization and ``SearchHeadline``'s fragment options.
        supports_full_text_index: A full-text index is a table of its own the database keeps in
            step with the indexed table (``FullTextIndex``).
        supports_vector_search: Vector columns compared by distance - a vector extension of the
            database.
        supports_tenant_schemas: A connection keeps the tables of each tenant in a schema of its own
            (``tenant_schema_template``) and reaches them through the schema search path.
        supports_spatial: Spatial lookups, functions and aggregates of ``hare.gis`` - a spatial
            extension of the database.
        supports_geography: A ``GeometryField(geography=True)`` measured on the ellipsoid.
        spatial_reference_ids: The SRIDs the database's spatial metadata knows - a geography is
            measured, and a spatial index registered, in one of them; None for no limit.
        supports_ordered_aggregates: An aggregate orders the rows it reads - ``ORDER BY`` inside its
            arguments (``MakeLine(..., order_by=...)``).
    """

    supports_transactions: bool = True
    supports_savepoints: bool = True
    supports_generated_keys: bool = True
    takes_keys_before_insert: bool = False
    table_alias_hides_name: bool = True
    supports_recursive_queries: bool = True
    supports_recursive_union_distinct: bool = True
    supports_row_updates: bool = True
    supports_row_deletes: bool = True
    supports_correlated_subqueries: bool = True
    rewrites_correlated_exists: bool = False
    supports_ordered_correlated_subqueries: bool = True
    orders_by_correlated_subqueries: bool = True
    supports_correlated_aggregates: bool = True
    can_rollback_ddl: bool = False
    supports_select_for_update: bool = True
    supports_select_for_no_key_update: bool = False
    supports_select_for_share: bool = False
    supports_select_for_key_share: bool = False
    supports_update_limit_order_by: bool = True
    supports_posix_regex: bool = False
    cuts_like_pattern_at_null_byte: bool = False
    supports_returning: bool = False
    supports_two_phase_commit: bool = False
    supports_listen_notify: bool = False
    inline_comments: bool = False
    supports_positional_rows: bool = False
    supports_streaming: bool = False
    streams_without_transaction: bool = False
    execute_many_scales_poorly: bool = False
    binds_written_parameters: bool = False
    max_bind_parameters: int = DEFAULT_MAX_BIND_PARAMETERS
    max_expression_depth: int | None = None
    max_bulk_update_rows: int | None = None
    cascade_depth_limit: int | None = None
    supports_nulls_distinct: bool = False
    explain_options: frozenset[str] = frozenset()
    supports_schemas: bool = True
    references_other_schemas: bool = True
    supports_distinct_on: bool = False
    supports_grouping_sets: bool = False
    supports_lateral: bool = False
    supports_table_sample: bool = False
    supports_asof_join: bool = False
    supports_array_join: bool = False
    orders_json_values_in_aggregates: bool = True
    reads_trailing_comment_of_insert: bool = True
    supports_merge: bool = False
    supports_merge_returning: bool = False
    supports_merge_not_matched_by_source: bool = False
    supports_uuid_v7: bool = False
    supports_without_overlaps: bool = False
    supports_returning_old_new: bool = False
    supports_json_table: bool = False
    supports_enum_types: bool = False
    sorts_nulls_first: bool = False
    divides_integers_exactly: bool = False
    computes_integer_statistics_exactly: bool = False
    computes_decimal_statistics_exactly: bool = True
    keeps_enum_columns: bool = False
    reports_written_row_count: bool = True
    supports_insert_conflicts: bool = True
    supports_conflict_constraint_names: bool = False
    supports_conflict_where: bool = False
    guarantees_returning_order: bool = True
    supports_copy: bool = False
    copies_bulk_inserts: bool = False
    supports_pool_status: bool = False
    supports_virtual_generated_columns: bool = True
    matches_ordering_to_grouping_by_sql: bool = False
    checks_foreign_keys_per_cascade_step: bool = False
    checks_restrict_at_statement_end: bool = False
    isolation_levels: tuple[IsolationLevel, ...] = tuple(IsolationLevel)
    max_identifier_length: int | None = None
    creates_tables_with_forward_references: bool = False
    supports_partial_indexes: bool = False
    supports_deferrable_constraints: bool = False
    supports_index_key_order: bool = True
    collates_in_ordering_only: bool = False
    supports_index_nulls_order: bool = True
    supports_concurrent_indexes: bool = False
    supports_not_valid_constraints: bool = False
    supports_extensions: bool = False
    supports_collations: bool = False
    truncates_values_on_type_change: bool = False
    alters_indexed_columns: bool = True
    binds_array_parameters: bool = False
    supports_foreign_keys: bool = True
    supports_unique_constraints: bool = True
    supports_views: bool = False
    supports_materialized_views: bool = False
    supports_dictionaries: bool = False
    supports_database_functions: bool = False
    supports_sequences: bool = False
    supports_row_level_security: bool = False
    supports_grants: bool = False
    supports_text_search_configurations: bool = False
    supports_full_text_index: bool = False
    supports_vector_search: bool = False
    supports_tenant_schemas: bool = False
    supports_spatial: bool = False
    supports_geography: bool = False
    spatial_reference_ids: frozenset[int] | None = None
    supports_ordered_aggregates: bool = False
    #: The number of these features in the key of a statement plan - a plan built for a connection of
    #: other features isn't reused under the same connection name. Never one number for two sets of
    #: features, even once the features are gone.
    plan_key: int = dataclasses.field(default=0, init=False, compare=False, repr=False)

    #: Numbers the features made.
    plan_keys: ClassVar[Iterator[int]] = itertools.count(1)
    #: The features ``replace()`` made, each set of features once - one plan key for the connections
    #: of the same features.
    replaced_features: ClassVar[Cache[Features]] = Cache(
        REPLACED_FEATURES_CACHE_SIZE, holds_sql=False, keyed_by_model=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_key", next(Features.plan_keys))

    def replace(self, **overrides: Any) -> Self:
        """A copy with the given features changed.

        Args:
            overrides: Feature values by name.

        Returns:
            The new features - the ones made before when they are equal.
        """
        replaced = dataclasses.replace(self, **overrides)
        kept = Features.replaced_features.get((replaced,))
        if kept is None:
            Features.replaced_features[(replaced,)] = kept = replaced
        return cast("Self", kept)
