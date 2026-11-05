from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field as dataclass_field
from typing import Any

from hare.declarations.constants import DECLARATION_RESERVED_NAMES


@dataclass
class ImportManager:
    """Import and declaration names shared by generated model and migration modules.

    Callers reserve module-level names before serializing values. Each reference
    receives its final alias when first requested and keeps it throughout rendering.
    """

    reserved_names: frozenset[str] = DECLARATION_RESERVED_NAMES
    imports: dict[str, set[str]] = dataclass_field(default_factory=dict)
    #: The name an import binds where its own is taken by another module's - ``from b import f as f2``,
    #: by the module and the imported name.
    import_aliases: dict[tuple[str, str], str] = dataclass_field(default_factory=dict)
    modules: set[str] = dataclass_field(default_factory=set)
    module_references: dict[str, str] = dataclass_field(default_factory=dict)
    #: Additional import bindings needed to avoid capturing a copied function's locals.
    scoped_imports: dict[str, tuple[str, str | None]] = dataclass_field(default_factory=dict)
    uses_fields_module: bool = False
    fields_reference: str = "fields"
    uses_indexes: set[str] = dataclass_field(default_factory=set)
    uses_constraints: set[str] = dataclass_field(default_factory=set)
    #: The name each enum the migration declares itself (one it can't import) is declared under,
    #: by what the enum holds (``ComparableValues.get_enum_content()``) - one declaration per such enum.
    enum_names: dict[tuple[Any, ...], str] = dataclass_field(default_factory=dict)
    #: The declarations of those enums, in the order they were met.
    enum_declarations: list[str] = dataclass_field(default_factory=list)
    #: The enums the migration declares itself though it could import them - their members are part
    #: of the schema (the labels of a native enum type), kept as they were when it was written.
    snapshot_enum_types: set[type] = dataclass_field(default_factory=set)
    #: The name each function the migration declares itself (one of a module it can't import - a
    #: migration's own) is declared under, by its module and qualified name.
    function_names: dict[tuple[str, str], str] = dataclass_field(default_factory=dict)
    #: The source of those functions, in the order they were met.
    function_declarations: list[str] = dataclass_field(default_factory=list)

    def add_from(self, module: str, name: str) -> str:
        """Imports a name of a module.

        Args:
            module: The module.
            name: The name.

        Returns:
            The name the migration refers to it by - the name itself, else a free one it is imported as
            when another import or declaration binds it: two functions of one name from two modules are two
            values.
        """
        if self._has_from_import(module, name):
            return self.import_aliases.get((module, name), name)
        bound_name = self.get_free_name(name)
        self.imports.setdefault(module, set()).add(name)
        if bound_name != name:
            self.import_aliases[module, name] = bound_name
        return bound_name

    def _has_from_import(self, module: str, name: str) -> bool:
        """Whether a regular or grouped import already binds this object."""
        return (
            name in self.imports.get(module, ())
            or (module, name) in self.scoped_imports.values()
            or (module == "hare" and name == "fields" and self.uses_fields_module)
            or (module == "hare.ddl.indexes" and name in self.uses_indexes)
            or (module == "hare.ddl.constraints" and name in self.uses_constraints)
        )

    def add_module(self, module: str) -> str:
        """Imports a module and returns its reference, aliased when its root name is taken."""
        reference = self.module_references.get(module)
        if reference is not None:
            return reference
        root_name = module.split(".", 1)[0]
        shares_root = any(
            name.split(".", 1)[0] == root_name and self.module_references.get(name, name) == name
            for name in self.modules
        )
        reference = module
        if not shares_root and root_name in self.bound_names() | self.reserved_names:
            reference = self.get_free_name(root_name)
        self.modules.add(module)
        self.module_references[module] = reference
        return reference

    def add_scoped_import(self, module: str, name: str | None, excluded_names: Collection[str]) -> str:
        """Bind an imported object under a name free in both module and function scopes.

        A module reference is always a single name, including dotted modules, so it
        remains valid in an explicit ``global`` declaration. Existing bindings are
        reused when local parameters, comprehensions or nested functions do not hide them.
        """
        reference = (
            self.module_references.get(module)
            if name is None
            else self.import_aliases.get((module, name), name)
            if self._has_from_import(module, name)
            else None
        )
        if reference is not None and "." not in reference and reference not in excluded_names:
            return reference
        for alias, target in self.scoped_imports.items():
            if target == (module, name) and alias not in excluded_names:
                return alias
        alias = self.get_free_name(name or module.rsplit(".", 1)[-1], excluded_names=excluded_names)
        self.scoped_imports[alias] = (module, name)
        if name is None:
            self.module_references.setdefault(module, alias)
        else:
            self.import_aliases.setdefault((module, name), alias)
        return alias

    def add_fields_alias(self) -> str:
        if self.uses_fields_module:
            return self.fields_reference
        if self._has_from_import("hare", "fields"):
            return self.import_aliases.get(("hare", "fields"), "fields")
        name = self.get_free_name("fields")
        if name != "fields":
            self.import_aliases["hare", "fields"] = name
        self.uses_fields_module = True
        self.fields_reference = name
        return name

    def add_index_class(self, name: str) -> str:
        return self._add_grouped_import("hare.ddl.indexes", name, self.uses_indexes)

    def add_constraint_class(self, name: str) -> str:
        return self._add_grouped_import("hare.ddl.constraints", name, self.uses_constraints)

    def _add_grouped_import(self, module: str, name: str, names: set[str]) -> str:
        """Adds an import to a dedicated group, keeping earlier references to the same import."""
        if self._has_from_import(module, name):
            return self.import_aliases.get((module, name), name)
        bound_name = self.get_free_name(name)
        if bound_name != name:
            self.import_aliases[module, name] = bound_name
        names.add(name)
        return bound_name

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
        names = {self.module_references.get(module, module).split(".", 1)[0] for module in self.modules}
        for module, imported_names in self.imports.items():
            names.update(self.import_aliases.get((module, name), name) for name in imported_names)
        if self.uses_fields_module:
            names.add(self.import_aliases.get(("hare", "fields"), "fields"))
        names.update(self.import_aliases.get(("hare.ddl.indexes", name), name) for name in self.uses_indexes)
        names.update(self.import_aliases.get(("hare.ddl.constraints", name), name) for name in self.uses_constraints)
        return names | self.scoped_imports.keys()

    def get_imported_name_text(self, module: str, name: str) -> str:
        """An imported name as its import line writes it.

        Args:
            module: The module.
            name: The name.

        Returns:
            ``name``, or ``name as alias``.
        """
        alias = self.import_aliases.get((module, name))
        return name if alias is None else f"{name} as {alias}"

    def get_free_name(self, name: str, *, excluded_names: Collection[str] = ()) -> str:
        """A name no import or declaration of the migration binds yet.

        Args:
            name: The wanted name.
            excluded_names: Bindings in a scope where the generated reference is used.

        Returns:
            ``name`` itself, else ``name`` with the first free number after it.
        """
        taken_names = self.bound_names() | self.reserved_names | set(excluded_names)
        if name not in taken_names:
            return name
        number = 2
        while f"{name}{number}" in taken_names:
            number += 1
        return f"{name}{number}"

    def render(self) -> list[str]:
        """The import lines of the migration file, sorted.

        Returns:
            The lines.
        """
        lines: list[str] = []
        lines.extend(
            f"from {module} import {name}" + (f" as {alias}" if alias != name else "")
            if name is not None
            else f"import {module}" + (f" as {alias}" if alias != module else "")
            for alias, (module, name) in sorted(self.scoped_imports.items())
        )
        lines.extend(
            f"import {module}"
            + (f" as {self.module_references[module]}" if self.module_references.get(module, module) != module else "")
            for module in sorted(self.modules)
        )
        lines.extend(
            f"from {module} import {', '.join(sorted(self.get_imported_name_text(module, name) for name in names))}"
            for module, names in sorted(self.imports.items())
        )
        if self.uses_fields_module:
            lines.append(f"from hare import {self.get_imported_name_text('hare', 'fields')}")
        if self.uses_indexes:
            index_names = ", ".join(
                sorted(self.get_imported_name_text("hare.ddl.indexes", name) for name in self.uses_indexes)
            )
            lines.append(f"from hare.ddl.indexes import {index_names}")
        if self.uses_constraints:
            constraint_names = ", ".join(
                sorted(self.get_imported_name_text("hare.ddl.constraints", name) for name in self.uses_constraints)
            )
            lines.append(f"from hare.ddl.constraints import {constraint_names}")
        return lines
