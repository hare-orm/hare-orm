from __future__ import annotations

from typing import Any

from hare.query.expressions.structure.declarations import FieldReference
from hare.query.expressions.structure.expression_traversal import ExpressionTraversal


class ExpressionFieldRenaming:
    """Read and rename model references using declared expression argument roles.

    Plain values and SQL text are opaque. References into an enclosing query are
    separate from the fields of the current model, even when their names match.
    Rebuilding goes through the expression constructor to refresh derived state.
    """

    @staticmethod
    def get_renamed_path(path: str, old_name: str, new_name: str) -> str:
        """Rename the first component of a field, relation or lookup path."""
        first, separator, rest = path.partition("__")
        return f"{new_name}{separator}{rest}" if first == old_name else path

    @staticmethod
    def get_renamed(value: Any, old_name: str, new_name: str) -> Any:
        """Return the transformed declaration, preserving unchanged branches."""

        def rename(reference: FieldReference) -> str:
            if reference.outer_scopes:
                return reference.path
            return ExpressionFieldRenaming.get_renamed_path(reference.path, old_name, new_name)

        return ExpressionTraversal.map_fields(value, rename)

    @staticmethod
    def get_all_read_field_names(value: Any) -> set[str]:
        """Return roots of references belonging to the current model scope."""
        return {
            reference.path.partition("__")[0]
            for reference in ExpressionTraversal.get_field_references(value)
            if not reference.outer_scopes
        }
