from __future__ import annotations

import datetime
import decimal
import re
from typing import Any

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.base.connection.connection_options import ConnectionOptions
from hare.dialects.base.connection.constants import PASSWORD_PROVIDER_OPTIONS
from hare.dialects.enums import ConnectionOptionType, ParameterPosition
from hare.dialects.postgresql.postgresql_dialect import PostgresqlDialect
from hare.lazy_loading.lazy_pattern import LazyPattern

#: Default connection pool bounds when not overridden via connection credentials.
DEFAULT_POOL_MINSIZE = 1
DEFAULT_POOL_MAXSIZE = 16

#: Default connect-retry credentials (PostgresqlClient._create_pool_with_retry) - retry
#: disabled by default (0 retries) for backward compatibility with existing callers that expect
#: a connection failure to raise immediately.
DEFAULT_CONNECT_MAX_RETRIES = 0
DEFAULT_CONNECT_RETRY_BACKOFF_BASE_SECONDS = 0.1

#: How often a read-only query is retried when its connection is lost mid-query - a write never is.
#: Off by default.
DEFAULT_READ_QUERY_MAX_RETRIES = 0
DEFAULT_READ_QUERY_RETRY_BACKOFF_BASE_SECONDS = 0.1

#: Sanity ceiling (one day, in seconds) for the ``command_timeout`` credential - not a business
#: limit, just a guard against a typo (e.g. milliseconds passed as seconds) silently disabling
#: the timeout in practice.
MAX_COMMAND_TIMEOUT_SECONDS = 86400.0

#: Sanity ceiling for the ``min_size``/``max_size`` pool credentials - far above any real
#: Postgres ``max_connections``, only a guard against a typo.
MAX_POOL_SIZE = 10000

#: Sanity ceiling (one day, in seconds) for the ``pool_acquire_timeout`` credential.
MAX_POOL_ACQUIRE_TIMEOUT_SECONDS = 86400.0

#: Sanity ceilings for the ``connect_max_retries``/``read_retry_max_retries`` credentials (the
#: backoff doubles on every attempt, so a larger count never finishes in practice) and for their
#: ``*_backoff_base_seconds`` counterparts (one hour).
MAX_CONNECT_RETRIES = 100
MAX_READ_QUERY_RETRIES = 100
MAX_RETRY_BACKOFF_BASE_SECONDS = 3600.0


#: Valid TCP port range for the ``port`` credential.
MIN_POSTGRES_PORT = 1
MAX_POSTGRES_PORT = 65535

#: Sanity ceiling for the ``statement_cache_size`` credential (both drivers).
MAX_STATEMENT_CACHE_SIZE = 1_000_000

#: The port setting of a PostgreSQL connection.
POSTGRES_PORT_OPTION = ConnectionOption(
    "port", ConnectionOptionType.WHOLE_NUMBER, minimum=MIN_POSTGRES_PORT, maximum=MAX_POSTGRES_PORT
)

#: The settings both PostgreSQL drivers take besides host, port, user, password and database.
POSTGRES_CONNECTION_OPTIONS = (
    ConnectionOptions(
        ConnectionOption("min_size", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_POOL_SIZE),
        ConnectionOption("max_size", ConnectionOptionType.WHOLE_NUMBER, minimum=1, maximum=MAX_POOL_SIZE),
        ConnectionOption(
            "connect_max_retries", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_CONNECT_RETRIES
        ),
        ConnectionOption(
            "connect_retry_backoff_base_seconds", ConnectionOptionType.SECONDS, maximum=MAX_RETRY_BACKOFF_BASE_SECONDS
        ),
        ConnectionOption(
            "read_retry_max_retries", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_READ_QUERY_RETRIES
        ),
        ConnectionOption(
            "read_retry_backoff_base_seconds", ConnectionOptionType.SECONDS, maximum=MAX_RETRY_BACKOFF_BASE_SECONDS
        ),
        ConnectionOption(
            "statement_cache_size", ConnectionOptionType.WHOLE_NUMBER, minimum=0, maximum=MAX_STATEMENT_CACHE_SIZE
        ),
        ConnectionOption(
            "pool_acquire_timeout",
            ConnectionOptionType.SECONDS,
            positive=True,
            maximum=MAX_POOL_ACQUIRE_TIMEOUT_SECONDS,
        ),
        ConnectionOption(
            "command_timeout", ConnectionOptionType.SECONDS, positive=True, maximum=MAX_COMMAND_TIMEOUT_SECONDS
        ),
        ConnectionOption("schema", ConnectionOptionType.TEXT),
        ConnectionOption("application_name", ConnectionOptionType.TEXT),
        ConnectionOption("tenant_schema_template", ConnectionOptionType.TEXT),
        ConnectionOption("tenant_row_level_security", ConnectionOptionType.BOOLEAN),
        ConnectionOption("transaction_pooling", ConnectionOptionType.BOOLEAN),
        ConnectionOption("direct_host", ConnectionOptionType.TEXT),
        ConnectionOption(
            "direct_port", ConnectionOptionType.WHOLE_NUMBER, minimum=MIN_POSTGRES_PORT, maximum=MAX_POSTGRES_PORT
        ),
    )
    + PASSWORD_PROVIDER_OPTIONS
)

