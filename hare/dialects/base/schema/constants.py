from __future__ import annotations

#: How many rows an AlterField reads and writes at once when it rewrites the stored values of a
#: field whose change the column itself doesn't make.
STORED_VALUE_REWRITE_BATCH_SIZE = 500

#: DDL templates of the schema editor's table, key and constraint clauses. Identifier placeholders
#: have no quotes - the caller passes quoted values.
CHECK_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {name} CHECK ({check})"

UNIQUE_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {index_name} UNIQUE{nulls} ({fields}){include}"

PRIMARY_KEY_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {index_name} PRIMARY KEY ({fields})"

GENERATED_PK_TEMPLATE = "{field_name} {generated_sql}{comment}"

FOREIGN_KEY_TEMPLATE = " REFERENCES {table} ({field}) ON DELETE {on_delete}{comment}"

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
