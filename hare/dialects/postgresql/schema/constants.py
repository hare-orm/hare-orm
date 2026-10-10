from __future__ import annotations

import re

from hare.ddl.enums import FunctionVolatility, GrantTarget, PolicyCommand, Privilege
from hare.lazy_loading.lazy_pattern import LazyPattern

#: Lookup of the deferrable foreign key constraint behind one ``on_delete=PROTECT`` field: $1 is the
#: quoted (optionally schema-qualified) referencing table, $2 the referenced table, $3 the ordered
#: FK column names. Returns the constraint's schema-qualified, quoted name.
POSTGRESQL_DEFERRABLE_CONSTRAINT_NAME_QUERY = (
    "SELECT format('%I.%I', nsp.nspname, con.conname) AS name "
    "FROM pg_constraint con "
    "JOIN pg_namespace nsp ON nsp.oid = con.connamespace "
    "WHERE con.contype = 'f' "
    "AND con.condeferrable "
    "AND con.conrelid = to_regclass($1) "
    "AND con.confrelid = to_regclass($2) "
    "AND ARRAY("
    "  SELECT att.attname::text"
    "  FROM unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, ord)"
    "  JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = key_column.attnum"
    "  ORDER BY key_column.ord"
    ") = $3::text[]"
)

#: The constraints of a table ($1) in a schema ($2; the current schema when NULL), by name.
POSTGRESQL_TABLE_CONSTRAINT_NAMES_SQL = (
    "SELECT con.conname AS name FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class rel ON rel.oid = con.conrelid "
    "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace "
    "WHERE rel.relname = $1 AND nsp.nspname = COALESCE($2, current_schema())"
)

#: The sequences a table ($1) in a schema ($2; the current schema when NULL) owns - its serial
#: columns' - by name.
POSTGRESQL_TABLE_SEQUENCE_NAMES_SQL = (
    "SELECT seq.relname AS name FROM pg_catalog.pg_class seq "
    "JOIN pg_catalog.pg_sequence sequence_options ON sequence_options.seqrelid = seq.oid "
    "JOIN pg_catalog.pg_depend dep ON dep.objid = seq.oid AND dep.deptype IN ('a', 'i') "
    "JOIN pg_catalog.pg_class rel ON rel.oid = dep.refobjid "
    "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace "
    "WHERE rel.relname = $1 AND nsp.nspname = COALESCE($2, current_schema())"
)

#: The foreign keys of other tables referencing a table ($1) in a schema ($2; the current schema
#: when NULL): the referencing table as written in SQL, the constraint's name and its definition -
#: without the copies a partitioned referencing table keeps for its partitions.
POSTGRESQL_INCOMING_FOREIGN_KEYS_SQL = (
    "SELECT con.conrelid::regclass::text AS table_sql, con.conname AS name, "
    "pg_catalog.pg_get_constraintdef(con.oid) AS definition "
    "FROM pg_catalog.pg_constraint con "
    "JOIN pg_catalog.pg_class rel ON rel.oid = con.confrelid "
    "JOIN pg_catalog.pg_namespace nsp ON nsp.oid = rel.relnamespace "
    "WHERE con.contype = 'f' AND con.conparentid = 0 AND con.conrelid <> con.confrelid "
    "AND rel.relname = $1 AND nsp.nspname = COALESCE($2, current_schema()) "
    "ORDER BY 1, 2"
)

#: Moves the sequence of a generated key column past the highest key a table holds: the table as
#: an SQL string literal, the column as one, and both as identifiers.
POSTGRESQL_RESYNC_SEQUENCE_TEMPLATE = (
    "SELECT setval(pg_catalog.pg_get_serial_sequence({table_literal}, {column_literal}), "
    "COALESCE((SELECT MAX({column}) FROM {table}), 0) + 1, false)"
)

#: The suffix of the table a rebuild keeps a table's rows in while the table is created anew.
POSTGRESQL_TABLE_COPY_SUFFIX = "__copy"

