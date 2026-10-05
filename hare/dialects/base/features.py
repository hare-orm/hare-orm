from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

from hare.dialects.constants import DEFAULT_MAX_BIND_PARAMETERS
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
        returns_rows_by_reading: The rows a write returns without ``RETURNING`` - read by their keys:
            an update's after it, a delete's before it, the values the database gives inserted rows after
            the insert. Rows changed in between the read and the write are read as they are then.
        checks_constraints_before_write: hare checks the uniqueness and the relations a model declares
            before its rows are written - the database keeps neither: a key, a ``unique=True``
            field, a ``UniqueConstraint``, an ``Index(unique=True)``, a relation of ``db_constraint=True``.
        takes_keys_before_insert: The database hands out the generated keys of the rows before they are
            written - a series of numbers: hare takes them and writes
            each row with its key.
        supports_generated_keys: The database generates a key for a row written without one (an
            identity or auto-increment column) and tells it back. Without, a model whose primary key
            the database generates can't be written on the connection.
        supports_row_updates: ``UPDATE`` changes stored rows. Without, ``save()`` of a stored row,
            ``QuerySet.update()``, ``bulk_update()`` and every write that changes a row in place are
            refused before any SQL is sent.
        supports_row_deletes: ``DELETE`` removes stored rows. Without, ``delete()`` and every write
            that removes rows are refused before any SQL is sent.
        rewrites_correlated_exists: An ``EXISTS`` correlated by equalities of columns is written as a
            membership test where correlated subqueries run too - the database computes it right only
            so (its own correlated ``EXISTS`` misses rows).
        orders_by_correlated_subqueries: A correlated subquery runs in ``ORDER BY``, and selected beside
            a ``WHERE`` - without, such a query selects its rows in a derived table of no ``WHERE``, its
            conditions and sort keys hidden columns of it, and filters, sorts and slices the rows of that
            (the database runs neither); a mutation runs no correlated subquery there.
        supports_ordered_correlated_subqueries: A correlated subquery orders and limits its own rows
            (``Subquery(... .order_by(...)[:1])``).
        supports_correlated_subqueries: A subquery reads the columns of the query around it. Without,
            a filter across a to-many relation is written as a key ``IN (SELECT ...)``, and a query
            needing a correlated subquery (``Exists`` or ``Subquery`` with ``OuterReference``) is refused
            before any SQL is sent.
        can_rollback_ddl: DDL runs inside a transaction and rolls back with it.
        supports_select_for_update: ``SELECT ... FOR UPDATE``.
        locks_rows_by_key: ``select_for_update()`` locks the rows by their keys outside SQL, through the
            transaction (``DatabaseClient.take_row_locks()``): the keys of the rows are read first, then
            locked, then the rows are read by them.
        supports_select_for_no_key_update: ``SELECT ... FOR NO KEY UPDATE``.
        supports_select_for_share: ``SELECT ... FOR SHARE``.
        supports_select_for_key_share: ``SELECT ... FOR KEY SHARE``.
        supports_update_limit_order_by: ``UPDATE``/``DELETE`` with ``ORDER BY`` and ``LIMIT``.
        supports_posix_regex: POSIX regular expression lookups.
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
        cascade_depth_limit: The recursion depth a native ``ON DELETE CASCADE`` stops at, None for
            none. The client then raises ``CascadeDepthLimitError`` (or a subclass) when it stops,
            and ``delete()`` of a model with a cascade cycle retries such a DELETE with a
            Python-side cascade walk, deleting fewer levels than this at a time.
        supports_nulls_distinct: A unique constraint takes ``NULLS [NOT] DISTINCT``.
        supports_partitioned_exclusion_constraints: A partitioned table takes an exclusion constraint.
        supports_unhex: ``unhex()`` decodes hex text to bytes.
        supports_drop_column: ``ALTER TABLE ... DROP COLUMN`` drops a plain column in place - without,
            a removed field's table is rebuilt.
        explain_options: The options ``EXPLAIN`` takes, upper-case.
        supports_schemas: A table can be qualified by a schema; without, a schema-qualified model's
            table is used unqualified.
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
        rebuilds_projections: A table with projections takes a lightweight ``DELETE`` and merges
            dropping rows - by table settings rebuilding its projections; a server
            without them deletes the rows of such a table by a mutation.
        supports_lightweight_update: A row is changed by an ``UPDATE`` writing its new values beside
            it, read in place of the old ones - not by a mutation rewriting its part.
        supports_variant_types: A column holds values of several types (``Variant``, ``Dynamic``);
            without, such a column raises ``UnSupportedError`` before its DDL.
        supports_json_type: A JSON value is stored by a type of its own holding its paths typed
            (a ``JSON`` type), not as its text - an object alone, its NULL values and empty
            objects dropped.
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
        enforces_numeric_ranges: Integer and decimal columns reject values out of their range or
            scale.
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
        supports_adding_constraints: ``ALTER TABLE ... ADD CONSTRAINT``; without, a new table's
            CHECK constraints go into ``CREATE TABLE`` and its unique constraints become unique
            indexes, and a later change rebuilds the table.
        supports_partial_indexes: An index takes a ``WHERE`` condition.
        supports_exclusion_constraints: Exclusion constraints.
        supports_deferrable_constraints: A constraint or constraint trigger can be ``DEFERRABLE``
            and deferred with ``SET CONSTRAINTS``.
        supports_index_nulls_order: An index key sets where NULLs sort.
        supports_concurrent_indexes: An index is built or dropped without blocking writes, outside
            a transaction.
        supports_not_valid_constraints: A constraint is added without checking the existing rows,
            which are checked later.
        supports_statement_triggers: A trigger fires ``FOR EACH STATEMENT``.
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
        supports_foreign_keys: The database enforces foreign keys. Without, a relation's table gets
            no FOREIGN KEY constraint, and hare runs every ``on_delete`` action and ``PROTECT``
            check itself, as for a ``db_constraint=False`` relation.
        supports_unique_constraints: The database enforces uniqueness. Without, a unique field, a
            ``UniqueConstraint`` and a unique through table get no constraint, a unique ``Index``
            is a plain index, and an upsert can target the primary key only.
        supports_views: A model's ``Meta.views`` - views created, replaced and dropped by migrations.
        supports_materialized_views: ``Meta.materialized_views`` - materialized views and their
            refresh, concurrent too.
        supports_refreshable_materialized_views: A materialized view is refreshed by the database
            on a schedule of its own.
        supports_dictionaries: ``Meta.dictionaries`` - rows of a table kept loaded for lookups by key.
        supports_database_functions: ``Meta.functions`` - functions stored in the database.
        supports_sequences: ``Meta.sequences`` - sequences and reading their next value.
        supports_row_level_security: ``Meta.row_level_security`` and ``Meta.policies``.
        supports_grants: ``Meta.grants`` - privileges granted to and revoked from roles.
        supports_strict_tables: A table is created ``STRICT`` - each column holds values of its
            declared type only.
        supports_text_search_configurations: Text search beyond matching a model's fields: text search
            configurations, ``SearchVector`` values, lexemes, rank weights by label, rank length
            normalization and ``SearchHeadline``'s fragment options - PostgreSQL's tsvector search.
        supports_full_text_index: A full-text index is a table of its own the database keeps in
            step with the indexed table (SQLite's FTS5).
        supports_vector_search: Vector columns compared by distance - pgvector on PostgreSQL, the
            sqlite-vec extension on SQLite.
        supports_tenant_schemas: A connection keeps the tables of each tenant in a schema of its own
            (``tenant_schema_template``) and reaches them through the schema search path.
        supports_spatial: Spatial lookups, functions and aggregates of ``hare.gis`` - PostGIS on
            PostgreSQL, the SpatiaLite extension on SQLite.
        supports_geography: A ``GeometryField(geography=True)`` measured on the ellipsoid - PostGIS's
            geography, SpatiaLite with its spatial metadata on SQLite.
        supports_spatial_index: A spatial index of SpatiaLite (``SpatialiteIndex``) - its R*Tree
            registered in the spatial metadata.
        spatial_reference_ids: The SRIDs the database's spatial metadata knows - a geography is
            measured, and a SpatiaLite spatial index registered, in one of them; None for no limit.
        supports_ordered_aggregates: An aggregate orders the rows it reads - ``ORDER BY`` inside its
            arguments (``MakeLine(..., order_by=...)``).
    """

    supports_transactions: bool = True
    supports_savepoints: bool = True
    supports_generated_keys: bool = True
    takes_keys_before_insert: bool = False
    checks_constraints_before_write: bool = False
    returns_rows_by_reading: bool = False
    supports_row_updates: bool = True
    supports_row_deletes: bool = True
    supports_correlated_subqueries: bool = True
    rewrites_correlated_exists: bool = False
    supports_ordered_correlated_subqueries: bool = True
    orders_by_correlated_subqueries: bool = True
    can_rollback_ddl: bool = False
    supports_select_for_update: bool = True
    locks_rows_by_key: bool = False
    supports_select_for_no_key_update: bool = False
    supports_select_for_share: bool = False
    supports_select_for_key_share: bool = False
    supports_update_limit_order_by: bool = True
    supports_posix_regex: bool = False
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
    cascade_depth_limit: int | None = None
    supports_nulls_distinct: bool = False
    supports_partitioned_exclusion_constraints: bool = False
    supports_unhex: bool = False
    supports_drop_column: bool = True
    explain_options: frozenset[str] = frozenset()
    supports_schemas: bool = True
    supports_distinct_on: bool = False
    supports_grouping_sets: bool = False
    supports_lateral: bool = False
    supports_table_sample: bool = False
    supports_asof_join: bool = False
    supports_array_join: bool = False
    rebuilds_projections: bool = False
    supports_lightweight_update: bool = False
    supports_json_type: bool = False
    supports_variant_types: bool = False
    supports_merge: bool = False
    supports_merge_returning: bool = False
    supports_merge_not_matched_by_source: bool = False
    supports_uuid_v7: bool = False
    supports_without_overlaps: bool = False
    supports_returning_old_new: bool = False
    supports_json_table: bool = False
    supports_enum_types: bool = False
    sorts_nulls_first: bool = False
    enforces_numeric_ranges: bool = True
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
    alters_indexed_columns: bool = True
    binds_array_parameters: bool = False
    supports_foreign_keys: bool = True
    supports_unique_constraints: bool = True
    supports_views: bool = False
    supports_materialized_views: bool = False
    supports_refreshable_materialized_views: bool = False
    supports_dictionaries: bool = False
    supports_database_functions: bool = False
    supports_sequences: bool = False
    supports_row_level_security: bool = False
    supports_grants: bool = False
    supports_strict_tables: bool = False
    supports_text_search_configurations: bool = False
    supports_full_text_index: bool = False
    supports_vector_search: bool = False
    supports_tenant_schemas: bool = False
    supports_spatial: bool = False
    supports_geography: bool = False
    supports_spatial_index: bool = False
    spatial_reference_ids: frozenset[int] | None = None
    supports_ordered_aggregates: bool = False

    def replace(self, **overrides: Any) -> Features:
        """A copy with the given features changed.

        Args:
            overrides: Feature values by name.

        Returns:
            The new features.
        """
        return dataclasses.replace(self, **overrides)