#: The values of POSTGRES_CONNECTION_OPTIONS left unset.
POSTGRES_CONNECTION_OPTION_DEFAULTS: dict[str, Any] = {
    "min_size": DEFAULT_POOL_MINSIZE,
    "max_size": DEFAULT_POOL_MAXSIZE,
    "connect_max_retries": DEFAULT_CONNECT_MAX_RETRIES,
    "connect_retry_backoff_base_seconds": DEFAULT_CONNECT_RETRY_BACKOFF_BASE_SECONDS,
    "read_retry_max_retries": DEFAULT_READ_QUERY_MAX_RETRIES,
    "read_retry_backoff_base_seconds": DEFAULT_READ_QUERY_RETRY_BACKOFF_BASE_SECONDS,
    "pool_acquire_timeout": None,
    "command_timeout": None,
    "schema": None,
    "application_name": None,
    "tenant_schema_template": None,
    "tenant_row_level_security": False,
    "transaction_pooling": False,
    "direct_host": None,
    "direct_port": None,
    "password_provider": None,  # nosec B105 - a setting left unset, not a credential
    "password_refresh_seconds": None,  # nosec B105 - a setting left unset, not a credential
}


#: Default Postgres server port.
POSTGRES_DEFAULT_PORT = 5432


#: Connection credential carrying the lease number of a database leased from the reusable test
#: database pool - its db_create()/db_delete() reset the database instead of creating/dropping it.
REUSABLE_TEST_DATABASE_LEASE_CREDENTIAL = "reusable_test_database_lease"


POSTGRES_SERVER_VERSION_NUMBER_COLUMN = "server_version_number"
POSTGRES_SERVER_VERSION_NUMBER_SQL = (
    f"SELECT current_setting('server_version_num')::int AS {POSTGRES_SERVER_VERSION_NUMBER_COLUMN}"
)


#: The type a bare number literal is cast to.
POSTGRESQL_NUMBER_LITERAL_TYPES: dict[type, str] = {
    int: "BIGINT",
    float: "FLOAT",
    decimal.Decimal: "NUMERIC",
}
#: The type a bare literal argument of a function or an aggregate is cast to.
POSTGRESQL_FUNCTION_ARGUMENT_TYPES: dict[type, str] = {
    **POSTGRESQL_NUMBER_LITERAL_TYPES,
    str: "TEXT",
    datetime.date: "DATE",
    datetime.datetime: "TIMESTAMPTZ",
    datetime.time: "TIMETZ",
}
#: The type a bare literal argument of a text function is cast to.
POSTGRESQL_TEXT_FUNCTION_ARGUMENT_TYPES: dict[type, str] = {int: "INTEGER", str: "TEXT"}
#: The type a bare literal is cast to, by where it stands - a position selected on its own takes
#: POSTGRESQL_SELECTED_LITERAL_TYPES first.
POSTGRESQL_PARAMETER_TYPES_BY_POSITION: dict[ParameterPosition, dict[type, str]] = {
    ParameterPosition.CASE_BRANCH: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.RAW_SQL: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.SELECTED_VALUE: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.COMPARED_VALUE: POSTGRESQL_NUMBER_LITERAL_TYPES,
    ParameterPosition.FUNCTION_ARGUMENT: POSTGRESQL_FUNCTION_ARGUMENT_TYPES,
    ParameterPosition.TEXT_FUNCTION_ARGUMENT: POSTGRESQL_TEXT_FUNCTION_ARGUMENT_TYPES,
}


POSTGRESQL_DIALECT = PostgresqlDialect()


