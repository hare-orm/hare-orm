UNREPRESENTABLE_GRANT = (
    "The catalog privilege or function overload cannot be represented by a Grant operation; reconcile it explicitly."
)
UNREPRESENTABLE_FUNCTION = (
    "The callable signatures differ; a name-based operation cannot identify the existing overload to replace."
)
UNREPRESENTABLE_PRIMARY_KEY = (
    "The physical primary key differs and has no model columns to identify a field operation."
)
UNREPRESENTABLE_COLUMN = "Physical column properties differ beyond the reconstructed field declaration."
UNREPRESENTABLE_PARTITIONING = "The catalog partitioning cannot be reconstructed as a table declaration."
UNREPRESENTABLE_SCHEMA_OBJECT = "The existing object cannot be reconstructed as a migration declaration."
UNTRACKED_COLUMNS_BLOCK_REPAIR = "A repair could rebuild a table with columns the model does not declare."
MISSING_SECONDARY_TABLE = (
    "A declared table is missing while its storage table exists; restore it explicitly without recreating stored rows."
)
