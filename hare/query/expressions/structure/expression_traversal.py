from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

from hare.exceptions import QueryError
from hare.query.expressions.structure.declarations import ExpressionChild, FieldReference
from hare.query.expressions.structure.enums import ArgumentRole
from hare.query.expressions.structure.expression_declaration import ExpressionDeclaration
from hare.query.expressions.structure.messages import CYCLIC_EXPRESSION_TREE


class ExpressionTraversal:
    """Transform declared references without guessing from their string values.

    The iterative postorder walk rebuilds only changed branches. Shared children
    are visited per occurrence; a reference back to an ancestor is rejected.
    Literal expressions and raw SQL are opaque. A nested query starts a distinct
    source scope and is not rewritten as a declaration of the surrounding model.
    """

    @staticmethod
    def map_fields(
        value: Any, transform: Callable[[FieldReference], str], role: ArgumentRole = ArgumentRole.VALUE
    ) -> Any:
        """Map references in one source scope and preserve unchanged object identity."""
        pending: list[tuple[Any, ArgumentRole, tuple[ExpressionChild, ...] | None]] = [(value, role, None)]
        ancestors: set[int] = set()
        results: list[Any] = []
        while pending:
            item, item_role, children = pending.pop()
            if children is not None:
                transformed = results[-len(children) :]
                del results[-len(children) :]
                ancestors.remove(id(item))
                if all(child.value is result for child, result in zip(children, transformed, strict=True)):
                    results.append(item)
                else:
                    results.append(ExpressionTraversal.rebuild(item, children, transformed))
                continue
            if isinstance(item, str):
                results.append(ExpressionTraversal.map_reference(item, item_role, transform))
                continue
            children = ExpressionTraversal.get_children(item, item_role)
            if not children:
                results.append(item)
                continue
            if id(item) in ancestors:
                raise QueryError(CYCLIC_EXPRESSION_TREE)
            ancestors.add(id(item))
            pending.append((item, item_role, children))
            pending.extend((child.value, child.role, None) for child in reversed(children))
        return results[0]

    @staticmethod
    def map_reference(value: str, role: ArgumentRole, transform: Callable[[FieldReference], str]) -> str:
        """Interpret only strings in declared field, ordering or lookup positions."""
        if (
            role not in (ArgumentRole.FIELD, ArgumentRole.OUTER_FIELD, ArgumentRole.ORDERING, ArgumentRole.LOOKUP)
            or value == "*"
        ):
            return value
        prefix = "-" if role is ArgumentRole.ORDERING and value.startswith("-") else ""
        path = value[len(prefix) :]
        renamed = transform(FieldReference(path, int(role is ArgumentRole.OUTER_FIELD), role is ArgumentRole.LOOKUP))
        return value if renamed == path else prefix + renamed

    @staticmethod
    def get_children(value: Any, role: ArgumentRole) -> tuple[ExpressionChild, ...]:
        """Read direct declared children; containers inherit their parent's role."""
        # A lookup accepts a direct expression, but a container is one literal
        # passed to the field's encoder, never a collection of expressions.
        if role is ArgumentRole.FILTER_VALUE and isinstance(value, (dict, list, tuple, set)):
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(ExpressionChild(index, item, role) for index, item in enumerate(value))
        if isinstance(value, dict):
            key_role = (
                ArgumentRole.LOOKUP
                if role is ArgumentRole.FILTERS
                else ArgumentRole.FIELD
                if role is ArgumentRole.FIELD_MAPPING
                else ArgumentRole.VALUE
            )
            value_role = (
                ArgumentRole.FILTER_VALUE
                if role is ArgumentRole.FILTERS
                else ArgumentRole.VALUE
                if role is ArgumentRole.FIELD_MAPPING
                else role
            )
            return tuple(
                child
                for index, (key, item) in enumerate(value.items())
                for child in (
                    ExpressionChild(2 * index, key, key_role),
                    ExpressionChild(2 * index + 1, item, value_role),
                )
            )
        # These classes import the declaration protocol themselves.
        from hare.query.expressions.conditions.q import Q
        from hare.query.expressions.ordering import Ordering

        if isinstance(value, ExpressionDeclaration):
            return () if value.structure.query_scope else value.get_expression_children()
        if isinstance(value, (Q, Ordering)):
            return value.get_expression_children()
        return ()

    @staticmethod
    def rebuild(value: Any, children: tuple[ExpressionChild, ...], transformed: list[Any]) -> Any:
        """Rebuild a container or delegate derived state to the declaration."""
        if isinstance(value, (list, tuple)):
            return type(value)(transformed)
        if isinstance(value, dict):
            return dict(zip(transformed[::2], transformed[1::2], strict=True))
        return value.rebuild_expression(
            tuple(replace(child, value=result) for child, result in zip(children, transformed, strict=True))
        )

    @staticmethod
    def walk(value: Any) -> Iterator[Any]:
        """Visit declarations, including scope boundaries, without entering nested queries."""
        pending = [(value, ArgumentRole.VALUE, False)]
        ancestors: set[int] = set()
        while pending:
            item, role, leaving = pending.pop()
            if leaving:
                ancestors.remove(id(item))
                continue
            if id(item) in ancestors:
                raise QueryError(CYCLIC_EXPRESSION_TREE)
            yield item
            children = ExpressionTraversal.get_children(item, role)
            if children:
                ancestors.add(id(item))
                pending.append((item, role, True))
                pending.extend((child.value, child.role, False) for child in reversed(children))

    @staticmethod
    def get_field_references(value: Any) -> tuple[FieldReference, ...]:
        """Collect current and explicitly outer references without changing declarations."""
        references: list[FieldReference] = []

        def collect(reference: FieldReference) -> str:
            references.append(reference)
            return reference.path

        ExpressionTraversal.map_fields(value, collect)
        return tuple(references)