#: The name of a table storage parameter (``WITH (fillfactor = 70)``) - a lower-case identifier,
#: optionally namespaced (``toast.autovacuum_enabled``).
POSTGRESQL_STORAGE_PARAMETER_NAME_PATTERN = LazyPattern(r"[a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)?")


#: Postgres (db type keyword, field path, extra kwargs) - checked in order, first match wins, so
#: more specific keywords come before their substrings (e.g. "bigint" before "int").
POSTGRESQL_TYPE_MAP: list[tuple[str, str, dict[str, Any]]] = [
    # Range types first: "daterange" contains "date", and the first match wins. tsrange has no field
    # and falls to the ambiguous TextField. Multirange types before them: "datemultirange" contains
    # "date" too.
    ("int4multirange", "hare.dialects.postgresql.fields.multiranges.IntMultiRangeField", {}),
    ("int8multirange", "hare.dialects.postgresql.fields.multiranges.BigIntMultiRangeField", {}),
    ("nummultirange", "hare.dialects.postgresql.fields.multiranges.DecimalMultiRangeField", {}),
    ("datemultirange", "hare.dialects.postgresql.fields.multiranges.DateMultiRangeField", {}),
    ("tstzmultirange", "hare.dialects.postgresql.fields.multiranges.DateTimeMultiRangeField", {}),
    ("int4range", "hare.dialects.postgresql.fields.ranges.IntRangeField", {}),
    ("int8range", "hare.dialects.postgresql.fields.ranges.BigIntRangeField", {}),
    ("numrange", "hare.dialects.postgresql.fields.ranges.DecimalRangeField", {}),
    ("daterange", "hare.dialects.postgresql.fields.ranges.DateRangeField", {}),
    ("tstzrange", "hare.dialects.postgresql.fields.ranges.DateTimeRangeField", {}),
    ("uuid", "hare.fields.data.uuid_field.UUIDField", {}),
    ("boolean", "hare.fields.data.boolean_field.BooleanField", {}),
    ("smallint", "hare.fields.data.numeric.SmallIntField", {}),
    ("bigint", "hare.fields.data.numeric.BigIntField", {}),
    ("integer", "hare.fields.data.numeric.IntField", {}),
    ("double precision", "hare.fields.data.numeric.FloatField", {}),
    ("real", "hare.fields.data.numeric.FloatField", {}),
    ("numeric", "hare.fields.data.numeric.DecimalField", {"max_digits": 20, "decimal_places": 6}),
    ("timestamp", "hare.fields.data.temporal.DatetimeField", {}),
    ("date", "hare.fields.data.temporal.DateField", {}),
    ("time", "hare.fields.data.temporal.TimeField", {}),
    ("jsonb", "hare.fields.data.json.JSONField", {}),
    ("json", "hare.fields.data.json.JSONField", {}),
    ("bytea", "hare.fields.data.binary_field.BinaryField", {}),
    # Reports as a plain base type (data_type="tsvector"), not "USER-DEFINED" - unlike
    # geography/vector, which need their own udt_name-keyed branch instead.
    ("tsvector", "hare.dialects.postgresql.fields.ts_vector_field.TSVectorField", {}),
    # A bare "character" (CHAR/bpchar) data_type never reaches this list at all - it's
    # intercepted earlier, by exact-string match, before the generic substring loop runs.
    ("character varying", "hare.fields.data.text.CharField", {"max_length": 255}),
    ("text", "hare.fields.data.text.TextField", {}),
]

#: The field of an extension type's column, by the type's name (udt_name) - PostGIS's geography is
#: ``geography(Point,4326)``, PostGISField's only supported shape.
POSTGRESQL_EXTENSION_TYPE_FIELD_PATHS = {
    "citext": "hare.dialects.postgresql.fields.citext_field.CitextField",
    "hstore": "hare.dialects.postgresql.fields.hstore.HStoreField",
    "geography": "hare.dialects.postgresql.fields.postgis_field.PostGISField",
    "ltree": "hare.dialects.postgresql.fields.ltree_field.LtreeField",
}

#: The field of a column type matched by its whole name - ``macaddr8`` is not a ``macaddr``.
POSTGRESQL_EXACT_TYPE_FIELD_PATHS = {
    "inet": "hare.dialects.postgresql.fields.network.InetField",
    "cidr": "hare.dialects.postgresql.fields.network.CidrField",
    "macaddr": "hare.dialects.postgresql.fields.network.MacAddressField",
}

