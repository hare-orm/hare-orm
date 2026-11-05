from __future__ import annotations

SCHEMA_CLEANUP_FAILED = "Schema change and cleanup both failed"
SCHEMA_FAILURE_CLEANUP_PREVIEW = "-- On failure or cancellation only:"
COLUMN_CONVERSION_CHECK_PREVIEW = "-- Check stored values of {table}.{column} before changing its type."
COLUMN_NULL_CHECK_PREVIEW = "-- Check that {table}.{column} contains no NULL values before setting NOT NULL."
STORED_VALUE_REWRITE_PREVIEW = "-- {description}: rewrite every non-NULL stored value in Python."
RELATION_TARGET_CHECK_PREVIEW = "-- Check that {relation} stores no keys before changing its target."
RELATION_TARGET_CHANGE_MESSAGE = (
    "Cannot repoint {relation} from {old} to {new} - {count} row(s) store values of the old target column, "
    "which would silently refer to the wrong rows (or none) afterwards. Migrate the data by hand instead: "
    "add a new relation field targeting the new column, fill it with RunPython/RunSQL, then remove the old field."
)
COLUMN_DEFAULT_BACKFILL_PREVIEW = (
    "-- Backfill NULL rows in {table!r}.{column!r}: evaluate the runtime default once during execution."
)
COLUMN_BATCH_BACKFILL_PREVIEW = (
    "-- Evaluate the backfill value once during execution; repeat only while changed rows are reported."
    "\n{sql}  -- params: runtime backfill value, twice"
)
TABLE_COPY_DEFAULT_PREVIEW = "-- Copy rows from {source} to {target}, evaluating clock defaults once during execution."
TABLE_COPY_SQL = "INSERT INTO {target} ({columns})\n                SELECT {values}\n                FROM {source}"  # nosec B608
THROUGH_ROWS_COPY_SQL = "INSERT INTO {target} ({columns}) SELECT {distinct}{values} FROM {source}"  # nosec B608
THROUGH_ROWS_COPY_PREVIEW = (
    "-- Copy relation rows from {source} to {target}, evaluating defaults and generated keys during execution."
)

#: How many rows an AlterField reads and writes at once when it rewrites the stored values of a
#: field whose change the column itself doesn't make.
STORED_VALUE_REWRITE_BATCH_SIZE = 500

#: DDL templates of the schema editor's table, key and constraint clauses. Identifier placeholders
#: have no quotes - the caller passes quoted values.
CHECK_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {name} CHECK ({check})"

UNIQUE_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {index_name} UNIQUE{nulls} ({fields}){include}"

PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {index_name} PRIMARY KEY ({fields})"

GENERATED_PK_TEMPLATE = "{field_name} {generated_sql}{primary_key}{comment}"

FOREIGN_KEY_TEMPLATE = " CONSTRAINT {name} REFERENCES {table} ({field}) ON DELETE {on_delete}{comment}"

#: Table-level composite FK constraint - a composite-target FK/O2O renders N plain shadow
#: columns (no inline REFERENCES, unlike FOREIGN_KEY_TEMPLATE's single-column case) plus one of these,
#: applied after CREATE TABLE the same way Meta.constraints already are.
FOREIGN_KEY_CONSTRAINT_CREATE_TEMPLATE = (
    "CONSTRAINT {name} FOREIGN KEY ({fields}) REFERENCES {table} ({to_fields}) ON DELETE {on_delete}"
)

#: A backfill of a table without a primary key on a database naming no row otherwise.
BACKFILL_WITHOUT_ROW_IDENTITY_MESSAGE = (
    "Can't backfill {model}.{field} in batches - the model has no primary key and the {dialect} "
    "database has no other way to pick a batch's rows. Fill the column in a RunPython step instead."
)
