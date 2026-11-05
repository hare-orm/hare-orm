from __future__ import annotations

import ast
from collections import defaultdict
from importlib.util import resolve_name as absolute_import_name
from pathlib import Path


class ModuleUsage:
    """Declarations and import dependencies, parsed once for architecture assertions.

    Imports count as dependencies, including public re-exports. Qualified attribute reads
    identify members accessed through imported modules. Unrelated names and prose do not.
    This is an import index, not an interpreter or a Python type checker.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.trees = {path: ast.parse(path.read_text(encoding="utf-8")) for path in sorted(root.rglob("*.py"))}
        self.modules = {
            path: ".".join(path.relative_to(root.parent).with_suffix("").parts).removesuffix(".__init__")
            for path in self.trees
        }
        self.references: dict[str, set[Path]] = defaultdict(set)
        for path, tree in self.trees.items():
            module = self.modules[path]
            package = module if path.name == "__init__.py" else module.rpartition(".")[0]
            imports: dict[str, str] = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    source = node.module or ""
                    if node.level:
                        source = absolute_import_name("." * node.level + source, package)
                    for alias in node.names:
                        name = f"{source}.{alias.name}"
                        imports[alias.asname or alias.name] = name
                        self.references[name].add(path)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        imports[alias.asname or alias.name.partition(".")[0]] = (
                            alias.name if alias.asname else alias.name.partition(".")[0]
                        )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or not isinstance(node.ctx, ast.Load):
                    continue
                components = []
                value: ast.expr = node
                while isinstance(value, ast.Attribute):
                    components.append(value.attr)
                    value = value.value
                if isinstance(value, ast.Name) and value.id in imports:
                    self.references[".".join((imports[value.id], *reversed(components)))].add(path)

    def get_definitions(self, path: Path) -> list[str]:
        """The top-level classes and uppercase constants declared by a module."""
        names = []
        for node in self.trees[path].body:
            if isinstance(node, ast.ClassDef):
                names.append(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names.extend(target.id for target in targets if isinstance(target, ast.Name) and target.id.isupper())
        return names

    def get_attribute_reads(self, paths: list[Path], receivers: set[str]) -> set[str]:
        """Reads through the named variables or attributes, excluding unrelated receivers.

        This syntactic check follows the project's feature receiver names; it does not
        infer types or follow arbitrary aliases.
        """
        return {
            node.attr
            for path in paths
            for node in ast.walk(self.trees[path])
            if isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Load)
            and (
                isinstance(node.value, ast.Name)
                and node.value.id in receivers
                or isinstance(node.value, ast.Attribute)
                and node.value.attr in receivers
            )
        }

    def get_declared_strings(self, path: Path, names: set[str]) -> set[str]:
        """Literal strings in module or class declarations, including dictionary values.

        Names are relative to the module, such as ``UuidV7.required_feature``.
        Function locals, dictionary keys and computed expressions do not count.
        """
        if path not in self.trees:
            return set()
        values = set()
        for name in names:
            *classes, attribute = name.split(".")
            statements = self.trees[path].body
            for class_name in classes:
                statements = [
                    statement
                    for node in statements
                    if isinstance(node, ast.ClassDef) and node.name == class_name
                    for statement in node.body
                ]
            for node in statements:
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if not any(isinstance(target, ast.Name) and target.id == attribute for target in targets):
                    continue
                candidates = node.value.values if isinstance(node.value, ast.Dict) else [node.value]
                values.update(
                    value.value
                    for value in candidates
                    if isinstance(value, ast.Constant) and isinstance(value.value, str)
                )
        return values
