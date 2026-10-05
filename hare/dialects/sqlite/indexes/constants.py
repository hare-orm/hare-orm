from __future__ import annotations

#: The R*Tree table SpatiaLite keeps a column's spatial index in - named by SpatiaLite itself.
SPATIALITE_INDEX_TABLE_NAME = "idx_{table}_{column}"

#: The dimensions of a geometry column as SpatiaLite registers it - by the field's ``dimensions``.
SPATIALITE_DIMENSION_NAMES = {2: "XY", 3: "XYZ"}

#: The condition of a column registered in the spatial metadata - with ``{spatial_index}`` its
#: spatial index enabled.
SPATIALITE_REGISTERED_COLUMN_SQL = (
    "SELECT 1 FROM geometry_columns WHERE lower(f_table_name) = lower({table}) "
    "AND lower(f_geometry_column) = lower({column}){spatial_index}"
)

SPATIALITE_ENABLED_INDEX_CONDITION_SQL = " AND spatial_index_enabled = 1"

#: Registers a geometry column in the spatial metadata - SpatiaLite's triggers refuse a geometry of
#: another type, SRID or dimensions from then on; 1 when done, 0 when a row's geometry doesn't fit.
SPATIALITE_REGISTER_COLUMN_SQL = (
    "SELECT RecoverGeometryColumn({table}, {column}, {srid}, {geometry_type}, {dimensions}) AS {result}"
)

#: Creates a registered column's R*Tree and the triggers keeping it in step - 1 when done.
SPATIALITE_CREATE_INDEX_SQL = "SELECT CreateSpatialIndex({table}, {column}) AS {result}"

#: Removes a column's spatial index - its triggers and its registration, the R*Tree table itself.
SPATIALITE_DISABLE_INDEX_SQL = "SELECT DisableSpatialIndex({table}, {column})"

SPATIALITE_DROP_INDEX_TABLE_SQL = "DROP TABLE IF EXISTS {index_table}"

SPATIALITE_UNREGISTER_COLUMN_SQL = "SELECT DiscardGeometryColumn({table}, {column})"

#: The command filling an FTS5 index from its table again.
SQLITE_FULL_TEXT_REBUILD_COMMAND = "rebuild"

#: The command removing a row from an FTS5 index kept in step with its table.
SQLITE_FULL_TEXT_DELETE_COMMAND = "delete"

#: How a ``FullTextIndex``'s tokenizer sets it apart from another index over the same fields.
SQLITE_FULL_TEXT_TOKENIZER_DESCRIPTION = " tokenize={tokenizer}"

#: The longest ``tokenizer`` a ``FullTextIndex`` takes - a sanity ceiling.
SQLITE_FULL_TEXT_MAX_TOKENIZER_LENGTH = 200
