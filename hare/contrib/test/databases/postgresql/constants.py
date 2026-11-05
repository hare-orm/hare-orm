"""Catalog statements for leasing, clearing and reusing PostgreSQL test databases."""

from __future__ import annotations

#: Whether a database of the name exists.
POSTGRES_DATABASE_EXISTS_SQL = "SELECT 1 AS database_exists FROM pg_database WHERE datname = $1"

#: Ends every other session of the current database.
POSTGRES_TERMINATE_OTHER_DATABASE_SESSIONS_SQL = (
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = current_database() AND pid <> pg_backend_pid()"
)

#: The statements below reset a reusable test database to the state of a freshly created one. None of
#: them requests a checkpoint, unlike DROP DATABASE.
#: The prepared transactions of the current database.
POSTGRES_DATABASE_PREPARED_TRANSACTIONS_SQL = "SELECT gid FROM pg_prepared_xacts WHERE database = current_database()"

#: Rolls a prepared transaction back.
POSTGRES_ROLLBACK_PREPARED_SQL = "ROLLBACK PREPARED {gid_literal}"

#: The schemas the database's user made.
POSTGRES_USER_SCHEMAS_SQL = (
    r"SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\_%' AND nspname <> 'information_schema'"
)

#: The roles with settings of their own in the current database.
POSTGRES_DATABASE_ROLE_SETTINGS_SQL = (
    "SELECT setrole::regrole::text AS role_name FROM pg_db_role_setting "
    "WHERE setdatabase = (SELECT oid FROM pg_database WHERE datname = current_database()) AND setrole <> 0"
)

#: Drops a schema and everything in it.
POSTGRES_DROP_SCHEMA_CASCADE_SQL = "DROP SCHEMA {schema} CASCADE"

#: Resets the settings of a database.
POSTGRES_RESET_DATABASE_SETTINGS_SQL = "ALTER DATABASE {database} RESET ALL"

#: Resets the settings of a role in a database.
POSTGRES_RESET_DATABASE_ROLE_SETTINGS_SQL = "ALTER ROLE {role} IN DATABASE {database} RESET ALL"

#: The current transaction's id, as the 32-bit id a catalog row's xmin holds.
POSTGRES_CURRENT_TRANSACTION_ID_EXPRESSION = "(pg_current_xact_id()::text::bigint % 4294967296)"

#: The number of rows of a catalog.
POSTGRES_CATALOG_ROW_COUNT_EXPRESSION = "(SELECT count(*) FROM {catalog})"

#: The catalogs of the current database that DDL writes: its own, not the ones the cluster shares, without
#: the statistics ANALYZE writes and the large objects' data.
POSTGRES_DATABASE_CATALOGS_SQL = (
    "SELECT c.oid::regclass::text AS catalog_name FROM pg_class c "
    "WHERE c.relnamespace = 'pg_catalog'::regnamespace AND c.relkind = 'r' AND NOT c.relisshared "
    "AND c.relname NOT IN ('pg_statistic', 'pg_statistic_ext_data', 'pg_largeobject') ORDER BY 1"
)

#: Whether a catalog holds a row a transaction later than the given one wrote.
POSTGRES_CATALOG_CHANGED_SQL = "EXISTS (SELECT FROM {catalog} WHERE age(xmin) < age('{transaction_id}'::xid))"

#: The catalog whose rows a statement changing no definition writes too - TRUNCATE and ALTER SEQUENCE ...
#: RESTART give a relation new storage - told by the fingerprint of its definitions instead.
POSTGRES_STORAGE_WRITTEN_CATALOG = "pg_class"

#: The digest of every relation's definition: pg_class without its storage and statistics columns.
POSTGRES_CLASS_DEFINITIONS_FINGERPRINT_EXPRESSION = (
    "(SELECT md5(coalesce(string_agg(concat_ws('|', oid, relname, relnamespace, reltype, reloftype, relowner, "
    "relam, reltablespace, relhasindex, relpersistence, relkind, relnatts, relchecks, relhasrules, relhastriggers, "
    "relhassubclass, relrowsecurity, relforcerowsecurity, relreplident, relispartition, "
    "pg_get_expr(relpartbound, oid), reloptions::text, relacl::text), ',' ORDER BY oid), '')) FROM pg_class)"
)

#: The start of a test database's comment keeping the schema it holds - a later test process takes the
#: schema from there instead of creating the database again.
POSTGRES_KEPT_TEST_SCHEMA_COMMENT_PREFIX = "hare-kept-test-schema:"

#: The comment of the current database.
POSTGRES_DATABASE_COMMENT_SQL = (
    "SELECT shobj_description(oid, 'pg_database') AS comment FROM pg_database WHERE datname = current_database()"
)

#: Sets a database's comment - a row of a catalog the cluster shares, so no change of the database's schema.
POSTGRES_COMMENT_ON_DATABASE_SQL = "COMMENT ON DATABASE {database} IS {comment}"

#: Whether the database holds data emptying its tables leaves: a materialized view's rows, a foreign
#: table's, a large object.
POSTGRES_DATA_BEYOND_TABLES_SQL = (
    "SELECT EXISTS (SELECT FROM pg_class WHERE relkind IN ('m', 'f')) "
    "OR EXISTS (SELECT FROM pg_largeobject_metadata) AS found"
)

#: The condition of a pg_class row ``c`` (in pg_namespace ``n``) the database's user made: in a user schema,
#: not created by an extension.
POSTGRES_USER_RELATION_CONDITION = (
    r"n.nspname NOT LIKE 'pg\_%' AND n.nspname <> 'information_schema' AND NOT EXISTS ("
    "SELECT FROM pg_depend d WHERE d.classid = 'pg_class'::regclass AND d.objid = c.oid AND d.deptype = 'e')"
)

#: Every table the database's user made, quoted with its schema.
POSTGRES_USER_TABLES_SQL = (
    "SELECT format('%I.%I', n.nspname, c.relname) AS table_name FROM pg_class c "  # nosec B608 - constants only
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    f"WHERE c.relkind IN ('r', 'p') AND {POSTGRES_USER_RELATION_CONDITION}"
)

#: Every sequence the database's user made that gave out a value, quoted with its schema.
POSTGRES_USED_SEQUENCES_SQL = (
    "SELECT format('%I.%I', n.nspname, c.relname) AS sequence_name FROM pg_class c "  # nosec B608 - constants only
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE CASE WHEN c.relkind = 'S' THEN pg_sequence_last_value(c.oid) IS NOT NULL END "
    f"AND {POSTGRES_USER_RELATION_CONDITION}"
)

POSTGRES_RESTART_SEQUENCE_SQL = "ALTER SEQUENCE {sequence} RESTART"

#: First server version whose fresh public schema is owned by pg_database_owner with only USAGE
#: granted to PUBLIC - older ones grant PUBLIC both CREATE and USAGE.
POSTGRES_DATABASE_OWNER_PUBLIC_SCHEMA_VERSION_NUMBER = 150000

POSTGRES_CREATE_PUBLIC_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA public AUTHORIZATION pg_database_owner",
    "GRANT USAGE ON SCHEMA public TO PUBLIC",
    "COMMENT ON SCHEMA public IS 'standard public schema'",
)

POSTGRES_CREATE_LEGACY_PUBLIC_SCHEMA_STATEMENTS = (
    "CREATE SCHEMA public",
    "GRANT ALL ON SCHEMA public TO PUBLIC",
    "COMMENT ON SCHEMA public IS 'standard public schema'",
)
