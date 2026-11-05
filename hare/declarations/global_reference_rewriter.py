from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

import libcst as cst
from libcst.metadata import BuiltinScope, GlobalScope, MetadataWrapper, ScopeProvider


class GlobalReferenceRewriter(cst.CSTTransformer):
    """Rebind declaration references while preserving Python's local scopes.

    LibCST supplies bindings for parameters, nested functions and comprehensions.
    Only unresolved or module-level names are candidates; attribute names and
    keyword labels are not variable references. No source text is searched or replaced.
    """

    METADATA_DEPENDENCIES: ClassVar = (ScopeProvider,)

    def __init__(self, replacements: Mapping[str, str]) -> None:
        self.replacements = replacements
        self.global_names: set[str] = set()

    @classmethod
    def rewrite(cls, source: str, replacements: Mapping[str, str]) -> str:
        """Return source with global references bound to the supplied names."""
        return MetadataWrapper(cst.parse_module(source)).visit(cls(replacements)).code

    @classmethod
    def get_global_names(cls, source: str) -> frozenset[str]:
        """Names a copied function needs from its module, excluding local bindings."""
        visitor = cls({})
        MetadataWrapper(cst.parse_module(source)).visit(visitor)
        return frozenset(visitor.global_names)

    @staticmethod
    def get_local_names(source: str) -> frozenset[str]:
        """Bindings that must not capture newly introduced module references."""
        scopes = MetadataWrapper(cst.parse_module(source)).resolve(ScopeProvider).values()
        return frozenset(
            assignment.name
            for scope in set(scopes)
            if scope is not None and not isinstance(scope, (GlobalScope, BuiltinScope))
            for assignment in scope.assignments
        )

    def is_global_reference(self, node: cst.Name) -> bool:
        scope = self.get_metadata(ScopeProvider, node, None)
        return scope is not None and all(
            isinstance(assignment.scope, (GlobalScope, BuiltinScope)) for assignment in scope[node.value]
        )

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.BaseExpression:
        if not self.is_global_reference(original_node):
            return updated_node
        self.global_names.add(original_node.value)
        replacement = self.replacements.get(original_node.value)
        return updated_node if replacement is None else cst.Name(replacement)

    def leave_Global(self, original_node: cst.Global, updated_node: cst.Global) -> cst.Global:
        return updated_node.with_changes(
            names=[
                item.with_changes(name=cst.Name(self.replacements.get(item.name.value, item.name.value)))
                for item in updated_node.names
            ]
        )
