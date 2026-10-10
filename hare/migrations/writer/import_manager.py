from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any

from hare.migrations.writer.constants import MIGRATION_MODULE_NAMES


@dataclass
class ImportManager:
    imports: dict[str, set[str]] = dataclass_field(default_factory=dict)
    modules: set[str] = dataclass_field(default_factory=set)
    uses_fields_module: bool = False
    uses_indexes: set[str] = dataclass_field(default_factory=set)
    uses_constraints: set[str] = dataclass_field(default_factory=set)
    #: The name each enum the migration declares itself (one it can't import) is declared under,
    #: by what the enum holds (``RuntimeEnums.get_content()``) - one declaration per such enum.
    enum_names: dict[tuple[Any, ...], str] = dataclass_field(default_factory=dict)
    #: The declarations of those enums, in the order they were met.
    enum_declarations: list[str] = dataclass_field(default_factory=list)
    #: The name each function the migration declares itself (one of a module it can't import - a
    #: migration's own) is declared under, by its module and qualified name.
    function_names: dict[tuple[str, str], str] = dataclass_field(default_factory=dict)
    #: The source of those functions, in the order they were met.
    function_declarations: list[str] = dataclass_field(default_factory=list)

    def add_from(self, module: str, name: str) -> None:
        self.imports.setdefault(module, set()).add(name)

    def add_module(self, module: str) -> None:
        self.modules.add(module)

    def add_fields_alias(self) -> None:
        self.uses_fields_module = True

    def add_index_class(self, name: str) -> None:
        self.uses_indexes.add(name)

    def add_constraint_class(self, name: str) -> None:
        self.uses_constraints.add(name)

    def merge(self, other: ImportManager) -> None:
        """Adds what ``other`` imports and declares, for one module rendered from several sources.

        Args:
            other: The other imports.
        """
        for module, names in other.imports.items():
            self.imports.setdefault(module, set()).update(names)
        self.modules |= other.modules
        self.uses_fields_module = self.uses_fields_module or other.uses_fields_module
        self.uses_indexes |= other.uses_indexes
        self.uses_constraints |= other.uses_constraints
        for content, name in other.enum_names.items():
            self.enum_names.setdefault(content, name)
        self.enum_declarations.extend(
            declaration for declaration in other.enum_declarations if declaration not in self.enum_declarations
        )
        for function_key, name in other.function_names.items():
            self.function_names.setdefault(function_key, name)
        self.function_declarations.extend(
            declaration for declaration in other.function_declarations if declaration not in self.function_declarations
        )

    def bound_names(self) -> set[str]:
        """Names the migration binds at its top: its imports, its enum and function declarations.

        Returns:
            Every such name.
        """
        return self.imported_names() | set(self.enum_names.values()) | set(self.function_names.values())

    def imported_names(self) -> set[str]:
        """Names the rendered import lines bind in the importing module's namespace.

        Returns:
            Every top-level name bound by render().
        """
        names = {module.split(".", 1)[0] for module in self.modules}
        for imported_names in self.imports.values():
            names.update(imported_names)
        if self.uses_fields_module:
            names.add("fields")
        names.update(self.uses_indexes)
        names.update(self.uses_constraints)
        return names

    def get_free_name(self, name: str) -> str:
        """A name no import or declaration of the migration binds yet.

        Args:
            name: The wanted name.

        Returns:
            ``name`` itself, else ``name`` with the first free number after it.
        """
        taken_names = self.bound_names() | MIGRATION_MODULE_NAMES
        if name not in taken_names:
            return name
        number = 2
        while f"{name}{number}" in taken_names:
            number += 1
        return f"{name}{number}"

    def render(self) -> list[str]:
        lines: list[str] = []
        lines.extend(f"import {module}" for module in sorted(self.modules))
        lines.extend(
            f"from {module} import {', '.join(sorted(names))}" for module, names in sorted(self.imports.items())
        )
        if self.uses_fields_module:
            lines.append("from hare import fields")
        if self.uses_indexes:
            index_names = ", ".join(sorted(self.uses_indexes))
            lines.append(f"from hare.ddl.indexes import {index_names}")
        if self.uses_constraints:
            constraint_names = ", ".join(sorted(self.uses_constraints))
            lines.append(f"from hare.ddl.constraints import {constraint_names}")
        return lines
