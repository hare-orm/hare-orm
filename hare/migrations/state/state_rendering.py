from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from hare.migrations.state.state_apps import StateApps

if TYPE_CHECKING:
    from hare.migrations.state.migration_environment import MigrationEnvironment
    from hare.migrations.state.model_state import ModelState


class StateRendering:
    """Publish complete historical graphs from pending state changes.

    Added declarations can disappear before their first rendering. Every other
    invalidated declaration is rebuilt on the next read. Failed preparation
    retains the published registry and all pending marks for a later retry.
    """

    __slots__ = ("added_keys", "apps", "changed_keys", "default_connections", "environment")

    def __init__(self, default_connections: dict[str, str], environment: MigrationEnvironment) -> None:
        self.default_connections = default_connections
        self.environment = environment
        self.apps: StateApps | None = None
        self.added_keys: set[tuple[str, str]] = set()
        self.changed_keys: set[tuple[str, str]] = set()

    def mark_added(self, key: tuple[str, str]) -> None:
        """Mark an added declaration, retaining invalidation after a removal or change."""
        if self.apps is not None and key not in self.changed_keys:
            self.added_keys.add(key)

    def mark_changed(self, key: tuple[str, str]) -> None:
        """Mark an existing declaration; newly added declarations are already invalidated."""
        if self.apps is None or key in self.added_keys:
            return
        self.changed_keys.add(key)

    def mark_removed(self, key: tuple[str, str]) -> None:
        """Cancel an unrendered addition or invalidate a previously rendered declaration."""
        if self.apps is None:
            return
        if key in self.added_keys:
            self.added_keys.discard(key)
            return
        self.changed_keys.add(key)

    def get_apps(self, model_states: Mapping[tuple[str, str], ModelState]) -> StateApps:
        """Build and atomically publish the state's complete model graph."""
        if self.apps is None or self.added_keys or self.changed_keys:
            reusable = {}
            if self.apps is not None:
                changed_model_keys = self.changed_keys | self.added_keys
                reusable = {
                    (label, name): model
                    for label, models in self.apps.apps.items()
                    for name, model in models.items()
                    if (label, name) not in changed_model_keys
                }
            prepared = StateApps.from_model_states(
                model_states,
                default_connections=self.default_connections,
                environment=self.environment,
                reusable_models=reusable,
            )
            self.apps = prepared
            self.added_keys.clear()
            self.changed_keys.clear()
        return self.apps
