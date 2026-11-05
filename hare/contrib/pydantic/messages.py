RECURSIVE_SCHEMA_FETCH = (
    "Automatic relation fetching requires a finite Pydantic schema. Generate a schema with a recursion limit, "
    "or load the relations explicitly and use model_validate()."
)

COLUMN_NOT_FETCHED = (
    "Field {field!r} has not been fetched - the instance was loaded with "
    "a .only()/.defer() that excluded it, or a write returning no row left it unread. "
    "Fetch it (or drop the field from this schema) before serializing."
)
RELATION_NOT_FETCHED = (
    "Field {field!r} has not been fetched - use select_related()/prefetch_related() before serializing."
)
RELATED_ROWS_NOT_FETCHED = "Field {field!r} has not been fetched - use prefetch_related() before serializing."
