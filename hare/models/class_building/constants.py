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

#: Meta options a model can no longer declare -> what to declare instead.
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

#: Meta options taking ``True`` or ``False`` - but ``soft_delete_hard_cascade``, checked with the
#: soft delete field it needs.
BOOLEAN_META_OPTIONS = frozenset(
    {
        ModelOption.ABSTRACT,
        ModelOption.MANAGED,
        ModelOption.RETURNING,
        ModelOption.TENANT_SCHEMA,
        ModelOption.TRACK_DIRTY_FIELDS,
    }
)
#: Meta options taking a text - the name of a table, an app or a field, a description; ``schema`` is
#: checked when the model is bound.
TEXT_META_OPTIONS = frozenset(
    {
        ModelOption.APP,
        ModelOption.OPTIMISTIC_LOCK_FIELD,
        ModelOption.SOFT_DELETE_FIELD,
        ModelOption.TABLE,
        ModelOption.TABLE_DESCRIPTION,
        ModelOption.TENANT_FIELD,
    }
)
#: Meta options taking a list or a tuple - a text in place of one would be read letter by letter.
#: The lists of declared database objects (views, functions, ...) are checked as they are read.
SEQUENCE_META_OPTIONS = frozenset(
    {
        ModelOption.CONSTRAINTS,
        ModelOption.EXTENSIONS,
        ModelOption.INDEXES,
        ModelOption.ORDERING,
        ModelOption.TABLE_OPTIONS,
        ModelOption.TRIGGERS,
    }
)
#: Each Meta option of one type of value -> the types of its values, and how an error names them.
META_OPTION_VALUE_TYPES: dict[str, tuple[tuple[type, ...], str]] = {
    **{option.value: ((bool,), "True or False") for option in BOOLEAN_META_OPTIONS},
    **{option.value: ((str,), "a text") for option in TEXT_META_OPTIONS},
    **{option.value: ((list, tuple), "a list or a tuple") for option in SEQUENCE_META_OPTIONS},
}
#: What joins a schema and a table in a qualified name - no table name holds it.
TABLE_NAME_SEPARATOR = "."
#: Meta options naming something - an empty text names nothing.
NAMING_META_OPTIONS = frozenset(
    {
        ModelOption.APP.value,
        ModelOption.OPTIMISTIC_LOCK_FIELD.value,
        ModelOption.SOFT_DELETE_FIELD.value,
        ModelOption.TABLE.value,
        ModelOption.TENANT_FIELD.value,
    }
)
