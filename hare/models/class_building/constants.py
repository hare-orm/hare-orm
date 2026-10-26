from __future__ import annotations

import re

from hare.models.enums import ModelOption

#: A run of "#:" comment lines right before an attribute - the attribute's documentation.
FIELD_COMMENT_RE = re.compile(r"((?:^[ \t]*#:.*\n)+)[ \t]*(\w+)\s*[:=]", re.MULTILINE)
#: What starts a line of a field comment - a class body without it holds none.
FIELD_COMMENT_MARKER = "#:"

#: The fields of a parsed statement holding nested statements - where a class definition can be.
STATEMENT_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")

#: What joins the parts of a lookup path (``author__name__icontains``) - no field name holds it.
LOOKUP_SEPARATOR = "__"

#: Meta options of other ORMs a model doesn't declare -> what to declare instead.
UNSUPPORTED_META_OPTIONS = {
    "unique_together": "declare the uniqueness as UniqueConstraint(fields=(...)) in Meta.constraints",
    "version_field": "name the optimistic lock field in Meta.optimistic_lock_field",
    "db_table": "name the table in Meta.table",
    "app_label": "name the app in Meta.app",
    "db_table_comment": "describe the table in Meta.table_description",
    "default_connection": "set it per app in the apps.<name>.default_connection key of Hare.init()",
}

#: The "#:" opening a field comment line, and the whitespace ending one.
FIELD_COMMENT_MARKER_PATTERN = re.compile(r"(^\s*#:\s*|\s*$)", re.MULTILINE)

#: Meta options the DDL writes as a text - the table's name and description; ``schema`` takes one too,
#: refused empty once the model's fields are known.
TEXT_META_OPTIONS = frozenset({ModelOption.TABLE, ModelOption.TABLE_DESCRIPTION})
#: Each Meta option of one type of value -> the types of its values, and how an error names them.
META_OPTION_VALUE_TYPES: dict[str, tuple[tuple[type, ...], str]] = {
    **{option.value: ((str,), "a text") for option in TEXT_META_OPTIONS},
    ModelOption.SCHEMA.value: ((str,), "a text"),
}
#: Meta options None leaves unset - the ones naming something, which name nothing by default.
NONE_TAKING_META_OPTIONS = frozenset({*(option.value for option in TEXT_META_OPTIONS), ModelOption.SCHEMA.value})
#: What joins a schema and a table in a qualified name - no table name holds it.
TABLE_NAME_SEPARATOR = "."
#: Meta options naming something in the DDL - an empty text names nothing.
NAMING_META_OPTIONS = frozenset({ModelOption.TABLE.value})
