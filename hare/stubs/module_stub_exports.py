from __future__ import annotations

import sys
from collections.abc import Iterator

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor
from libcst.helpers import get_full_name_for_node


class ModuleStubExports:
    """Retain explicit source reexports omitted by stubgen's unused-import filtering.

    Only direct ``from ... import Name as Name`` declarations are added. LibCST's
    import visitor preserves relative levels, places imports and avoids duplicates.
    Modules whose loaders provide no Python source retain stubgen's output.
    """

    @staticmethod
    def preserve(text: str, module_name: str) -> str:
        module = sys.modules[module_name]
        get_source = getattr(getattr(module, "__loader__", None), "get_source", None)
        source = None if get_source is None else get_source(module_name)
        if source is None:
            return text
        stub = cst.parse_module(text)
        existing = set(ModuleStubExports.get_reexports(stub))
        context = CodemodContext()
        for module_name, name, relative in ModuleStubExports.get_reexports(cst.parse_module(source)):
            if (module_name, name, relative) not in existing:
                AddImportsVisitor.add_needed_import(context, module_name, name, name, relative=relative)
                existing.add((module_name, name, relative))
        return AddImportsVisitor(context).transform_module(stub).code

    @staticmethod
    def get_reexports(module: cst.Module) -> Iterator[tuple[str, str, int]]:
        """Read direct explicit exports, retaining relative import levels.

        Relative imports need an explicit comparison because the import visitor
        cannot deduplicate them without converting them to absolute imports.
        """
        for statement in module.body:
            if not isinstance(statement, cst.SimpleStatementLine):
                continue
            for item in statement.body:
                if not isinstance(item, cst.ImportFrom) or isinstance(item.names, cst.ImportStar):
                    continue
                for alias in item.names:
                    name = get_full_name_for_node(alias.name)
                    if (
                        name is not None
                        and alias.asname is not None
                        and get_full_name_for_node(alias.asname.name) == name
                    ):
                        module_name = "" if item.module is None else get_full_name_for_node(item.module) or ""
                        yield module_name, name, len(item.relative)
