from __future__ import annotations

#: The oldest PostgreSQL server hare runs on.
POSTGRESQL_MINIMUM_SERVER_VERSION = (14,)
#: The first PostgreSQL version whose unique constraints take ``NULLS [NOT] DISTINCT``.
POSTGRESQL_NULLS_DISTINCT_SERVER_VERSION = (15,)
#: The first PostgreSQL version whose partitioned tables take an exclusion constraint.
POSTGRESQL_PARTITIONED_EXCLUSION_SERVER_VERSION = (17,)
#: The first PostgreSQL version with ``MERGE``.
POSTGRESQL_MERGE_SERVER_VERSION = (15,)
#: The first PostgreSQL version whose ``MERGE`` takes ``RETURNING`` and ``WHEN NOT MATCHED BY SOURCE``.
POSTGRESQL_MERGE_RETURNING_SERVER_VERSION = (17,)
#: The first PostgreSQL version with ``JSON_TABLE``.
POSTGRESQL_JSON_TABLE_SERVER_VERSION = (17,)
#: The first PostgreSQL version with VIRTUAL generated columns.
POSTGRESQL_VIRTUAL_GENERATED_COLUMNS_SERVER_VERSION = (18,)
#: The first PostgreSQL version with ``uuidv7()``.
POSTGRESQL_UUID_V7_SERVER_VERSION = (18,)
#: The first PostgreSQL version whose unique constraints and primary keys take ``WITHOUT OVERLAPS``.
POSTGRESQL_WITHOUT_OVERLAPS_SERVER_VERSION = (18,)
#: The first PostgreSQL version whose ``RETURNING`` reads ``OLD`` and ``NEW``.
POSTGRESQL_RETURNING_OLD_NEW_SERVER_VERSION = (18,)
#: The first PostgreSQL version of each ``EXPLAIN`` option an older server lacks.
POSTGRESQL_EXPLAIN_OPTION_SERVER_VERSIONS = {"GENERIC_PLAN": (16,), "MEMORY": (17,), "SERIALIZE": (17,)}