#: Creates one partition of a partitioned table.
POSTGRESQL_PARTITION_CREATE_TEMPLATE = "CREATE TABLE {exists}{partition} PARTITION OF {table} {bound}{storage};"

#: The advisory lock key ``migrate`` holds for its whole run - "hare" read as a 32-bit integer.
POSTGRESQL_MIGRATION_LOCK_KEY = 0x68617265

#: Most tables one query asks "does it hold a row" when a test database is emptied.
POSTGRESQL_CLEAR_TABLES_PROBE_SIZE = 200

#: The PostgreSQL extension giving GiST operator classes to plain scalar types - an
#: ``ExclusionConstraint`` using GiST needs it for a scalar column (``("team", "=")``).
BTREE_GIST_EXTENSION = "btree_gist"

#: PostgreSQL column types ``btree_gist`` gives a GiST operator class, lower-cased and without a
#: length/precision suffix.
BTREE_GIST_SQL_TYPES = frozenset(
    {
        "smallint",
        "int",
        "integer",
        "int2",
        "int4",
        "int8",
        "bigint",
        "serial",
        "bigserial",
        "real",
        "float4",
        "float8",
        "double precision",
        "numeric",
        "decimal",
        "money",
        "oid",
        "char",
        "character",
        "varchar",
        "character varying",
        "text",
        "bytea",
        "bit",
        "varbit",
        "bit varying",
        "bool",
        "boolean",
        "date",
        "time",
        "timetz",
        "time with time zone",
        "time without time zone",
        "timestamp",
        "timestamptz",
        "timestamp with time zone",
        "timestamp without time zone",
        "interval",
        "uuid",
        "inet",
        "cidr",
        "macaddr",
        "macaddr8",
    }
)

#: Moves a table (``{table}``, as a string literal) to the database's default tablespace - named
#: at run time, since ``ALTER TABLE ... SET TABLESPACE`` takes no DEFAULT and the database's
#: default isn't always ``pg_default``.
POSTGRESQL_SET_DEFAULT_TABLESPACE_SQL = """DO $hare$ BEGIN
EXECUTE format('ALTER TABLE %s SET TABLESPACE %I', {table}, (
    SELECT dts.spcname FROM pg_database db JOIN pg_tablespace dts ON dts.oid = db.dattablespace
    WHERE db.datname = current_database()
));
END $hare$"""

#: A column type as hare writes it: its name, and its size arguments in parentheses if any.
POSTGRES_COLUMN_TYPE_RE = LazyPattern(
    r"^\s*(?P<name>[a-z][a-z ]*?)\s*(?:\((?P<arguments>[^)]*)\))?\s*$", re.IGNORECASE
)

#: The names of the variable-length text types - one type to PostgreSQL.
POSTGRES_VARCHAR_TYPE_NAMES = frozenset({"varchar", "character varying"})

#: The names of the exact numeric type.
POSTGRES_NUMERIC_TYPE_NAMES = frozenset({"numeric", "decimal"})

#: Column type changes PostgreSQL makes without rewriting the table, whatever the sizes:
#: (old type name, new type name).
POSTGRES_BINARY_COERCIBLE_TYPE_CHANGES = frozenset(
    {
        ("varchar", "text"),
        ("character varying", "text"),
        ("text", "varchar"),
        ("text", "character varying"),
        ("cidr", "inet"),
    }
)

#: An ``EXCLUDE USING`` constraint of a CREATE TABLE.
POSTGRESQL_EXCLUSION_CONSTRAINT_CREATE_TEMPLATE = "CONSTRAINT {name} EXCLUDE USING {using} ({expressions}){where}"

#: The start of a CREATE [UNIQUE] INDEX statement, where CONCURRENTLY goes.
POSTGRESQL_CONCURRENT_INDEX_CREATE_PATTERN = re.compile(r"^(CREATE (?:UNIQUE )?INDEX )")

