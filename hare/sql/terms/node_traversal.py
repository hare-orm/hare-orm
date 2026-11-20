from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING

from hare.exceptions import QueryError
from hare.sql.terms.messages import CYCLIC_SQL_TREE

if TYPE_CHECKING:
    from hare.sql.terms.node import Node


class NodeTraversal:
    """Iterative traversal of direct children with an explicit boundary predicate.

    Shared subexpressions are visited at each occurrence. Only ancestor cycles are
    rejected; a shared value is not mistaken for a cycle. Literals and opaque SQL expose
    no structural children beyond their explicitly declared parameter nodes.
    """

    @staticmethod
    def walk(root: Node, descend: Callable[[Node], bool] | None = None) -> Iterator[Node]:
        ancestors: set[int] = set()
        stack: list[tuple[Node, bool]] = [(root, False)]
        while stack:
            node, leaving = stack.pop()
            identity = id(node)
            if leaving:
                ancestors.remove(identity)
                continue
            if identity in ancestors:
                raise QueryError(CYCLIC_SQL_TREE)
            yield node
            if descend is not None and not descend(node):
                continue
            children = tuple(node.get_children())
            if not children:
                continue
            ancestors.add(identity)
            stack.append((node, True))
            stack.extend((child, False) for child in reversed(children))
