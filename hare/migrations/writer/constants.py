from __future__ import annotations

import re

#: ``python -m ruff`` argument lists makemigrations formats new migration files with, in order -
#: import sorting first, then the formatter.
MIGRATION_FORMATTER_RUFF_ARGUMENTS: tuple[tuple[str, ...], ...] = (
    ("check", "--select", "I", "--fix", "--quiet"),
    ("format", "--quiet"),
)

MIGRATION_SLUG_RE = re.compile(r"[^\w]+")

#: The names every migration file binds itself - an enum the migration declares never takes one.
MIGRATION_MODULE_NAMES = frozenset({"migrations", "ops", "Migration"})

#: The longest line a migration file writes an operation on before splitting it one argument per
#: line - the formatter's own line length.
MIGRATION_LINE_LENGTH = 119

#: A run of underscores in a migration name - collapsed to one.
MIGRATION_UNDERSCORE_RUN_PATTERN = re.compile(r"_+")

#: The longest name of a migration file after its number - a file name any file system takes.
MIGRATION_SLUG_MAX_LENGTH = 80

#: What follows the package of a dialect in the path of a field class of its - its ``fields`` package.
DIALECT_FIELDS_PACKAGE_SEPARATOR = ".fields."
