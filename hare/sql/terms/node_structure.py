from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import copy
from enum import Enum
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.sql.terms.messages import CYCLIC_SQL_TREE
from hare.sql.terms.structure_identity import StructureIdentity

if TYPE_CHECKING:
    from hare.sql.builder.queries.query_builder import QueryBuilder
    from hare.sql.builder.tables.declarations import SourceBinding
    from hare.sql.terms.node import Node


class NodeStructure:
    """Structural identity and rewriting of declared SQL node attributes.

    Declarations distinguish opaque literal payloads from expression inputs. A class
    must opt in explicitly; an unknown subclass is opaque even when a base class
    declares attributes. Source references stop at their binding and nested query
    bodies are never inspected by an enclosing expression traversal.
    """

    @staticmethod
    def get_source_bindings(query: QueryBuilder) -> dict[Any, SourceBinding]:
        """Bind references to the canonical source occurrences of one statement scope."""
        sources = [*query._from, *(join.item for join in query._joins)]
        return {source: source.source_binding for source in sources if "source_binding" in vars(source)}

    @staticmethod
    def get_attributes(node: Node, *, declaration: type[Node] | None = None) -> tuple[tuple[str, Any], ...] | None:
        """Read an explicit class's structural attributes, including its inherited declarations.

        By default the concrete class must opt in; an undeclared subclass is opaque. An explicit
        ``declaration`` lets a specialized traversal retain a known base class's contract.
        """
        declaration = type(node) if declaration is None else declaration
        if declaration.__dict__.get("structure_attributes") is None:
            return None
        names = dict.fromkeys(
            name
            for node_class in reversed(declaration.__mro__)
            for name in node_class.__dict__.get("structure_attributes") or ()
        )
        return tuple((name, getattr(node, name)) for name in names if hasattr(node, name))

    @staticmethod
    def get_key(root: Node, source_bindings: Mapping[Any, Any] | None = None) -> tuple[Any, ...]:
        """Describe a value without rendering SQL, ignoring presentation aliases.

        The key is flat, so hashing a deeply nested expression does not recurse.
        Unknown Python payloads and SQL nodes are distinguished by retained identity.
        """
        from hare.sql.terms.node import Node

        result: list[Any] = []
        ancestors: set[int] = set()
        stack: list[tuple[Any, bool, bool]] = [(root, False, False)]
        while stack:
            value, literal, leaving = stack.pop()
            if leaving:
                ancestors.remove(id(value))
                result.append(("end",))
                continue
            if isinstance(value, Node) and not literal:
                # A Field's table is a source reference, not an inline copy of its query.
                binding = vars(value).get("source_binding")
                if binding is not None:
                    if source_bindings is not None:
                        binding = source_bindings.get(value, binding)
                    result.append(("source", StructureIdentity(binding)))
                    continue
                attributes = NodeStructure.get_attributes(value)
                if attributes is None or value.is_subquery or not value.has_repeatable_value:
                    identity = vars(value).get("expression_identity", value)
                    result.append(("opaque", type(value), StructureIdentity(identity)))
                    continue
                result.append(("node", type(value)))
                children = [(item, name in value.literal_attributes, False) for name, item in attributes]
                # Attribute names distinguish optional settings that happen to share a value.
                entries: list[tuple[Any, bool, bool]] = []
                for (name, _), child in zip(attributes, children, strict=True):
                    entries.extend(((name, True, False), child))
            elif isinstance(value, (tuple, list)):
                result.append(("sequence", type(value), len(value)))
                entries = [(item, literal, False) for item in value]
            elif isinstance(value, dict):
                result.append(("mapping", type(value), len(value)))
                entries = [(item, literal, False) for pair in value.items() for item in pair]
            else:
                if isinstance(value, Enum):
                    result.append(("enum", type(value), value.name))
                elif value is None or type(value) in (str, bytes, bool, int):
                    result.append(("value", type(value), value))
                elif type(value) is float:
                    result.append(("float", value.hex()))
                else:
                    result.append(("identity", type(value), StructureIdentity(value)))
                continue
            if id(value) in ancestors:
                raise QueryError(CYCLIC_SQL_TREE)
            ancestors.add(id(value))
            stack.append((value, literal, True))
            stack.extend(reversed(entries))
        return tuple(result)

    @staticmethod
    def rebuild(node: Node, attributes: Mapping[str, Any]) -> Node:
        """Return a shallow node copy with its declared structural inputs replaced."""
        declared = NodeStructure.get_attributes(node)
        if declared is None or any(name not in dict(declared) for name in attributes):
            raise QueryError(f"{type(node).__name__} does not declare these structural attributes")
        rebuilt = copy(node)
        vars(rebuilt).pop("_hash_cache", None)
        for name, value in attributes.items():
            setattr(rebuilt, name, value)
        return rebuilt

    @staticmethod
    def get_table_replacement_attributes(node: Node) -> tuple[tuple[str, Any], ...] | None:
        """Read the nearest declared base, preserving inherited table-replacement behavior.

        An undeclared Function subclass still rewrites its known arguments, while its extra state
        stays opaque. This does not relax structural identity or the default rewrite protocol.
        """
        declaration = next(
            (base for base in type(node).__mro__ if base.__dict__.get("structure_attributes") is not None),
            None,
        )
        return None if declaration is None else NodeStructure.get_attributes(node, declaration=declaration)

    @staticmethod
    def rebuild_table_references(node: Node, attributes: Mapping[str, Any]) -> Node:
        """Finalize table replacement, respecting mutable builders and invalidating ancestor hashes.

        An unchanged child identity can hide an in-place rewrite. Clear the parent's cached hash
        even then; copy only a node whose structural references need replacing.
        """
        if not attributes:
            return node
        if all(getattr(node, name) is value for name, value in attributes.items()):
            vars(node).pop("_hash_cache", None)
            return node
        rebuilt = copy(node) if getattr(node, "immutable", True) else node
        vars(rebuilt).pop("_hash_cache", None)
        for name, value in attributes.items():
            setattr(rebuilt, name, value)
        return rebuilt

    @staticmethod
    def rewrite(
        root: Node,
        replace_node: Callable[[Node], Node | None],
        *,
        get_attributes: Callable[[Node], tuple[tuple[str, Any], ...] | None] | None = None,
        rebuild_node: Callable[[Node, Mapping[str, Any]], Node] | None = None,
    ) -> Node:
        """Rewrite declared children iteratively; a returned node stops descent at that boundary.

        Returning None visits a node's declared inputs. Literal payloads, sources and
        nested query bodies stay opaque. Unchanged containers and nodes retain identity.
        ``get_attributes`` can select an explicit declaration boundary. A ``rebuild_node`` hook
        finalizes every visited structural node, including one whose children retain identity:
        a mutable child can have changed in place. Without hooks the strict copy-on-change
        behavior is preserved.
        """
        from hare.sql.terms.node import Node

        read_attributes = NodeStructure.get_attributes if get_attributes is None else get_attributes
        results: list[Any] = []
        ancestors: set[int] = set()
        stack: list[tuple[Any, Any]] = [(root, None)]
        while stack:
            value, frame = stack.pop()
            if frame is not None:
                names, children = frame
                rewritten = results[-len(children) :] if children else []
                if children:
                    del results[-len(children) :]
                ancestors.remove(id(value))
                if isinstance(value, Node) and rebuild_node is not None:
                    results.append(rebuild_node(value, dict(zip(names, rewritten, strict=True))))
                elif all(left is right for left, right in zip(children, rewritten, strict=True)):
                    results.append(value)
                elif isinstance(value, Node):
                    results.append(value.rebuild_structure(dict(zip(names, rewritten, strict=True))))
                elif isinstance(value, tuple):
                    results.append(tuple(rewritten))
                elif isinstance(value, dict):
                    results.append(dict(zip(names, rewritten, strict=True)))
                else:
                    results.append(list(rewritten))
                continue
            if isinstance(value, Node):
                replacement = replace_node(value)
                if replacement is not None:
                    results.append(replacement)
                    continue
                attributes = read_attributes(value)
                if attributes is None or value.is_subquery or "source_binding" in vars(value):
                    results.append(value)
                    continue
                inputs = [(name, item) for name, item in attributes if name not in value.literal_attributes]
                names = [name for name, _ in inputs]
                children = [item for _, item in inputs]
            elif isinstance(value, (list, tuple)):
                names = []
                children = list(value)
            elif isinstance(value, dict):
                names = list(value)
                children = list(value.values())
            else:
                results.append(value)
                continue
            if id(value) in ancestors:
                raise QueryError(CYCLIC_SQL_TREE)
            ancestors.add(id(value))
            stack.append((value, (names, children)))
            stack.extend((child, None) for child in reversed(children))
        return results[0]