#: By the element type's pg_catalog name (udt_name without its "_" prefix) - "int4", not "integer".
#: A multidimensional array reports the same name, so dimensions aren't rebuilt.
POSTGRESQL_ARRAY_ELEMENT_TYPE_MAP: dict[str, tuple[str, dict[str, Any]]] = {
    "int2": ("hare.fields.data.numeric.SmallIntField", {}),
    "int4": ("hare.fields.data.numeric.IntField", {}),
    "int8": ("hare.fields.data.numeric.BigIntField", {}),
    "float4": ("hare.fields.data.numeric.FloatField", {}),
    "float8": ("hare.fields.data.numeric.FloatField", {}),
    "bool": ("hare.fields.data.boolean_field.BooleanField", {}),
    "text": ("hare.fields.data.text.TextField", {}),
    "varchar": ("hare.fields.data.text.CharField", {"max_length": 255}),
    "bpchar": ("hare.fields.data.text.CharField", {"max_length": 255}),
    "uuid": ("hare.fields.data.uuid_field.UUIDField", {}),
    "date": ("hare.fields.data.temporal.DateField", {}),
    "timestamptz": ("hare.fields.data.temporal.DatetimeField", {}),
    "jsonb": ("hare.fields.data.json.JSONField", {}),
    "json": ("hare.fields.data.json.JSONField", {}),
    "numeric": ("hare.fields.data.numeric.DecimalField", {"max_digits": 20, "decimal_places": 6}),
    "int4range": ("hare.dialects.postgresql.fields.ranges.IntRangeField", {}),
    "int8range": ("hare.dialects.postgresql.fields.ranges.BigIntRangeField", {}),
    "numrange": ("hare.dialects.postgresql.fields.ranges.DecimalRangeField", {}),
    "daterange": ("hare.dialects.postgresql.fields.ranges.DateRangeField", {}),
    "tstzrange": ("hare.dialects.postgresql.fields.ranges.DateTimeRangeField", {}),
    "citext": ("hare.dialects.postgresql.fields.citext_field.CitextField", {}),
    "hstore": ("hare.dialects.postgresql.fields.hstore.HStoreField", {}),
    "geography": ("hare.dialects.postgresql.fields.postgis_field.PostGISField", {}),
}

#: Postgres type casts whose quoted default literal (e.g. "'-1.5'::double precision") is a number.
POSTGRESQL_NUMERIC_CAST_TYPES = frozenset({"smallint", "integer", "bigint", "numeric", "real", "double precision"})
#: A type cast ending an expression PostgreSQL echoes back - ``'active'::character varying``,
#: ``(isbn)::text``.
POSTGRESQL_TRAILING_TYPE_CAST_RE = LazyPattern(r"^(?P<sql>.*)::(?P<cast_type>[\w \[\]]+)$", re.DOTALL)
#: Any type cast PostgreSQL adds to an expression it re-serializes - ``'a'::text``,
#: ``(0)::numeric(10,2)``, ``'{}'::text[]``.
POSTGRESQL_TYPE_CAST_RE = LazyPattern(
    r"::\s*[a-z_][a-z0-9_ ]*(?:\(\s*\d+(?:\s*,\s*\d+)?\s*\))?(?:\s*\[\s*\])*", re.IGNORECASE
)

#: index_type values whose WITH (...) storage parameters (m/ef_construction/lists) are rendered
#: as constructor kwargs - a NOTE comment flags an index the database reported none for.
POSTGRESQL_TUNED_INDEX_TYPES = frozenset({"hnsw", "ivfflat"})

#: Suffixes of the name Postgres gives an unnamed index (``<table>_<columns>_idx``) or UNIQUE
#: constraint (``<table>_<columns>_key``).
POSTGRESQL_DEFAULT_INDEX_NAME_SUFFIXES = ("idx", "key")

#: Fallback default schema when Postgres's current_schema() reports none.
POSTGRESQL_DEFAULT_SCHEMA = "public"
#: The names of the database's schemas, sorted.
POSTGRESQL_SCHEMA_NAMES_SQL = "SELECT nspname AS schema_name FROM pg_catalog.pg_namespace ORDER BY nspname"

#: One row when the table ($1) exists in the connection's default schema.
POSTGRESQL_TABLE_EXISTS_SQL = (
    "SELECT 1 FROM pg_catalog.pg_tables WHERE tablename = $1 AND schemaname = current_schema()"
)

