PROJECTION_WRONG_MODEL = (
    "{model}: ClickhouseProjection {name!r} is a queryset of {query_model} - a projection is a query of its table"
)
PROJECTION_COMBINED_QUERIES = "ClickhouseProjection {name!r} cannot combine queries"
DICTIONARY_SOURCE_IDENTIFIER = "Dictionary source type and option names must be SQL identifiers, got {identifier!r}"
DICTIONARY_SOURCE_DUPLICATE_OPTION = "Dictionary source names an option more than once ignoring case"
PROJECTION_UNSUPPORTED_SHAPE = (
    "{model}: ClickhouseProjection {name!r} filters, joins or limits its rows, or uses unsupported DISTINCT, "
    "CTEs, sampling, row locks or query modifiers - a projection requires every row with optional grouping "
    "and ascending ordering"
)
PROJECTION_UNSUPPORTED_ORDER = (
    "{model}: ClickhouseProjection {name!r} orders its rows otherwise than ascending - "
    "structured projections require ascending ordering, NULL last"
)
ENGINE_INVALID_NAME = "Engine name must be a SQL identifier; declare arguments with ClickhouseEngine, got {name!r}"
ENGINE_REPLICA_ARGUMENTS = "replica_path and replica_name must be supplied together for a Replicated or Shared engine"
ENGINE_RAW_SHADOW_PATH = (
    "{name}: an opaque replicated engine cannot be isolated in a shadow database; "
    "declare its arguments and replica_path with ClickhouseEngine"
)

ENGINE_RAW_REBUILD_PATH = "A replicated raw engine cannot be rebuilt without a structured replica path"
ENGINE_DISTRIBUTED_SHADOW_TARGET = (
    "Cannot isolate a Distributed engine: declare its target database as currentDatabase() or as a literal "
    "database included in the schema state; opaque SQL and external databases cannot be shadow targets"
)
REBUILD_STORAGE_TARGET_EXISTS = (
    "Cannot move rebuilt storage to {database!r}.{table!r}: that table already exists. "
    "Choose an unused storage table name."
)
REBUILD_STORAGE_TO_ROUTER = (
    "Replacing stored rows with a standalone Distributed router requires an explicit data migration. "
    "Create the router separately and move or retain the original storage before removing its table."
)
REBUILD_DATABASE_ENGINE_UNSUPPORTED = (
    "Database {database!r} uses {engine!r}; rebuilding a table requires an Atomic, Replicated or Shared "
    "database with EXCHANGE TABLES support. The original table has not been replaced."
)
ENGINE_MISSING_REPLICA_DEFAULTS = (
    "Cannot isolate a replicated engine without its path: server default_replica_path must be captured first"
)
ENGINE_UNSCOPED_REPLICA_DEFAULT = (
    "Cannot isolate a replicated engine: default_replica_path must contain {uuid} or {database}; "
    "otherwise declare an explicit replica_path"
)
ENGINE_UNKNOWN_DATABASE_PATH = (
    "Cannot isolate the catalog database engine: expected Replicated(path, shard, replica) with a literal Keeper path"
)
ENGINE_FIXED_DEFAULT_REPLICA_PATH = (
    "{model}: rebuilding this table requires default_replica_path to contain {uuid}; "
    "declare a replica_path containing {uuid} or change the server default"
)
