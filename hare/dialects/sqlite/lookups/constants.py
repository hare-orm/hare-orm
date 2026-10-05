from __future__ import annotations

#: ``json_type()`` names of each JSON ``__filter`` comparison type.
SQLITE_JSON_TYPE_NAMES: dict[str, tuple[str, ...]] = {
    "boolean": ("true", "false"),
    "number": ("integer", "real"),
    "string": ("text",),
    "container": ("object", "array"),
}

#: The ``printf()`` format of the SQLite JSON path of one key - the key written as a JSON string, so a
#: key with a dot or a quote in its name is one label.
SQLITE_JSON_KEY_PATH_FORMAT = "$.%s"

#: Why ``__search`` can't run on a field no ``FullTextIndex`` covers.
SQLITE_FULL_TEXT_SEARCH_UNSUPPORTED_REASON = (
    "SQLite searches text through an FTS5 index - declare a FullTextIndex over the field in Meta.indexes"
)