#: Primary key columns, in the constraint's own declared column order.
POSTGRESQL_PRIMARY_KEY_COLUMNS_SQL = """
SELECT tc.relname AS table_name, a.attname AS column_name, key_column.position AS ordinal_position
FROM pg_constraint con
JOIN pg_class tc ON tc.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key_column(attnum, position)
JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = key_column.attnum
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND con.contype = 'p'
ORDER BY tc.relname, key_column.position
"""

#: Every index, the ones behind UNIQUE constraints and the primary key (is_primary) included; an
#: EXCLUDE constraint's index is left out. Key columns carry their opclass, order flags and
#: collation (only where it differs from the column's); INCLUDE columns are listed apart. term_sqls
#: is each key term as pg_get_indexdef() prints it. nulls_not_distinct is read through to_jsonb() -
#: the column exists from Postgres 15 on.
POSTGRESQL_INDEXES_SQL = """
SELECT tc.relname AS table_name, ic.relname AS index_name, array_agg(a.attname ORDER BY ord.n) AS columns,
       array_agg(oc.opcname ORDER BY ord.n) AS opclasses,
       array_agg(oc.opcdefault ORDER BY ord.n) AS opclass_is_default,
       array_agg(pg_get_indexdef(idx.indexrelid, ord.n::integer, true) ORDER BY ord.n) AS term_sqls,
       array_agg((ord.key_option & 1) = 1 ORDER BY ord.n) AS descending_keys,
       array_agg((ord.key_option & 2) = 2 ORDER BY ord.n) AS nulls_first_keys,
       array_agg(
           CASE WHEN a.attnum IS NOT NULL AND ord.collation_oid <> 0 AND ord.collation_oid <> a.attcollation
               THEN coll.collname END
           ORDER BY ord.n
       ) AS key_collations,
       (
           SELECT array_agg(ia.attname ORDER BY include_key.n)
           FROM unnest(idx.indkey::int2[]) WITH ORDINALITY AS include_key(attnum, n)
           JOIN pg_attribute ia ON ia.attrelid = idx.indrelid AND ia.attnum = include_key.attnum
           WHERE include_key.n > idx.indnkeyatts
       ) AS include_columns,
       idx.indisunique AS is_unique, idx.indisprimary AS is_primary,
       bool_or(COALESCE((to_jsonb(idx) ->> 'indnullsnotdistinct')::boolean, false)) AS nulls_not_distinct,
       am.amname AS index_type,
       pg_get_expr(idx.indpred, idx.indrelid) AS condition_sql,
       pg_get_indexdef(idx.indexrelid) AS index_def,
       ic.reloptions AS storage_parameters,
       COALESCE(con.condeferrable, false) AS is_deferrable,
       COALESCE(con.condeferred, false) AS is_initially_deferred
FROM pg_index idx
JOIN pg_class ic ON ic.oid = idx.indexrelid
JOIN pg_class tc ON tc.oid = idx.indrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_am am ON am.oid = ic.relam
CROSS JOIN LATERAL unnest(idx.indkey::int2[], idx.indclass::oid[], idx.indoption::int2[], idx.indcollation::oid[])
    WITH ORDINALITY AS ord(attnum, opclassoid, key_option, collation_oid, n)
-- LEFT, not JOIN: an expression index term (e.g. (lower(name))) has attnum = 0, which never
-- matches a pg_attribute row - an inner join would drop that term, or the whole index when every
-- term is an expression. The NULL it leaves in `columns` is how such an index is detected.
LEFT JOIN pg_attribute a ON a.attrelid = tc.oid AND a.attnum = ord.attnum
LEFT JOIN pg_opclass oc ON oc.oid = ord.opclassoid
LEFT JOIN pg_collation coll ON coll.oid = ord.collation_oid
-- The UNIQUE constraint the index backs, for its DEFERRABLE/INITIALLY DEFERRED flags - conrelid
-- keeps out another table's FK constraint whose conindid is this same index.
LEFT JOIN pg_constraint con
    ON con.conindid = idx.indexrelid AND con.conrelid = tc.oid AND con.contype = 'u'
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND ord.n <= idx.indnkeyatts
    AND idx.indexrelid NOT IN (
        SELECT conindid FROM pg_constraint WHERE conrelid = tc.oid AND contype = 'x'
    )
GROUP BY tc.relname, ic.relname, idx.indisunique, idx.indisprimary, am.amname, idx.indpred, idx.indrelid,
         idx.indexrelid, idx.indkey, idx.indnkeyatts, ic.reloptions, con.condeferrable, con.condeferred
ORDER BY tc.relname, ic.relname
"""