#: Moves a table into the connection's current schema - SET SCHEMA takes no expression.
POSTGRESQL_MOVE_TABLE_TO_CURRENT_SCHEMA_TEMPLATE = (
    "DO $hare_move_table$ BEGIN EXECUTE format('ALTER TABLE %s SET SCHEMA %I', "
    "{table_literal}, current_schema()); END $hare_move_table$;"
)

POSTGRESQL_TABLE_COMMENT_TEMPLATE = "COMMENT ON TABLE {table} IS {comment};"

POSTGRESQL_COLUMN_COMMENT_TEMPLATE = "COMMENT ON COLUMN {table}.{column} IS {comment};"

#: PostgreSQL has no inline column comment (comments go through COMMENT ON COLUMN), so a generated
#: primary key's column has no {comment} placeholder.
POSTGRESQL_GENERATED_PK_TEMPLATE = "{field_name} {generated_sql}"

#: Creates an ENUM type unless it exists - PostgreSQL has no ``CREATE TYPE IF NOT EXISTS``.
POSTGRESQL_SAFE_CREATE_TYPE_SQL = (
    "DO $hare$ BEGIN {create_sql}; EXCEPTION WHEN duplicate_object THEN NULL; END $hare$;"
)

#: The suffix of the name an ENUM type is renamed to while a new one of its name replaces it.
POSTGRESQL_REPLACED_ENUM_TYPE_SUFFIX = "__hare_old"

#: Replaces an ENUM type by one of other labels: renames it, creates the new one, converts every
#: column (and array column) of the old type through text - its default too - and drops the old.
POSTGRESQL_REPLACE_ENUM_TYPE_SQL = """DO $hare$
DECLARE
    column_row record;
BEGIN
    ALTER TYPE {type_sql} RENAME TO {old_type_sql};
    CREATE TYPE {type_sql} AS ENUM ({labels_sql});
    FOR column_row IN
        SELECT namespace.nspname AS schema_name, class.relname AS table_name, attribute.attname AS column_name,
            attribute.atttypid <> {old_type_literal}::regtype AS is_array,
            pg_get_expr(default_value.adbin, default_value.adrelid) AS default_sql
        FROM pg_attribute attribute
        JOIN pg_class class ON class.oid = attribute.attrelid
        JOIN pg_namespace namespace ON namespace.oid = class.relnamespace
        LEFT JOIN pg_attrdef default_value
            ON default_value.adrelid = attribute.attrelid AND default_value.adnum = attribute.attnum
        WHERE attribute.attnum > 0 AND NOT attribute.attisdropped AND class.relkind IN ('r', 'p')
            AND attribute.atttypid IN (
                {old_type_literal}::regtype,
                (SELECT typarray FROM pg_type WHERE oid = {old_type_literal}::regtype)
            )
    LOOP
        IF column_row.default_sql IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I DROP DEFAULT',
                column_row.schema_name, column_row.table_name, column_row.column_name);
        END IF;
        EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I TYPE %s USING %I::text%s::%s',
            column_row.schema_name, column_row.table_name, column_row.column_name,
            {type_literal} || CASE WHEN column_row.is_array THEN '[]' ELSE '' END,
            column_row.column_name, CASE WHEN column_row.is_array THEN '[]' ELSE '' END,
            {type_literal} || CASE WHEN column_row.is_array THEN '[]' ELSE '' END);
        IF column_row.default_sql IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN %I SET DEFAULT %s',
                column_row.schema_name, column_row.table_name, column_row.column_name,
                regexp_replace(column_row.default_sql, {old_type_pattern_literal}, '::' || {type_literal}, 'g'));
        END IF;
    END LOOP;
    DROP TYPE {old_type_sql};
END $hare$;"""

#: The language of a DatabaseFunction declared without one.
POSTGRESQL_DEFAULT_FUNCTION_LANGUAGE = "plpgsql"

#: The dollar quote around a DatabaseFunction's body - a body holding it is refused.
POSTGRESQL_FUNCTION_BODY_QUOTE = "$hare_function$"

