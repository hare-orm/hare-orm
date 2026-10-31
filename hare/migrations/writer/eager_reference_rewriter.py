from __future__ import annotations

import ast
from collections.abc import Mapping
from typing import cast


class EagerReferenceRewriter(ast.NodeTransformer):
    """Rewrites the names a function copied into a migration reads at definition time - its defaults and
    annotations - to the names the migration imports them under.

    Args:
        replacements: The migration's code of each name, by the name the function's module bound.
    """

    def __init__(self, replacements: Mapping[str, str]) -> None:
        self.replacements = replacements

    @staticmethod
    def get_dotted_name(node: ast.AST) -> str | None:
        """The dotted name a node reads (``module.attribute``).

        Args:
            node: A node of an expression.

        Returns:
            The name, None for a node that is no plain name or attribute of one.
        """
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            owner = EagerReferenceRewriter.get_dotted_name(node.value)
            return None if owner is None else f"{owner}.{node.attr}"
        return None

    def visit_Name(self, node: ast.Name) -> ast.expr:
        replacement = self.replacements.get(node.id)
        return node if replacement is None else ast.parse(replacement, mode="eval").body

    def visit_Lambda(self, node: ast.Lambda) -> ast.Lambda:
        """Rewrites defaults in the enclosing scope and the body in the lambda's parameter scope.

        Args:
            node: A lambda used in a function's declaration.

        Returns:
            The lambda with imported references rebound. Its parameters keep their names and
            shadow equally named imports, including the root of a dotted module import.
        """
        arguments = node.args
        arguments.defaults = [self.visit(default) for default in arguments.defaults]
        arguments.kw_defaults = [None if default is None else self.visit(default) for default in arguments.kw_defaults]
        parameter_names = {
            argument.arg
            for argument in (
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            )
            if argument is not None
        }
        replacements = {
            name: replacement
            for name, replacement in self.replacements.items()
            if name.partition(".")[0] not in parameter_names
        }
        node.body = EagerReferenceRewriter(replacements).visit(node.body)
        return node

    def visit_Attribute(self, node: ast.Attribute) -> ast.expr:
        dotted_name = self.get_dotted_name(node)
        if dotted_name is not None:
            for original, replacement in self.replacements.items():
                if dotted_name == original or dotted_name.startswith(f"{original}."):
                    return ast.parse(replacement + dotted_name[len(original) :], mode="eval").body
        return cast("ast.expr", self.generic_visit(node))