#: Columns in their declared order - information_schema's own type reporting, plus
#: format_type()'s full type text (vector dimensions, array element sizes) and column comments.
POSTGRESQL_COLUMNS_SQL = """
SELECT c.table_name, c.column_name, c.data_type, c.udt_name, c.is_nullable, c.character_maximum_length,
       c.numeric_precision, c.numeric_scale, c.column_default,
       c.is_generated, c.generation_expression, c.is_identity, c.identity_generation,
       col_description(tc.oid, c.ordinal_position) AS description,
       format_type(a.atttypid, a.atttypmod) AS full_type
FROM information_schema.columns c
JOIN pg_namespace ns ON ns.nspname = c.table_schema
JOIN pg_class tc ON tc.relnamespace = ns.oid AND tc.relname = c.table_name
JOIN pg_attribute a
    ON a.attrelid = tc.oid AND a.attname = c.column_name AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.table_schema = $1 AND c.table_name = ANY($2::text[])
ORDER BY c.table_name, c.ordinal_position
"""

#: FOREIGN KEY constraints: local and target columns paired by position, the ON DELETE rule, the
#: target's schema and primary key columns. The copies Postgres adds per partition of a partitioned
#: target are left out.
POSTGRESQL_FOREIGN_KEYS_SQL = """
SELECT tc.relname AS table_name, con.conname,
       array_agg(la.attname ORDER BY ord.n) AS columns,
       array_agg(ra.attname ORDER BY ord.n) AS target_columns,
       target.relname AS target_table, target_ns.nspname AS target_schema,
       CASE con.confdeltype
           WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
           WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT'
       END AS delete_rule,
       (
           SELECT array_agg(pa.attname ORDER BY target_key.position)
           FROM pg_constraint pkc
           CROSS JOIN LATERAL unnest(pkc.conkey) WITH ORDINALITY AS target_key(attnum, position)
           JOIN pg_attribute pa ON pa.attrelid = pkc.conrelid AND pa.attnum = target_key.attnum
           WHERE pkc.conrelid = con.confrelid AND pkc.contype = 'p'
       ) AS target_primary_key_columns
FROM pg_constraint con
JOIN pg_class tc ON tc.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_class target ON target.oid = con.confrelid
JOIN pg_namespace target_ns ON target_ns.oid = target.relnamespace
CROSS JOIN LATERAL unnest(con.conkey, con.confkey) WITH ORDINALITY AS ord(attnum, target_attnum, n)
JOIN pg_attribute la ON la.attrelid = tc.oid AND la.attnum = ord.attnum
JOIN pg_attribute ra ON ra.attrelid = target.oid AND ra.attnum = ord.target_attnum
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND con.contype = 'f'
    AND NOT EXISTS (
        SELECT 1 FROM pg_constraint parent_con
        WHERE parent_con.oid = con.conparentid AND parent_con.conrelid = con.conrelid
    )
GROUP BY tc.relname, con.oid, con.conname, target.relname, target_ns.nspname, con.confdeltype, con.confrelid
ORDER BY tc.relname, con.conname
"""

#: The tables of a schema ($1) - plain and partitioned ones, as pg_tables lists them; a partition of
#: a partitioned table only when $2 is true, its parent otherwise stands for it.
POSTGRESQL_TABLE_NAMES_SQL = """
SELECT tc.relname AS tablename
FROM pg_class tc
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_tables listed ON listed.schemaname = ns.nspname AND listed.tablename = tc.relname
WHERE ns.nspname = $1 AND ($2 OR NOT tc.relispartition)
ORDER BY tc.relname
"""

#: Whether a schema exists.
POSTGRESQL_SCHEMA_EXISTS_SQL = "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1"

#: Table comments, whether the schema is the connection's default one, and storage (persistence,
#: storage parameters, tablespace - NULL for the database's default one, named in
#: default_tablespace) - also tells which requested tables exist.
POSTGRESQL_TABLES_SQL = """
SELECT tc.relname AS table_name, obj_description(tc.oid, 'pg_class') AS description,
       ns.nspname = current_schema() AS is_default_schema,
       tc.relpersistence = 'u' AS unlogged, tc.reloptions AS storage_parameters, ts.spcname AS tablespace,
       (SELECT dts.spcname FROM pg_database db JOIN pg_tablespace dts ON dts.oid = db.dattablespace
        WHERE db.datname = current_database()) AS default_tablespace
FROM pg_class tc
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
LEFT JOIN pg_tablespace ts ON ts.oid = tc.reltablespace
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[])
"""

