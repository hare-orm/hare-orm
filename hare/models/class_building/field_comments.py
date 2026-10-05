from __future__ import annotations

import ast
import inspect
import linecache
import sys
from types import ModuleType
from typing import TYPE_CHECKING, ClassVar

from hare.core.caching.cache import Cache
from hare.models.class_building.constants import (
    FIELD_COMMENT_MARKER,
    FIELD_COMMENT_MARKER_PATTERN,
    FIELD_COMMENT_RE,
    STATEMENT_BLOCK_FIELDS,
)

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class FieldComments:
    """The descriptions a model's fields take from the #: comments above them in the class body - read
    from the module's source, the class found by its line span."""

    #: (source file,) -> the file's lines, the last line of each class in it by the class's first
    #: line and the classes' first lines by qualified name (``get_class_spans()``).
    source_class_spans: ClassVar[Cache[tuple[list[str], dict[int, int], dict[str, list[int]]]]] = Cache(
        Cache.max_size_from_env(), holds_sql=False, keyed_by_model=False
    )
    #: (source file,) -> the module its source was last checked for being current - every class of
    #: one loaded module reads the same source, checked once.
    checked_modules: ClassVar[Cache[ModuleType]] = Cache(
        Cache.max_size_from_env(), holds_sql=False, keyed_by_model=False
    )

    @staticmethod
    def get_comments(model_class: type[Model]) -> dict[str, str]:
        """The ``#:`` comments standing right before the model's attributes, by field name. ``{model}``
        in a comment is replaced with the model class's name.

        Args:
            model_class: The class whose source is read.

        Returns:
            The comments by field name.
        """
        try:
            filename = inspect.getsourcefile(model_class)
        except (TypeError, OSError):
            return {}
        if filename is None:
            return {}
        module = sys.modules.get(model_class.__module__)
        # A source file changed since it was read is read again - checked once for the module loaded.
        if module is None or FieldComments.checked_modules.get((filename,)) is not module:
            linecache.checkcache(filename)
            if module is not None:
                FieldComments.checked_modules[(filename,)] = module
        lines = linecache.getlines(filename, module.__dict__ if module is not None else None)
        last_line_by_first_line, first_lines_by_qualname = FieldComments.get_class_spans(filename, lines)
        first_line = getattr(model_class, "__firstlineno__", None)
        if first_line is None:
            # Before Python 3.13 a class doesn't know its first line - it is found by its qualified
            # name, unless the file defines several classes of that name.
            candidate_first_lines = first_lines_by_qualname.get(model_class.__qualname__, [])
            if len(candidate_first_lines) != 1:
                return {}
            first_line = candidate_first_lines[0]
        last_line = last_line_by_first_line.get(first_line)
        if last_line is None:
            return {}
        # Only model_class's own body, never an ancestor's - so every match belongs to model_class
        # itself, and the placeholder always resolves to model_class.__name__.
        source = "".join(lines[first_line - 1 : last_line])
        # Most classes have no comment at all.
        if FIELD_COMMENT_MARKER not in source:
            return {}
        comments = {}
        for comment_block, field_name in FIELD_COMMENT_RE.findall(source):
            comment = FIELD_COMMENT_MARKER_PATTERN.sub("", comment_block)
            comments[field_name] = comment.replace("{model}", model_class.__name__)

        return comments

    @staticmethod
    def get_class_spans(filename: str, lines: list[str]) -> tuple[dict[int, int], dict[str, list[int]]]:
        """The classes of a source file - parsed once per file, not per class.

        Args:
            filename: The file.
            lines: Its lines.

        Returns:
            Each class's last line by its first line (a decorator's, when it has one), and the first
            lines of the classes by qualified name.
        """
        key = (filename,)
        cached = FieldComments.source_class_spans.get(key)
        if cached is not None and cached[0] is lines:
            return cached[1], cached[2]
        last_line_by_first_line: dict[int, int] = {}
        first_lines_by_qualname: dict[str, list[int]] = {}
        source = "".join(lines)
        # A file without a comment has no class to find - it isn't parsed.
        tree = None
        if FIELD_COMMENT_MARKER in source:
            try:
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                pass
        # Statements only - a class is never defined inside an expression. Each statement comes with
        # the qualified name prefix of the scope it stands in.
        statements: list[tuple[ast.AST, str]] = [(node, "") for node in tree.body] if tree is not None else []
        while statements:
            node, scope_prefix = statements.pop()
            block_prefix = scope_prefix
            if isinstance(node, ast.ClassDef):
                block_prefix = f"{scope_prefix}{node.name}."
                if node.end_lineno is not None:
                    first_line = node.decorator_list[0].lineno if node.decorator_list else node.lineno
                    last_line_by_first_line[first_line] = node.end_lineno
                    first_lines_by_qualname.setdefault(f"{scope_prefix}{node.name}", []).append(first_line)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                block_prefix = f"{scope_prefix}{node.name}.<locals>."
            for block_field in STATEMENT_BLOCK_FIELDS:
                block = getattr(node, block_field, None)
                if block:
                    statements.extend((child, block_prefix) for child in block)
        FieldComments.source_class_spans[key] = (lines, last_line_by_first_line, first_lines_by_qualname)
        return last_line_by_first_line, first_lines_by_qualname
