from __future__ import annotations

from collections.abc import Iterable, Reversible
from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.migrations.constants import MISSING_MIGRATION_MESSAGE_TEMPLATE, UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE
from hare.migrations.exceptions import CircularDependencyError, MigrationLoadError, UnknownMigrationError
from hare.migrations.loading.graph.dummy_node import DummyNode
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.graph.node import Node

if TYPE_CHECKING:
    from hare.migrations.migration import Migration
    from hare.migrations.state.state import State


class MigrationGraph:
    """The migrations and their dependencies - the plans and the heads of each app."""

    def __init__(self) -> None:
        self.node_map: dict[MigrationKey, Node] = {}

    async def replay_state(self, keys: Iterable[MigrationKey], state: State) -> State:
        """Return the state after applying the selected migrations in order, without issuing DDL.

        Args:
            keys: The migration keys in execution order.
            state: The starting state.

        Raises:
            UnknownMigrationError: A selected node has no migration.
        """
        for key in keys:
            migration = self.node_map[key].migration
            if migration is None:
                raise UnknownMigrationError(MISSING_MIGRATION_MESSAGE_TEMPLATE.format(key=key))
            state = await migration.apply(state, dry_run=True, schema_editor=None)
        return state

    def get_migration_key(self, app_label: str, name: str) -> MigrationKey:
        """Find a migration by its full name or a unique prefix within one app.

        An exact name takes precedence over longer names sharing its prefix.

        Raises:
            UnknownMigrationError: No migration matches, or the prefix is ambiguous.
        """
        exact_key = MigrationKey(app_label=app_label, name=name)
        if exact_key in self.node_map:
            return exact_key
        matches = [key for key in self.node_map if key.app_label == app_label and key.name.startswith(name)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            names = ", ".join(key.name for key in matches)
            raise UnknownMigrationError(f"Ambiguous migration name {name!r} for app {app_label!r}. Matches: {names}")
        raise UnknownMigrationError(f"Cannot find migration {name!r} in app {app_label!r}")

    def add_node(self, key: MigrationKey, migration: Migration) -> None:
        if key in self.node_map:
            raise MigrationLoadError(f"Duplicate migration node {key}")
        self.node_map[key] = Node(key, migration)

    def add_dummy_node(self, key: MigrationKey, error_message: str) -> None:
        self.node_map[key] = DummyNode(key, error_message)

    def add_dependency(
        self,
        migration: MigrationKey,
        child: MigrationKey,
        parent: MigrationKey,
        *,
        skip_validation: bool = False,
    ) -> None:
        """Records that a migration depends on another - a migration missing from disk takes a node failing
        when the graph is checked.

        Args:
            migration: The migration declaring the dependency.
            child: The migration depending.
            parent: The migration depended on.
            skip_validation: Whether the graph is left unchecked, for many dependencies added at once.
        """
        if child not in self.node_map:
            self.add_dummy_node(child, f"Migration {migration} references nonexistent child {child}")
        if parent not in self.node_map:
            self.add_dummy_node(parent, f"Migration {migration} references nonexistent parent {parent}")
        self.node_map[child].add_parent(self.node_map[parent])
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
            if replaced_node is None:
                continue
            for child in replaced_node.children:
                child.parents.discard(replaced_node)
                if child.key not in replaced_set and child is not replacement_node:
                    child.add_parent(replacement_node)
            for parent in replaced_node.parents:
                parent.children.discard(replaced_node)
                if parent.key not in replaced_set and parent is not replacement_node:
                    replacement_node.add_parent(parent)

    def remove_replacement_node(self, replacement: MigrationKey, replaced: list[MigrationKey]) -> None:
        """Leaves the squashed migration out of the graph - some of the migrations it replaces are
        applied, so they run on their own: a dependency on the replacement becomes one on the last
        of them.

        Args:
            replacement: The squashed migration.
            replaced: The migrations it replaces.
        """
        replacement_node = self.node_map.pop(replacement)
        replaced_nodes = {self.node_map[key] for key in replaced if key in self.node_map}
        parents_of_replaced = {parent for node in replaced_nodes for parent in node.parents}
        last_replaced_nodes = [node for node in replaced_nodes if node not in parents_of_replaced]
        for child in replacement_node.children:
            child.parents.discard(replacement_node)
            for last_replaced_node in last_replaced_nodes:
                child.add_parent(last_replaced_node)
        for parent in replacement_node.parents:
            parent.children.discard(replacement_node)

    def validate_consistency(self) -> None:
        for node in self.node_map.values():
            if isinstance(node, DummyNode):
                node.raise_error()
        self._raise_if_cyclic()

    def _raise_if_cyclic(self) -> None:
        """Raises on a cycle, walking from every node - a cycle can leave no leaf node to start from."""
        self._iterative_dfs(self.node_map.values(), forwards=True)

    def root_nodes(self, app_label: str | None = None) -> list[MigrationKey]:
        """Every node with no parent - the first migrations of ``app_label``, or of the whole graph.
        With ``app_label`` only earlier nodes of that app count, including paths through other
        apps. A foreign dependency alone doesn't make a migration any less the app's first.
        """
        return self._get_boundary_nodes(app_label, forwards=True)

    def leaf_nodes(self, app_label: str | None = None) -> list[MigrationKey]:
        """Every node with no child - the latest migrations of ``app_label``, or of the whole graph.
        With ``app_label`` only later nodes of that app count, including paths through other apps.
        """
        return self._get_boundary_nodes(app_label, forwards=False)

    def _get_boundary_nodes(self, app_label: str | None, *, forwards: bool) -> list[MigrationKey]:
        """Return sorted keys with no predecessor or successor in the selected graph scope."""
        return sorted(
            node.key
            for node in self.node_map.values()
            if (app_label is None or node.key.app_label == app_label)
            and not self._get_scoped_neighbors(node, app_label, forwards=forwards)
        )

    @staticmethod
    def _get_scoped_neighbors(node: Node, app_label: str | None, *, forwards: bool) -> set[Node]:
        """Nearest nodes of an app, walking through foreign apps in either graph direction."""
        neighbors = node.parents if forwards else node.children
        if app_label is None:
            return neighbors
        result = set()
        visited = {node}
        pending = list(neighbors)
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            if current.key.app_label == app_label:
                result.add(current)
            else:
                pending.extend(current.parents if forwards else current.children)
        return result

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
                f"Conflicting migrations detected for app {app_label!r}: found multiple leaf "
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
        return self._iterative_dfs([self.node_map[leaf] for leaf in self.leaf_nodes()], forwards=True)

    def forwards_plan(self, target: MigrationKey) -> list[MigrationKey]:
        return self.forwards_plan_for_all([target])

    def backwards_plan(self, target: MigrationKey) -> list[MigrationKey]:
        return self.backwards_plan_for_all([target])

    def same_app_children(self, target: MigrationKey) -> list[MigrationKey]:
        """Nearest dependents in the same app, including paths through other apps, sorted."""
        node = self._get_target_node(target)
        return sorted(child.key for child in self._get_scoped_neighbors(node, target.app_label, forwards=False))

    def backwards_plan_for_all(self, targets: list[MigrationKey]) -> list[MigrationKey]:
        """Rollback order covering every target and all of their dependents, dependents first."""
        return self._iterative_dfs([self._get_target_node(target) for target in targets], forwards=False)

    def forwards_plan_for_all(self, targets: list[MigrationKey]) -> list[MigrationKey]:
        """Apply every target after its dependencies, visiting shared ancestors only once.

        Targets retain the caller's order, with the same dependency ordering as
        ``forwards_plan()``. Unknown targets are refused before traversal.
        """
        return self._iterative_dfs([self._get_target_node(target) for target in targets], forwards=True)

    def _get_target_node(self, target: MigrationKey) -> Node:
        try:
            return self.node_map[target]
        except KeyError:
            raise UnknownMigrationError(UNKNOWN_MIGRATION_TARGET_MESSAGE_TEMPLATE.format(target=target)) from None

    @staticmethod
    def _iterative_dfs(starts: Reversible[Node], *, forwards: bool) -> list[MigrationKey]:
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