#: The partitions of partitioned tables ($2) in a schema ($1): one row per partition with the
#: table's strategy, its key columns and their types, the partition's bound, storage parameters
#: and tablespace - a partitioned table without partitions has one row with no partition.
POSTGRESQL_PARTITIONS_SQL = """
SELECT tc.relname AS table_name, pt.partstrat::text AS strategy, pt.partnatts::int AS key_column_count,
       ARRAY(SELECT att.attname::text
             FROM unnest(pt.partattrs::int2[]) WITH ORDINALITY AS key_column(attnum, position)
             JOIN pg_attribute att ON att.attrelid = tc.oid AND att.attnum = key_column.attnum
             ORDER BY key_column.position) AS key_columns,
       ARRAY(SELECT format_type(att.atttypid, att.atttypmod)
             FROM unnest(pt.partattrs::int2[]) WITH ORDINALITY AS key_column(attnum, position)
             JOIN pg_attribute att ON att.attrelid = tc.oid AND att.attnum = key_column.attnum
             ORDER BY key_column.position) AS key_types,
       child.relname AS partition_table, pg_get_expr(child.relpartbound, child.oid) AS bound,
       child.reloptions AS storage_parameters, cts.spcname AS tablespace
FROM pg_partitioned_table pt
JOIN pg_class tc ON tc.oid = pt.partrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
LEFT JOIN pg_inherits inh ON inh.inhparent = tc.oid
LEFT JOIN pg_class child ON child.oid = inh.inhrelid
LEFT JOIN pg_tablespace cts ON cts.oid = child.reltablespace
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[])
ORDER BY tc.relname, child.relname
"""

#: User-defined triggers as Postgres's own canonical CREATE TRIGGER/CREATE FUNCTION text -
#: tgisinternal leaves out the hidden triggers enforcing a FOREIGN KEY constraint.
POSTGRESQL_TRIGGERS_SQL = """
SELECT tc.relname AS table_name, t.tgname AS name, pg_get_triggerdef(t.oid, false) AS trigger_def,
       pg_get_functiondef(p.oid) AS function_def
FROM pg_trigger t
JOIN pg_class tc ON tc.oid = t.tgrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_proc p ON p.oid = t.tgfoid
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND NOT t.tgisinternal
ORDER BY tc.relname, t.tgname
"""

#: EXCLUDE and CHECK constraints as pg_get_constraintdef()'s canonical text.
POSTGRESQL_CONSTRAINTS_SQL = """
SELECT tc.relname AS table_name, con.contype::text AS constraint_type, con.conname,
       pg_get_constraintdef(con.oid) AS definition
FROM pg_constraint con
JOIN pg_class tc ON tc.oid = con.conrelid
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND con.contype IN ('x', 'c')
ORDER BY tc.relname, con.conname
"""

#: pg_constraint.contype of an EXCLUDE constraint.
POSTGRESQL_EXCLUSION_CONSTRAINT_TYPE = "x"

#: Postgres type names drift compares a column's type by - every spelling Postgres accepts for one
#: of them (``int4``, ``varchar``, ``timestamptz``, ``serial``, ...) mapped to the name
#: ``format_type()`` reports it under. A type outside these values is never compared.
POSTGRESQL_CANONICAL_TYPE_NAMES: dict[str, str] = {
    "int": "integer",
    "int4": "integer",
    "integer": "integer",
    "serial": "integer",
    "serial4": "integer",
    "int2": "smallint",
    "smallint": "smallint",
    "smallserial": "smallint",
    "serial2": "smallint",
    "int8": "bigint",
    "bigint": "bigint",
    "bigserial": "bigint",
    "serial8": "bigint",
    "bool": "boolean",
    "boolean": "boolean",
    "varchar": "character varying",
    "character varying": "character varying",
    "char": "character",
    "character": "character",
    "bpchar": "character",
    "text": "text",
    "decimal": "numeric",
    "numeric": "numeric",
    "float": "double precision",
    "float8": "double precision",
    "double precision": "double precision",
    "float4": "real",
    "real": "real",
    "timestamptz": "timestamp with time zone",
    "timestamp with time zone": "timestamp with time zone",
    "timestamp": "timestamp without time zone",
    "timestamp without time zone": "timestamp without time zone",
    "timetz": "time with time zone",
    "time with time zone": "time with time zone",
    "time": "time without time zone",
    "time without time zone": "time without time zone",
    "date": "date",
    "uuid": "uuid",
    "json": "json",
    "jsonb": "jsonb",
    "bytea": "bytea",
}