#: The keyword of each function volatility.
POSTGRESQL_FUNCTION_VOLATILITY_KEYWORDS = {
    FunctionVolatility.VOLATILE: "VOLATILE",
    FunctionVolatility.STABLE: "STABLE",
    FunctionVolatility.IMMUTABLE: "IMMUTABLE",
}

#: The keyword of each policy command.
POSTGRESQL_POLICY_COMMAND_KEYWORDS = {
    PolicyCommand.ALL: "ALL",
    PolicyCommand.SELECT: "SELECT",
    PolicyCommand.INSERT: "INSERT",
    PolicyCommand.UPDATE: "UPDATE",
    PolicyCommand.DELETE: "DELETE",
}

#: The keyword of each privilege.
POSTGRESQL_PRIVILEGE_KEYWORDS = {
    Privilege.SELECT: "SELECT",
    Privilege.INSERT: "INSERT",
    Privilege.UPDATE: "UPDATE",
    Privilege.DELETE: "DELETE",
    Privilege.TRUNCATE: "TRUNCATE",
    Privilege.REFERENCES: "REFERENCES",
    Privilege.TRIGGER: "TRIGGER",
    Privilege.USAGE: "USAGE",
    Privilege.EXECUTE: "EXECUTE",
    Privilege.ALL: "ALL PRIVILEGES",
}

#: The object type a GRANT names before each type of object.
POSTGRESQL_GRANT_OBJECT_KEYWORDS = {
    GrantTarget.TABLE: "TABLE",
    GrantTarget.VIEW: "TABLE",
    GrantTarget.MATERIALIZED_VIEW: "TABLE",
    GrantTarget.SEQUENCE: "SEQUENCE",
    GrantTarget.FUNCTION: "FUNCTION",
}

#: The role names a policy or a grant writes as keywords - every other role name is quoted.
POSTGRESQL_ROLE_KEYWORDS = frozenset({"PUBLIC", "CURRENT_USER", "CURRENT_ROLE", "SESSION_USER"})

#: The suffix of the unique index of a materialized view's unique_columns.
POSTGRESQL_MATERIALIZED_VIEW_UNIQUE_INDEX_SUFFIX = "_unique"

POSTGRESQL_VIEW_CREATE_TEMPLATE = "CREATE {or_replace}VIEW {view} AS\n{query};"

POSTGRESQL_VIEW_DROP_TEMPLATE = "DROP VIEW {view};"

POSTGRESQL_VIEW_RENAME_TEMPLATE = "ALTER VIEW {view} RENAME TO {new_name};"

POSTGRESQL_MATERIALIZED_VIEW_CREATE_TEMPLATE = (
    "CREATE MATERIALIZED VIEW {if_not_exists}{view} AS\n{query}\nWITH {data};"
)

POSTGRESQL_MATERIALIZED_VIEW_UNIQUE_INDEX_TEMPLATE = (
    "CREATE UNIQUE INDEX {if_not_exists}{index_name} ON {view} ({columns});"
)

POSTGRESQL_MATERIALIZED_VIEW_DROP_TEMPLATE = "DROP MATERIALIZED VIEW {view};"

POSTGRESQL_MATERIALIZED_VIEW_DROP_IF_EXISTS_TEMPLATE = "DROP MATERIALIZED VIEW IF EXISTS {view};"

POSTGRESQL_MATERIALIZED_VIEW_RENAME_TEMPLATE = "ALTER MATERIALIZED VIEW {view} RENAME TO {new_name};"

POSTGRESQL_MATERIALIZED_VIEW_REFRESH_TEMPLATE = "REFRESH MATERIALIZED VIEW {concurrently}{view};"

POSTGRESQL_VIEW_DROP_IF_EXISTS_TEMPLATE = "DROP VIEW IF EXISTS {view};"

POSTGRESQL_INDEX_RENAME_TEMPLATE = "ALTER INDEX {index_name} RENAME TO {new_name};"

