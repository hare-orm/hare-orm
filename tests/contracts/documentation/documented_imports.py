from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

from tests.contracts.constants import DOCUMENTATION_DIRECTORY, DOCUMENTATION_PYTHON_BLOCK_PATTERN


@dataclasses.dataclass(frozen=True)
class DocumentedImport:
    """One name the documentation imports from hare.

    Attributes:
        module: The module.
        name: The name taken from it, None for ``import module``.
        files: The documents importing it, relative to the documentation directory.
    """

    module: str
    name: str | None
    files: tuple[str, ...]

    @property
    def identifier(self) -> str:
        """The import as the test's id."""
        return f"{self.module}:{self.name}" if self.name else self.module


class DocumentedImports:
    """The imports from hare in the python blocks of the documentation, found by reading the text."""

    @staticmethod
    def get_blocks(text: str) -> list[str]:
        """The python code blocks of a document.

        Args:
            text: The document.

        Returns:
            Each block's source.
        """
        return DOCUMENTATION_PYTHON_BLOCK_PATTERN.findall(text)

    @staticmethod
    def get_statements(block: str) -> list[ast.stmt]:
        """The import statements of a block - those of its parseable part when the block as a whole is
        pseudo-code.

        Args:
            block: The block's source.

        Returns:
            The statements.
        """
        try:
            tree = ast.parse(block)
        except SyntaxError:
            statements: list[ast.stmt] = []
            for match in re.finditer(r"^(?:from|import) hare[^\n(]*(?:\([^)]*\))?", block, re.MULTILINE):
                try:
                    statements.extend(ast.parse(match.group(0)).body)
                except SyntaxError:
                    continue
            return statements
        return [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]

    @classmethod
    def get_all(cls) -> list[DocumentedImport]:
        """Every distinct import from hare in the documentation.

        Returns:
            The imports, by module and name.
        """
        files_by_import: dict[tuple[str, str | None], list[str]] = {}
        for path in sorted(Path(DOCUMENTATION_DIRECTORY).rglob("*.md")):
            relative = path.relative_to(DOCUMENTATION_DIRECTORY).as_posix()
            for block in cls.get_blocks(path.read_text(encoding="utf-8")):
                for statement in cls.get_statements(block):
                    if isinstance(statement, ast.ImportFrom) and statement.module and not statement.level:
                        keys = [(statement.module, alias.name) for alias in statement.names]
                    elif isinstance(statement, ast.Import):
                        keys = [(alias.name, None) for alias in statement.names]
                    else:
                        continue
                    for module, name in keys:
                        if module == "hare" or module.startswith("hare."):
                            files_by_import.setdefault((module, name), []).append(relative)
        return [
            DocumentedImport(module, name, tuple(dict.fromkeys(files)))
            for (module, name), files in sorted(
                files_by_import.items(), key=lambda item: (item[0][0], item[0][1] or "")
            )
        ]