#: A Postgres type name in its own parameter-free spelling - ``timestamp(3) with time zone`` keeps
#: its precision apart from the time zone suffix.
POSTGRESQL_TYPE_RE = LazyPattern(
    r"^(?P<name>[a-z_][a-z0-9_ ]*?)\s*(?:\((?P<parameters>[^)]*)\))?"
    r"(?P<time_zone>\s+with(?:out)?\s+time\s+zone)?(?P<array>(?:\s*\[\s*\])*)$"
)

#: The length Postgres gives a ``character`` column declared without one.
POSTGRESQL_DEFAULT_CHARACTER_LENGTH = "1"


#: format_type(atttypid, atttypmod)'s own rendering of a pgvector column, e.g. "vector(768)" -
#: information_schema has no dedicated column for this (vector is an extension type), so the
#: dimension count is parsed out of this text instead.
VECTOR_DIMENSIONS_RE = LazyPattern(r"^vector\((\d+)\)$")

#: format_type()'s text of a numeric[]/varchar[]/char[] column ("numeric(10,2)[]") -
#: information_schema reports no size for an array column.
ARRAY_ELEMENT_NUMERIC_SIZE_RE = LazyPattern(r"^numeric\((\d+),(\d+)\)\[\]$")

ARRAY_ELEMENT_CHAR_LENGTH_RE = LazyPattern(r"^character(?: varying)?\((\d+)\)\[\]$")

EXCLUSION_CONSTRAINT_DEF_RE = LazyPattern(
    r"^EXCLUDE USING (?P<using>\w+) \((?P<expressions>.+?)\)(?: INCLUDE \((?P<include>[^)]*)\))?"
    r"(?: WHERE \((?P<condition>.+)\))?"
    r"(?P<deferrable> DEFERRABLE(?: INITIALLY (?P<initially>DEFERRED|IMMEDIATE))?)?$"
)

EXCLUSION_EXPRESSION_RE = LazyPattern(r'^(?P<field>"[^"]+"|\w+) WITH (?P<operator>[^\s,]+)$')

EXCLUSION_RAW_EXPRESSION_RE = LazyPattern(r"^(?P<expression>.+) WITH (?P<operator>[^\s,]+)$", re.DOTALL)

# pg_get_constraintdef() of a CHECK constraint: "CHECK (<expr>)", with NOT VALID matched apart.
POSTGRES_CHECK_CONSTRAINT_DEF_RE = LazyPattern(r"^CHECK \((?P<expression>.+)\)(?P<not_valid> NOT VALID)?$", re.DOTALL)


POSTGRES_TRIGGERDEF_RE = LazyPattern(
    r"""CREATE\s+(?P<constraint>CONSTRAINT\s+)?TRIGGER\s+"?(?P<name>[\w]+)"?\s+
        (?P<timing>BEFORE|AFTER|INSTEAD\ OF)\s+
        (?P<on>.+?)\s+ON\s+\S+\s+
        (?:FROM\s+(?P<from_table>\S+)\s+)?
        (?:(?P<not_deferrable>NOT\ DEFERRABLE)|DEFERRABLE(?:\s+INITIALLY\s+(?P<initially>IMMEDIATE|DEFERRED))?)?\s*
        FOR\ EACH\ (?P<for_each>ROW|STATEMENT)\s*
        (?:WHEN\s+\((?P<when>.+?)\)\s+)?
        EXECUTE\s+(?:FUNCTION|PROCEDURE)\s+\S+\([^)]*\)\s*;?\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

POSTGRES_FUNCTIONDEF_RE = LazyPattern(
    r"LANGUAGE\s+(?P<language>\w+).*?AS\s+\$(?P<tag>\w*)\$(?P<body>.*)\$(?P=tag)\$",
    re.IGNORECASE | re.DOTALL,
)

#: Strips the BEGIN...END the trigger function template wraps the body in.
POSTGRES_FUNCTION_BODY_WRAPPER_RE = LazyPattern(r"\A\s*BEGIN\s*(?P<inner>.*?)\s*END\s*;?\s*\Z", re.DOTALL)


#: The language of a trigger's function when its ``Trigger.language`` is None.
POSTGRESQL_DEFAULT_TRIGGER_LANGUAGE = "plpgsql"