POSTGRESQL_FUNCTION_CREATE_TEMPLATE = (
    "CREATE {or_replace}FUNCTION {function}({arguments}) RETURNS {returns}\n"
    "LANGUAGE {language} {volatility}{security}\n"
    "AS {quote}\n{body}\n{quote};"
)

POSTGRESQL_FUNCTION_DROP_TEMPLATE = "DROP FUNCTION {function}({arguments});"

POSTGRESQL_FUNCTION_DROP_IF_EXISTS_TEMPLATE = "DROP FUNCTION IF EXISTS {function}({arguments});"

POSTGRESQL_FUNCTION_RENAME_TEMPLATE = "ALTER FUNCTION {function}({arguments}) RENAME TO {new_name};"

POSTGRESQL_SEQUENCE_CREATE_TEMPLATE = "CREATE SEQUENCE {if_not_exists}{sequence}{options};"

POSTGRESQL_SEQUENCE_ALTER_TEMPLATE = "ALTER SEQUENCE {sequence}{options};"

POSTGRESQL_SEQUENCE_OWNER_TEMPLATE = "ALTER SEQUENCE {sequence} OWNED BY {owner};"

POSTGRESQL_SEQUENCE_DROP_TEMPLATE = "DROP SEQUENCE {sequence};"

POSTGRESQL_SEQUENCE_DROP_IF_EXISTS_TEMPLATE = "DROP SEQUENCE IF EXISTS {sequence};"

POSTGRESQL_SEQUENCE_RENAME_TEMPLATE = "ALTER SEQUENCE {sequence} RENAME TO {new_name};"

#: Takes the next number of a sequence, named by a text literal.
POSTGRESQL_SEQUENCE_NEXT_VALUE_TEMPLATE = "SELECT nextval({sequence_literal}) AS {column}"

#: The column the next number of a sequence is read as.
POSTGRESQL_SEQUENCE_NEXT_VALUE_COLUMN = "next_value"

#: What a sequence has without an owner column.
POSTGRESQL_SEQUENCE_NO_OWNER_SQL = "NONE"

POSTGRESQL_ROW_LEVEL_SECURITY_TEMPLATE = "ALTER TABLE {table} {action} ROW LEVEL SECURITY;"

#: The predicate of a ``TenantCondition`` policy: every row under ``Tenancy.ALL``, else a row whose
#: tenant column is one of the transaction's tenants - none without a tenant.
POSTGRESQL_TENANT_CONDITION_TEMPLATE = (
    "CASE current_setting('{setting}', true) WHEN '{all_tenants}' THEN true ELSE {column} = ANY "
    "(ARRAY(SELECT jsonb_array_elements_text(NULLIF(current_setting('{setting}', true), '')::jsonb))"
    "::{column_type}[]) END"
)

POSTGRESQL_POLICY_CREATE_TEMPLATE = (
    "CREATE POLICY {policy} ON {table} AS {policy_type} FOR {command} TO {roles}{using}{with_check};"
)

POSTGRESQL_POLICY_ALTER_TEMPLATE = "ALTER POLICY {policy} ON {table} TO {roles}{using}{with_check};"

POSTGRESQL_POLICY_DROP_TEMPLATE = "DROP POLICY {policy} ON {table};"

POSTGRESQL_POLICY_DROP_IF_EXISTS_TEMPLATE = "DROP POLICY IF EXISTS {policy} ON {table};"

POSTGRESQL_POLICY_RENAME_TEMPLATE = "ALTER POLICY {policy} ON {table} RENAME TO {new_name};"

POSTGRESQL_GRANT_TEMPLATE = "GRANT {privileges} ON {object_type} {object} TO {roles}{grant_option};"

POSTGRESQL_REVOKE_TEMPLATE = "REVOKE {privileges} ON {object_type} {object} FROM {roles};"

#: The system column naming a row of a table - what a backfill of a table without a primary key
#: picks its batches by.
POSTGRESQL_ROW_IDENTITY_COLUMN = "ctid"
#: The name a narrowing check reads each value of an array by.
POSTGRESQL_HELD_VALUE_ALIAS = "hare_held_value"
