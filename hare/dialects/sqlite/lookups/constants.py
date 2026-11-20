from __future__ import annotations

#: ``json_type()`` names of each JSON ``__filter`` comparison type.
SQLITE_JSON_TYPE_NAMES: dict[str, tuple[str, ...]] = {
    "boolean": ("true", "false"),
    "number": ("integer", "real"),
    "string": ("text",),
    "container": ("object", "array"),
}

#: Why ``__search`` can't run on a field no ``FullTextIndex`` covers.
SQLITE_FULL_TEXT_SEARCH_UNSUPPORTED_REASON = (
    "SQLite searches text through an FTS5 index - declare a FullTextIndex over the field in Meta.indexes"
)
