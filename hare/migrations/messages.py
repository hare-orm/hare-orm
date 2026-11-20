MIGRATION_SESSION_CLOSE_FAILED = "Migration request and session cleanup failed"
KEY_SERIES_SYNCHRONIZATION_PREVIEW = (
    "-- Synchronize generated key series for {model} from stored keys during execution, where the database uses them."
)
REMOVE_INDEX_SELECTORS_CONFLICT = "RemoveIndex.index is exclusive with name and fields."
REMOVED_INDEX_NOT_FOUND = "Index {index!r} is not present on {model}"
GENERATED_FIELD_PRESERVATION_HINT = (
    "To preserve values, copy them to a temporary column before removal, then populate the replacement "
    "column in a hand-written migration."
)
