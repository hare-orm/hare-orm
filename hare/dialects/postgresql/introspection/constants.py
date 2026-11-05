from hare.dialects.postgresql.constants import (
    POSTGRESQL_COLUMNS_SQL,
    POSTGRESQL_CONSTRAINTS_SQL,
    POSTGRESQL_FOREIGN_KEYS_SQL,
    POSTGRESQL_INDEXES_SQL,
    POSTGRESQL_INSPECTED_COLUMN_PRIVILEGES_SQL,
    POSTGRESQL_INSPECTED_POLICIES_SQL,
    POSTGRESQL_INSPECTED_SEQUENCES_SQL,
    POSTGRESQL_INSPECTED_TABLE_PRIVILEGES_SQL,
    POSTGRESQL_PARTITIONS_SQL,
    POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL,
    POSTGRESQL_TRIGGERS_SQL,
)

POSTGRESQL_TABLE_CATALOG_QUERIES = {
    "primary_keys": POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL,
    "indexes": POSTGRESQL_INDEXES_SQL,
    "columns": POSTGRESQL_COLUMNS_SQL,
    "foreign_keys": POSTGRESQL_FOREIGN_KEYS_SQL,
    "triggers": POSTGRESQL_TRIGGERS_SQL,
    "constraints": POSTGRESQL_CONSTRAINTS_SQL,
    "partitions": POSTGRESQL_PARTITIONS_SQL,
    "policies": POSTGRESQL_INSPECTED_POLICIES_SQL,
    "table_privileges": POSTGRESQL_INSPECTED_TABLE_PRIVILEGES_SQL,
    "column_privileges": POSTGRESQL_INSPECTED_COLUMN_PRIVILEGES_SQL,
    "sequences": POSTGRESQL_INSPECTED_SEQUENCES_SQL,
}

POSTGRESQL_CATALOG_REFERENCE_CASTS = frozenset(
    {
        "regclass",
        "regtype",
        "regprocedure",
        "regoperator",
        "regproc",
        "regoper",
        "regcollation",
        "regconfig",
        "regdictionary",
    }
)

POSTGRESQL_CATALOG_COLUMN_NONCOMPARABLE_MEMBERS = frozenset({"table_name", "enum_labels"})
POSTGRESQL_CATALOG_FOREIGN_KEY_NONCOMPARABLE_MEMBERS = frozenset(
    {
        "table_name",
        "conname",
        "target_primary_key_columns",
    }
)
