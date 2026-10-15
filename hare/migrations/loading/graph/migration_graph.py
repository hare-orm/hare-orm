from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.migrations.constants import UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE
from hare.migrations.exceptions import CircularDependencyError, MigrationLoadError, UnknownMigrationError

if TYPE_CHECKING:
    from hare.migrations.migration import Migration
from hare.migrations.loading.graph.dummy_node import DummyNode
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.graph.node import Node


class MigrationGraph:
    def __init__(self) -> None:
        self.node_map: dict[MigrationKey, Node] = {}
        self.nodes: dict[MigrationKey, Migration | None] = {}

    def add_node(self, key: MigrationKey, migration: Migration) -> None:
        if key in self.node_map:
            raise MigrationLoadError(f"Duplicate migration node {key}")
        node = Node(key)
        self.node_map[key] = node
        self.nodes[key] = migration

    def add_dummy_node(self, key: MigrationKey, origin: MigrationKey, error_message: str) -> None:
        node = DummyNode(key, origin, error_message)
        self.node_map[key] = node
        self.nodes[key] = None

    def add_dependency(
        self,
        migration: MigrationKey,
        child: MigrationKey,
        parent: MigrationKey,
        *,
        skip_validation: bool = False,
    ) -> None:
        if child not in self.nodes:
            self.add_dummy_node(
                child,
                migration,
                f"Migration {migration} references nonexistent child {child}",
            )
        if parent not in self.nodes:
            self.add_dummy_node(
                parent,
                migration,
                f"Migration {migration} references nonexistent parent {parent}",
            )
        self.node_map[child].add_parent(self.node_map[parent])
        self.node_map[parent].add_child(self.node_map[child])
        if not skip_validation:
            self.validate_consistency()

    def remove_replaced_nodes(self, replacement: MigrationKey, replaced: list[MigrationKey]) -> None:
        """Leaves the replaced migrations out of the graph: a dependency on any of them becomes one on
        the replacement, and the replacement depends on what they depended on outside themselves.

        Args:
            replacement: The squashed migration.
            replaced: The migrations it replaces - a missing one (its file deleted) included.
        """
        replacement_node = self.node_map[replacement]
        replaced_set = set(replaced)
        for replaced_key in replaced:
            replaced_node = self.node_map.pop(replaced_key, None)
            self.nodes.pop(replaced_key, None)
            if replaced_node is None:
                continue
            for child in replaced_node.children:
                child.parents.discard(replaced_node)
                if child.key not in replaced_set and child is not replacement_node:
                    replacement_node.add_child(child)
                    child.add_parent(replacement_node)
            for parent in replaced_node.parents:
                parent.children.discard(replaced_node)
                if parent.key not in replaced_set and parent is not replacement_node:
                    replacement_node.add_parent(parent)
                    parent.add_child(replacement_node)

    def remove_replacement_node(self, replacement: MigrationKey, replaced: list[MigrationKey]) -> None:
        """Leaves the squashed migration out of the graph - some of the migrations it replaces are
        applied, so they run on their own: a dependency on the replacement becomes one on the last
        of them.

        Args:
            replacement: The squashed migration.
            replaced: The migrations it replaces.
        """
        self.nodes.pop(replacement, None)
        replacement_node = self.node_map.pop(replacement)
        replaced_nodes = {self.node_map[key] for key in replaced if key in self.node_map}
        parents_of_replaced = {parent for node in replaced_nodes for parent in node.parents}
        last_replaced_nodes = [node for node in replaced_nodes if node not in parents_of_replaced]
        for child in replacement_node.children:
            child.parents.discard(replacement_node)
            for last_replaced_node in last_replaced_nodes:
                child.add_parent(last_replaced_node)
                last_replaced_node.add_child(child)
        for parent in replacement_node.parents:
            parent.children.discard(replacement_node)

    def validate_consistency(self) -> None:
        for node in self.node_map.values():
            if isinstance(node, DummyNode):
                node.raise_error()
        self._raise_if_cyclic()

    def _raise_if_cyclic(self) -> None:
        """Raises on a cycle, walking from every node - a cycle can leave no leaf node to start from."""
        for node in self.node_map.values():
            self._iterative_dfs([node], forwards=True)

    def root_nodes(self, app_label: str | None = None) -> list[MigrationKey]:
        """Every node with no parent - the first migrations of ``app_label``, or of the whole graph.
        With ``app_label`` only a parent of the same app counts: a dependency on another app's
        migration doesn't make a migration any less the app's first.
        """
        nodes = [
            node.key
            for node in self.node_map.values()
            if (app_label is None or node.key.app_label == app_label)
            and not self._has_parent_in_scope(node, app_label)
        ]
        return sorted(nodes)

    def leaf_nodes(self, app_label: str | None = None) -> list[MigrationKey]:
        """Every node with no child - the latest migrations of ``app_label``, or of the whole graph.
        With ``app_label`` only a child of the same app counts.
        """
        nodes = [
            node.key
            for node in self.node_map.values()
            if (app_label is None or node.key.app_label == app_label) and not self._has_child_in_scope(node, app_label)
        ]
        return sorted(nodes)

    @staticmethod
    def _has_parent_in_scope(node: Node, app_label: str | None) -> bool:
        if app_label is None:
            return bool(node.parents)
        return any(parent.key.app_label == app_label for parent in node.parents)

    @staticmethod
    def _has_child_in_scope(node: Node, app_label: str | None) -> bool:
        if app_label is None:
            return bool(node.children)
        return any(child.key.app_label == app_label for child in node.children)

    def get_single_leaf(self, app_label: str) -> MigrationKey | None:
        """Returns the sole leaf node for `app_label`, or None if it has no migrations at all.

        Raises:
            ConfigurationError: If the app's migration history has forked into more than one
                head with no merge migration between them yet - "the latest migration" is
                genuinely ambiguous in that case, the same way Django's own migration executor
                refuses to guess through it rather than silently applying every branch.
        """
        leaves = self.leaf_nodes(app_label)
        if len(leaves) > 1:
            heads = ", ".join(str(leaf) for leaf in leaves)
            raise ConfigurationError(
                f"Conflicting migrations detected for app '{app_label}': found multiple leaf "
                f"migrations ({heads}) with no merge migration between them. Create a migration "
                "that depends on all of them to resolve the conflict before migrating."
            )
        return leaves[0] if leaves else None

    def full_forwards_plan(self) -> list[MigrationKey]:
        """Every migration of the graph, in an order that applies each after its dependencies -
        the forwards plans of all leaves, each migration once.

        Returns:
            The migration keys.
        """
        plan: list[MigrationKey] = []
        seen: set[MigrationKey] = set()
        for leaf in self.leaf_nodes():
            for key in self.forwards_plan(leaf):
                if key not in seen:
                    seen.add(key)
                    plan.append(key)
        return plan

    def forwards_plan(self, target: MigrationKey) -> list[MigrationKey]:
        if target not in self.nodes:
            raise UnknownMigrationError(UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE.format(target=target))
        return self._iterative_dfs([self.node_map[target]], forwards=True)

    def backwards_plan(self, target: MigrationKey) -> list[MigrationKey]:
        if target not in self.nodes:
            raise UnknownMigrationError(UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE.format(target=target))
        return self._iterative_dfs([self.node_map[target]], forwards=False)

    def same_app_children(self, target: MigrationKey) -> list[MigrationKey]:
        """Direct dependents of ``target`` that belong to the same app, sorted."""
        if target not in self.nodes:
            raise UnknownMigrationError(UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE.format(target=target))
        return sorted(child.key for child in self.node_map[target].children if child.key.app_label == target.app_label)

    def backwards_plan_for_all(self, targets: list[MigrationKey]) -> list[MigrationKey]:
        """Rollback order covering every target and all of their dependents, dependents first."""
        for target in targets:
            if target not in self.nodes:
                raise UnknownMigrationError(UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE.format(target=target))
        return self._iterative_dfs([self.node_map[target] for target in targets], forwards=False)

    @staticmethod
    def _iterative_dfs(starts: list[Node], *, forwards: bool) -> list[MigrationKey]:
        visited: list[MigrationKey] = []
        visited_set: set[Node] = set()
        in_progress: set[Node] = set()
        stack: list[tuple[Node, bool]] = [(start, False) for start in reversed(starts)]
        while stack:
            node, processed = stack.pop()
            if node in visited_set:
                continue
            if processed:
                in_progress.discard(node)
                visited_set.add(node)
                visited.append(node.key)
                continue
            if node in in_progress:
                raise CircularDependencyError(f"Circular dependency detected in migration graph at {node.key}")
            in_progress.add(node)
            stack.append((node, True))
            neighbors = node.parents if forwards else node.children
            stack.extend((neighbor, False) for neighbor in sorted(neighbors))
        return visited
