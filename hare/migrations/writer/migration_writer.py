from __future__ import annotations

import importlib
import subprocess  # nosec B404 - runs ruff only
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from hare.declarations.declaration_value_serializer import DeclarationValueSerializer
from hare.declarations.import_manager import ImportManager
from hare.exceptions import ConfigurationError
from hare.migrations.operations import Operation
from hare.migrations.swappable_dependency import SwappableDependency
from hare.migrations.writer.constants import (
    MIGRATION_FORMATTER_RUFF_ARGUMENTS,
    MIGRATION_LINE_LENGTH,
    MIGRATION_MODULE_NAMES,
    MIGRATION_SLUG_MAX_LENGTH,
    MIGRATION_SLUG_RE,
    MIGRATION_UNDERSCORE_RUN_PATTERN,
)


class MigrationWriter:
    """Writes a migration file of operations."""

    @staticmethod
    def slugify_name(value: str) -> str:
        """The name of a migration file after its number - word characters joined by underscores, cut
        to ``MIGRATION_SLUG_MAX_LENGTH``.

        Args:
            value: The name given.

        Returns:
            The name; ``auto`` when nothing of it is left.
        """
        slug = unicodedata.normalize("NFKC", value).strip().lower().replace(" ", "_")
        slug = MIGRATION_SLUG_RE.sub("_", slug)
        slug = MIGRATION_UNDERSCORE_RUN_PATTERN.sub("_", slug).strip("_")
        slug = slug[:MIGRATION_SLUG_MAX_LENGTH].rstrip("_")
        return slug or "auto"

    @staticmethod
    def format_name(number: int, name: str) -> str:
        return f"{number:04d}_{MigrationWriter.slugify_name(name)}"

    @staticmethod
    def module_path(module_name: str) -> Path:
        module = importlib.import_module(module_name)
        if not hasattr(module, "__path__"):
            raise ConfigurationError(f"Migration module {module_name} is not a package")
        return Path(next(iter(module.__path__)))

    def __init__(
        self,
        name: str,
        app_label: str,
        operations: Iterable[Operation],
        *,
        dependencies: list[tuple[str, str]] | None = None,
        run_before: list[tuple[str, str]] | None = None,
        replaces: list[tuple[str, str]] | None = None,
        initial: bool | None = None,
        migrations_module: str | None = None,
        atomic: bool = True,
    ) -> None:
        self.name = name
        self.app_label = app_label
        self.operations = list(operations)
        self.dependencies = dependencies or []
        self.run_before = run_before or []
        self.replaces = replaces or []
        self.initial = initial
        self.migrations_module = migrations_module
        self.atomic = atomic

    def path(self) -> Path:
        if not self.migrations_module:
            raise ConfigurationError("migrations_module is required to resolve the output path")
        return self.module_path(self.migrations_module) / f"{self.name}.py"

    def write(self) -> Path:
        path = self.path()
        # LF on every platform.
        path.write_text(self.as_string(), encoding="utf-8", newline="\n")
        # A process importing the file right away would otherwise hit a stale directory listing.
        importlib.invalidate_caches()
        return path

    @staticmethod
    def format_files(paths: Iterable[Path]) -> None:
        """Sorts the imports of and formats written migration files with ruff, the way Django runs
        black on them - skipped when ruff isn't installed. A file ruff can't format is left as
        written.

        Args:
            paths: The written migration files.
        """
        file_names = [str(path) for path in paths]
        if not file_names:
            return
        ruff_executable = MigrationWriter.get_ruff_executable()
        if ruff_executable is None:
            return
        for ruff_arguments in MIGRATION_FORMATTER_RUFF_ARGUMENTS:
            subprocess.run(  # nosec B603 - the installed ruff executable and fixed ruff arguments
                [ruff_executable, *ruff_arguments, *file_names],
                capture_output=True,
                check=False,
            )

    @staticmethod
    def get_ruff_executable() -> str | None:
        """The executable of the installed ruff package - run directly, not through a second Python
        interpreter (``python -m ruff``), which would take several times as long to start.

        Returns:
            Its path, None when ruff isn't installed.
        """
        try:
            from ruff.__main__ import find_ruff_bin
        except ImportError:
            return None
        try:
            return find_ruff_bin()
        except FileNotFoundError:
            return None

    def as_string(self) -> str:
        """The source of the migration file.

        Returns:
            The source.
        """
        imports = ImportManager(reserved_names=MIGRATION_MODULE_NAMES)
        operations = []
        for operation in self.operations:
            operations.extend(self._format_operation(operation, imports, indent=" " * 8))

        lines: list[str] = [
            "from hare import migrations",
            "from hare.migrations import operations as ops",
        ]
        extra_imports = imports.render()
        if extra_imports:
            lines.extend(extra_imports)
        if imports.enum_declarations:
            colliding_names = sorted(imports.imported_names() & set(imports.enum_names.values()))
            if colliding_names:
                raise ConfigurationError(
                    f"The migration imports {colliding_names} and declares an enum of the same name - "
                    "rename one of the enums"
                )
            lines.extend(["", *imports.enum_declarations])
        if imports.function_declarations:
            colliding_names = sorted(imports.imported_names() & set(imports.function_names.values()))
            if colliding_names:
                raise ConfigurationError(
                    f"The migration imports {colliding_names} and declares a function of the same name - rename "
                    "one of the functions"
                )
            for function_declaration in imports.function_declarations:
                lines.extend(["", "", function_declaration])
        lines.extend(["", "class Migration(migrations.Migration):"])
        blocks: list[list[str]] = []
        if self.dependencies:
            blocks.append([f"    dependencies = {self._render_dependencies(self.dependencies)}"])
        if self.run_before:
            blocks.append([f"    run_before = {self.run_before!r}"])
        if self.replaces:
            blocks.append([f"    replaces = {self.replaces!r}"])
        if self.initial is not None:
            blocks.append([f"    initial = {self.initial!r}"])
        if not self.atomic:
            blocks.append(["    atomic = False"])
        blocks.append(["    operations = [", *operations, "    ]"])
        for position, block in enumerate(blocks):
            lines.extend(block)
            if position < len(blocks) - 1:
                lines.append("")
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_dependencies(dependencies: list[tuple[str, str]]) -> str:
        """Renders a migration's dependency list - a swappable one as
        ``migrations.swappable_dependency(setting)``.

        Args:
            dependencies: The (app_label, migration name) dependencies.
        """
        rendered_dependencies = [
            f"migrations.swappable_dependency({dependency.setting!r})"
            if isinstance(dependency, SwappableDependency)
            else repr(tuple(dependency))
            for dependency in dependencies
        ]
        return "[" + ", ".join(rendered_dependencies) + "]"

    @staticmethod
    def _format_operation(operation: Operation, imports: ImportManager, *, indent: str) -> list[str]:
        """An operation as source lines of the ``operations`` list - the call of its
        ``deconstruct()``: on one line when it fits, else its constructor and each argument
        on a line of its own, a non-empty list one item per line.

        Args:
            operation: The operation.
            imports: Collects the imports the arguments need.
            indent: The indentation of the call.

        Returns:
            The lines.
        """
        path, args, kwargs = operation.deconstruct()
        module_name, _, operation_name = path.rpartition(".")
        callee = (
            f"ops.{operation_name}"
            if module_name == "hare.migrations.operations"
            else DeclarationValueSerializer.get_import_reference(path, imports)
        )
        if not any(isinstance(value, list) and value for value in kwargs.values()):
            arguments = [DeclarationValueSerializer.render_value(arg, imports) for arg in args]
            arguments += [
                f"{name}={DeclarationValueSerializer.render_value(value, imports)}" for name, value in kwargs.items()
            ]
            line = f"{indent}{callee}({', '.join(arguments)}),"
            if len(line) <= MIGRATION_LINE_LENGTH and "\n" not in line:
                return [line]
        lines = [f"{indent}{callee}("]
        lines += [f"{indent}    {DeclarationValueSerializer.render_value(arg, imports)}," for arg in args]
        for name, value in kwargs.items():
            if isinstance(value, list) and value:
                lines.append(f"{indent}    {name}=[")
                lines += [
                    f"{indent}        {DeclarationValueSerializer.render_value(item, imports)}," for item in value
                ]
                lines.append(f"{indent}    ],")
            else:
                lines.append(f"{indent}    {name}={DeclarationValueSerializer.render_value(value, imports)},")
        lines.append(f"{indent}),")
        return lines
