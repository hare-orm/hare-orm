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

#: Default connect-retry credentials (PostgresqlClient._create_pool_with_retry) - no retry by
#: default: a connection failure raises at once.
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
#: The type a bare literal branch of a CASE is cast to - a time keeps its offset, bytes are no text.
POSTGRESQL_CASE_BRANCH_TYPES: dict[type, str] = {
    **POSTGRESQL_NUMBER_LITERAL_TYPES,
    datetime.time: "TIMETZ",
    bytes: "BYTEA",
}
#: The type a bare literal argument of a function or an aggregate is cast to.
POSTGRESQL_FUNCTION_ARGUMENT_TYPES: dict[type, str] = {
    **POSTGRESQL_NUMBER_LITERAL_TYPES,
    str: "TEXT",
    datetime.date: "DATE",
    datetime.datetime: "TIMESTAMPTZ",
    datetime.time: "TIMETZ",
    bytes: "BYTEA",
}
#: The type a bare literal argument of a text function is cast to.
POSTGRESQL_TEXT_FUNCTION_ARGUMENT_TYPES: dict[type, str] = {int: "INTEGER", str: "TEXT"}
#: The type a bare literal is cast to, by where it stands - a position selected on its own takes
#: POSTGRESQL_SELECTED_LITERAL_TYPES first.
POSTGRESQL_PARAMETER_TYPES_BY_POSITION: dict[ParameterPosition, dict[type, str]] = {
    ParameterPosition.CASE_BRANCH: POSTGRESQL_CASE_BRANCH_TYPES,
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

#: The field of a ``character(n)`` column.
POSTGRESQL_FIXED_CHAR_FIELD_PATH = "hare.dialects.postgresql.fields.FixedCharField"
#: The fields of the types of a wall clock without a time zone and of an interval, by the type's name.
POSTGRESQL_NAIVE_TYPE_FIELD_PATHS = {
    "timestamp without time zone": "hare.dialects.postgresql.fields.NaiveDatetimeField",
    "time without time zone": "hare.dialects.postgresql.fields.NaiveTimeField",
    "interval": "hare.dialects.postgresql.fields.IntervalField",
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
#: The integer and the float element types of an array default's literal.
POSTGRESQL_INTEGER_ELEMENT_TYPES = frozenset({"smallint", "integer", "bigint"})
POSTGRESQL_FLOAT_ELEMENT_TYPES = frozenset({"real", "double precision"})
#: The texts PostgreSQL reads a true boolean of.
POSTGRESQL_TRUE_TEXTS = frozenset({"t", "true", "y", "yes", "on", "1"})
#: A type cast ending an expression PostgreSQL echoes back - ``'active'::character varying``,
#: ``(isbn)::text``.
POSTGRESQL_TRAILING_TYPE_CAST_RE = LazyPattern(r"^(?P<sql>.*)::(?P<cast_type>[\w \[\]]+)$", re.DOTALL)

#: index_type values whose WITH (...) storage parameters (m/ef_construction/lists) are rendered
#: as constructor kwargs - a NOTE comment flags an index the database reported none for.
POSTGRESQL_TUNED_INDEX_TYPES = frozenset({"hnsw", "ivfflat"})

#: Suffixes of the name Postgres gives an unnamed index (``<table>_<columns>_idx``) or UNIQUE
#: constraint (``<table>_<columns>_key``).
POSTGRESQL_DEFAULT_INDEX_NAME_SUFFIXES = ("idx", "key")

#: Fallback default schema when Postgres's current_schema() reports none.
POSTGRESQL_DEFAULT_SCHEMA = "public"
#: The start of the name of the schema drift builds the models in, to compare with the database.
POSTGRESQL_SHADOW_SCHEMA_PREFIX = "hare_shadow_"
#: The connection's default schema and its schema search path.
POSTGRESQL_SEARCH_PATH_SQL = "SELECT current_schema() AS default_schema, current_setting('search_path') AS search_path"
#: Creates the shadow's schema.
POSTGRESQL_SHADOW_SCHEMA_CREATE_TEMPLATE = "CREATE SCHEMA {schema}"
#: Puts the shadow's schema first on the search path, for the rest of the transaction.
POSTGRESQL_SHADOW_SEARCH_PATH_TEMPLATE = "SET LOCAL search_path TO {schema}, {search_path}"
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

#: Every index, the ones behind UNIQUE constraints and the primary key (is_primary) included - with
#: whether that constraint holds its last key WITHOUT OVERLAPS (conperiod, read through to_jsonb():
#: the column exists from Postgres 18 on); an EXCLUDE constraint's index is left out. Key columns
#: carry their opclass, order flags and collation (only where it differs from the column's);
#: INCLUDE columns are listed apart. term_sqls is each key term as pg_get_indexdef() prints it.
#: nulls_not_distinct is read through to_jsonb() - the column exists from Postgres 15 on.
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
       COALESCE(con.condeferred, false) AS is_initially_deferred,
       bool_or(COALESCE((to_jsonb(con) ->> 'conperiod')::boolean, false)) AS without_overlaps
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
-- The UNIQUE or PRIMARY KEY constraint the index backs, for its DEFERRABLE/INITIALLY DEFERRED
-- flags and WITHOUT OVERLAPS - conrelid keeps out another table's FK constraint whose conindid is
-- this same index.
LEFT JOIN pg_constraint con
    ON con.conindid = idx.indexrelid AND con.conrelid = tc.oid AND con.contype IN ('u', 'p')
WHERE ns.nspname = $1 AND tc.relname = ANY($2::text[]) AND ord.n <= idx.indnkeyatts
    AND idx.indexrelid NOT IN (
        SELECT conindid FROM pg_constraint WHERE conrelid = tc.oid AND contype = 'x'
    )
GROUP BY tc.relname, ic.relname, idx.indisunique, idx.indisprimary, am.amname, idx.indpred, idx.indrelid,
         idx.indexrelid, idx.indkey, idx.indnkeyatts, ic.reloptions, con.condeferrable, con.condeferred
ORDER BY tc.relname, ic.relname
"""

#: The field a column of an ENUM type is read back as.
NATIVE_ENUM_FIELD_PATH = "hare.dialects.postgresql.fields.NativeEnumField"
#: The field of a stored text search vector column, read back from its generation expression.
POSTGRESQL_TS_VECTOR_FIELD_PATH = "hare.dialects.postgresql.fields.TSVectorField"
#: The type of a text search vector column.
POSTGRESQL_TS_VECTOR_TYPE_NAME = "tsvector"

#: ``pg_attribute.attgenerated`` of a generated column computed on read (``VIRTUAL``, PostgreSQL 18) -
#: ``s`` for a stored one, empty for a column not generated.
POSTGRESQL_VIRTUAL_GENERATED_COLUMN = "v"

#: The ``# TODO`` of a column of a domain - no field declares one.
POSTGRESQL_DOMAIN_COLUMN_TODO = (
    "the column is of the domain {domain!r} over {type!r} - hare has no field of a domain, the field writes the "
    "domain's type; drift reports the difference until the column is altered to the field's type"
)

#: Columns in their declared order - information_schema's own type reporting, plus
#: format_type()'s full type text (vector dimensions, array element sizes), whether a generated column is
#: stored (``attgenerated``), the domain of a column of one, column comments and the
#: labels of an ENUM type, an array's element type's too, with its schema when it isn't the default one.
POSTGRESQL_COLUMNS_SQL = """
SELECT c.table_name, c.column_name, c.data_type, c.udt_name, c.is_nullable, c.character_maximum_length,
       c.numeric_precision, c.numeric_scale, c.column_default,
       c.is_generated, c.generation_expression, c.is_identity, c.identity_generation, c.domain_name,
       col_description(tc.oid, c.ordinal_position) AS description,
       format_type(a.atttypid, a.atttypmod) AS full_type, a.attgenerated::text AS attgenerated,
       (
           SELECT array_agg(e.enumlabel ORDER BY e.enumsortorder)
           FROM pg_type t
           JOIN pg_enum e ON e.enumtypid = CASE WHEN t.typcategory = 'A' THEN t.typelem ELSE t.oid END
           WHERE t.oid = a.atttypid
       ) AS enum_labels,
       (
           SELECT nullif(type_namespace.nspname, current_schema())
           FROM pg_type t
           JOIN pg_type element_type ON element_type.oid = CASE WHEN t.typcategory = 'A' THEN t.typelem ELSE t.oid END
           JOIN pg_namespace type_namespace ON type_namespace.oid = element_type.typnamespace
           WHERE t.oid = a.atttypid AND element_type.typtype = 'e'
       ) AS enum_type_schema
FROM information_schema.columns c
JOIN pg_namespace ns ON ns.nspname = c.table_schema
JOIN pg_class tc ON tc.relnamespace = ns.oid AND tc.relname = c.table_name
JOIN pg_attribute a
    ON a.attrelid = tc.oid AND a.attname = c.column_name AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.table_schema = $1 AND c.table_name = ANY($2::text[])
ORDER BY c.table_name, c.ordinal_position
"""

#: FOREIGN KEY constraints: local and target columns paired by position, the ON DELETE and ON UPDATE
#: rules, whether the check is deferrable and deferred, the target's schema and primary key columns.
#: The copies Postgres adds per partition of a partitioned target are left out.
POSTGRESQL_FOREIGN_KEYS_SQL = """
SELECT tc.relname AS table_name, con.conname,
       array_agg(la.attname ORDER BY ord.n) AS columns,
       array_agg(ra.attname ORDER BY ord.n) AS target_columns,
       target.relname AS target_table, target_ns.nspname AS target_schema,
       CASE con.confdeltype
           WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
           WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT'
       END AS delete_rule,
       CASE con.confupdtype
           WHEN 'a' THEN 'NO ACTION' WHEN 'r' THEN 'RESTRICT' WHEN 'c' THEN 'CASCADE'
           WHEN 'n' THEN 'SET NULL' WHEN 'd' THEN 'SET DEFAULT'
       END AS update_rule,
       con.condeferrable AS is_deferrable, con.condeferred AS is_initially_deferred,
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
GROUP BY tc.relname, con.oid, con.conname, target.relname, target_ns.nspname, con.confdeltype, con.confupdtype,
    con.condeferrable, con.condeferred, con.confrelid
ORDER BY tc.relname, con.conname
"""

#: The views and materialized views of a schema ($1), with the triggers on each.
POSTGRESQL_VIEWS_SQL = """
SELECT view_class.relname AS name, view_class.relkind = 'm' AS is_materialized,
       array_remove(array_agg(trigger.tgname ORDER BY trigger.tgname), NULL) AS trigger_names
FROM pg_class view_class
JOIN pg_namespace ns ON ns.oid = view_class.relnamespace
LEFT JOIN pg_trigger trigger ON trigger.tgrelid = view_class.oid AND NOT trigger.tgisinternal
WHERE ns.nspname = $1 AND view_class.relkind IN ('v', 'm')
  AND NOT EXISTS (
    SELECT 1 FROM pg_depend extension_member
    WHERE extension_member.classid = 'pg_class'::regclass AND extension_member.objid = view_class.oid
      AND extension_member.deptype = 'e'
  )
GROUP BY view_class.relname, view_class.relkind
ORDER BY view_class.relname
"""

#: The tables of a schema ($1) - plain and partitioned ones, as pg_tables lists them; a partition of
#: a partitioned table only when $2 is true, its parent otherwise stands for it. A table of an
#: extension (PostGIS's spatial_ref_sys) is the extension's, no model's.
POSTGRESQL_TABLE_NAMES_SQL = """
SELECT tc.relname AS tablename
FROM pg_class tc
JOIN pg_namespace ns ON ns.oid = tc.relnamespace
JOIN pg_tables listed ON listed.schemaname = ns.nspname AND listed.tablename = tc.relname
WHERE ns.nspname = $1 AND ($2 OR NOT tc.relispartition)
  AND NOT EXISTS (
    SELECT 1 FROM pg_depend extension_member
    WHERE extension_member.classid = 'pg_class'::regclass AND extension_member.objid = tc.oid
      AND extension_member.deptype = 'e'
  )
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
       tc.relrowsecurity AS row_level_security, tc.relforcerowsecurity AS forced_row_level_security,
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
       pg_get_functiondef(p.oid) AS function_def, t.tgenabled <> 'D' AS enabled
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

#: format_type()'s text of a numeric[] column ("numeric(10,2)[]") - information_schema reports no size
#: for an array column.
ARRAY_ELEMENT_NUMERIC_SIZE_RE = LazyPattern(r"^numeric\((\d+),(\d+)\)\[\]$")
#: format_type()'s text of a varchar[]/char[] column ("character varying(20)[]") - the same.
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
        (?:(?P<not_deferrable>NOT\ DEFERRABLE)(?:\s+INITIALLY\s+IMMEDIATE)?
            |DEFERRABLE(?:\s+INITIALLY\s+(?P<initially>IMMEDIATE|DEFERRED))?)?\s*
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

#: The ENUM types of the database outside its system schemas - each with its schema and its labels, in
#: their order.
POSTGRESQL_ENUM_TYPES_SQL = """
SELECT n.nspname AS schema_name, t.typname AS name, array_agg(e.enumlabel ORDER BY e.enumsortorder) AS labels
FROM pg_type t
JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
GROUP BY n.nspname, t.typname
"""

#: The extensions created in the database.
POSTGRESQL_EXTENSIONS_SQL = "SELECT extname AS name FROM pg_extension"

#: The views and materialized views of a schema among the named ones, with their definitions.
POSTGRESQL_VIEW_DEFINITIONS_SQL = """
SELECT viewname AS name, definition, false AS is_materialized
FROM pg_views WHERE schemaname = $1 AND viewname = ANY($2::text[])
UNION ALL
SELECT matviewname AS name, definition, true AS is_materialized
FROM pg_matviews WHERE schemaname = $1 AND matviewname = ANY($2::text[])
"""

#: The columns of each unique index of a relation, in their order.
POSTGRESQL_UNIQUE_INDEX_COLUMNS_SQL = """
SELECT array_agg(a.attname ORDER BY k.ordinality) AS columns
FROM pg_index i
JOIN pg_class c ON c.oid = i.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality)
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum
WHERE n.nspname = $1 AND c.relname = $2 AND i.indisunique
GROUP BY i.indexrelid
"""


#: The functions of a schema among the named ones, as PostgreSQL keeps them.
POSTGRESQL_FUNCTIONS_SQL = """
SELECT p.proname AS name, pg_get_function_identity_arguments(p.oid) AS arguments,
       pg_get_function_arguments(p.oid) AS full_arguments,
       pg_get_function_result(p.oid) AS returns, p.prosrc AS body, l.lanname AS language,
       p.provolatile::text AS volatility, p.prosecdef AS security_definer
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = $1 AND p.proname = ANY($2::text[])
"""


#: ``pg_proc.provolatile`` by volatility.
POSTGRESQL_VOLATILITY_CODES = {"v": "volatile", "s": "stable", "i": "immutable"}

#: The role every role is a member of, as a grant names it.
POSTGRESQL_PUBLIC_ROLE = "PUBLIC"

#: The row level security policies of the tables of a schema ($1) among the named ones ($2) - what
#: inspectdb writes in Meta.policies.
POSTGRESQL_INSPECTED_POLICIES_SQL = """
SELECT tablename AS table_name, policyname AS name, permissive, roles::text[] AS roles, cmd, qual, with_check
FROM pg_policies WHERE schemaname = $1 AND tablename = ANY($2::text[])
ORDER BY tablename, policyname
"""

#: The privileges granted on the named tables of a schema to roles other than each table's owner -
#: what inspectdb writes in Meta.grants.
POSTGRESQL_INSPECTED_TABLE_PRIVILEGES_SQL = """
SELECT c.relname AS table_name, coalesce(r.rolname, 'PUBLIC') AS role, acl.privilege_type, acl.is_grantable
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(c.relacl) acl
LEFT JOIN pg_roles r ON r.oid = acl.grantee
WHERE n.nspname = $1 AND c.relname = ANY($2::text[]) AND acl.grantee <> c.relowner
ORDER BY c.relname, role, acl.privilege_type
"""

#: The same of the columns of the named tables.
POSTGRESQL_INSPECTED_COLUMN_PRIVILEGES_SQL = """
SELECT c.relname AS table_name, a.attname AS column_name, coalesce(r.rolname, 'PUBLIC') AS role,
       acl.privilege_type, acl.is_grantable
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(a.attacl) acl
LEFT JOIN pg_roles r ON r.oid = acl.grantee
WHERE n.nspname = $1 AND c.relname = ANY($2::text[]) AND a.attnum > 0 AND NOT a.attisdropped
    AND acl.grantee <> c.relowner
ORDER BY c.relname, role, acl.privilege_type, a.attnum
"""

#: The sequences owned by a column of the named tables of a schema (OWNED BY) - not the one a primary
#: key column takes its default from (SERIAL), nor an identity column's - what inspectdb writes in
#: Meta.sequences.
POSTGRESQL_INSPECTED_SEQUENCES_SQL = """
SELECT owner_table.relname AS table_name, s.sequencename AS name, s.start_value, s.increment_by, s.min_value,
       s.max_value, s.cycle, s.cache_size, owner_column.attname AS owner_column
FROM pg_sequences s
JOIN pg_namespace n ON n.nspname = s.schemaname
JOIN pg_class sequence_class ON sequence_class.relname = s.sequencename AND sequence_class.relnamespace = n.oid
JOIN pg_depend d ON d.objid = sequence_class.oid AND d.classid = 'pg_class'::regclass
    AND d.refclassid = 'pg_class'::regclass AND d.deptype = 'a' AND d.refobjsubid > 0
JOIN pg_class owner_table ON owner_table.oid = d.refobjid
JOIN pg_attribute owner_column ON owner_column.attrelid = d.refobjid AND owner_column.attnum = d.refobjsubid
WHERE s.schemaname = $1 AND owner_table.relname = ANY($2::text[])
    AND NOT EXISTS (
        SELECT 1 FROM pg_attrdef column_default
        JOIN pg_depend default_dependency ON default_dependency.objid = column_default.oid
            AND default_dependency.classid = 'pg_attrdef'::regclass
        JOIN pg_index primary_key ON primary_key.indrelid = column_default.adrelid AND primary_key.indisprimary
            AND column_default.adnum = ANY(primary_key.indkey)
        WHERE default_dependency.refobjid = sequence_class.oid
    )
ORDER BY s.sequencename
"""

#: The sequences of a schema no column owns nor a primary key column takes its default from - inspectdb
#: names them in a note.
POSTGRESQL_UNATTACHED_SEQUENCES_SQL = """
SELECT s.sequencename AS name, s.start_value, s.increment_by, s.min_value, s.max_value, s.cycle, s.cache_size
FROM pg_sequences s
JOIN pg_namespace n ON n.nspname = s.schemaname
JOIN pg_class sequence_class ON sequence_class.relname = s.sequencename AND sequence_class.relnamespace = n.oid
WHERE s.schemaname = $1
    AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.objid = sequence_class.oid AND d.classid = 'pg_class'::regclass AND d.deptype IN ('a', 'i')
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_attrdef column_default
        JOIN pg_depend default_dependency ON default_dependency.objid = column_default.oid
            AND default_dependency.classid = 'pg_attrdef'::regclass
        JOIN pg_index primary_key ON primary_key.indrelid = column_default.adrelid AND primary_key.indisprimary
            AND column_default.adnum = ANY(primary_key.indkey)
        WHERE default_dependency.refobjid = sequence_class.oid
    )
ORDER BY s.sequencename
"""

#: The functions of a schema - not a trigger's, an aggregate, a procedure nor one of an extension;
#: inspectdb names them in a note.
POSTGRESQL_INSPECTED_FUNCTIONS_SQL = """
SELECT p.proname AS name, pg_get_function_arguments(p.oid) AS arguments,
       pg_get_function_result(p.oid) AS returns, p.prosrc AS body, l.lanname AS language,
       p.provolatile::text AS volatility, p.prosecdef AS security_definer
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
JOIN pg_language l ON l.oid = p.prolang
WHERE n.nspname = $1 AND p.prokind = 'f' AND p.prorettype <> 'trigger'::regtype
    AND NOT EXISTS (
        SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.classid = 'pg_proc'::regclass AND d.deptype = 'e'
    )
ORDER BY p.proname, arguments
"""

#: The privileges granted on the functions of a schema to roles other than each function's owner -
#: inspectdb names them with the function.
POSTGRESQL_INSPECTED_FUNCTION_PRIVILEGES_SQL = """
SELECT p.proname AS object_name, coalesce(r.rolname, 'PUBLIC') AS role, acl.privilege_type, acl.is_grantable
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
CROSS JOIN LATERAL aclexplode(p.proacl) acl
LEFT JOIN pg_roles r ON r.oid = acl.grantee
WHERE n.nspname = $1 AND acl.grantee <> p.proowner
ORDER BY p.proname, role, acl.privilege_type
"""

#: The same of the sequences of a schema.
POSTGRESQL_INSPECTED_SEQUENCE_PRIVILEGES_SQL = """
SELECT c.relname AS object_name, coalesce(r.rolname, 'PUBLIC') AS role, acl.privilege_type, acl.is_grantable
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
CROSS JOIN LATERAL aclexplode(c.relacl) acl
LEFT JOIN pg_roles r ON r.oid = acl.grantee
WHERE n.nspname = $1 AND c.relkind = 'S' AND acl.grantee <> c.relowner
ORDER BY c.relname, role, acl.privilege_type
"""
