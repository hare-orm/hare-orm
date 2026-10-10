from __future__ import annotations

import re

#: A run of "#:" comment lines right before an attribute - the attribute's documentation.
FIELD_COMMENT_RE = re.compile(r"((?:^[ \t]*#:.*\n)+)[ \t]*(\w+)\s*[:=]", re.MULTILINE)
#: What starts a line of a field comment - a class body without it holds none.
FIELD_COMMENT_MARKER = "#:"

#: The fields of a parsed statement holding nested statements - where a class definition can be.
STATEMENT_BLOCK_FIELDS = ("body", "orelse", "finalbody", "handlers", "cases")

#: Meta options a model can no longer declare -> what to declare instead.
UNSUPPORTED_META_OPTIONS = {
    "unique_together": "declare the uniqueness as UniqueConstraint(fields=(...)) in Meta.constraints",
    "version_field": "name the optimistic lock field in Meta.optimistic_lock_field",
}

#: The "#:" opening a field comment line, and the whitespace ending one.
FIELD_COMMENT_MARKER_PATTERN = re.compile(r"(^\s*#:\s*|\s*$)", re.MULTILINE)
