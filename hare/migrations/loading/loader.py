import hashlib
import pkgutil
import sys
from importlib import import_module, invalidate_caches, reload
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.migrations.constants import FIRST_MIGRATION, LATEST_MIGRATION
from hare.migrations.exceptions import MigrationLoadError
from hare.migrations.loading.graph.migration_graph import MigrationGraph
from hare.migrations.loading.graph.migration_key import MigrationKey
from hare.migrations.loading.recorder.migration_recorder import MigrationRecorder
from hare.migrations.migration import Migration


class MigrationLoader:
    #: SHA-256 of each migration module's source as last executed in this process, keyed by
    #: dotted module path.
    MIGRATION_SOURCE_DIGESTS: dict[str, bytes] = {}

    def __init__(
        self,
        apps_config: dict[str, dict[str, Any]],
        recorder: MigrationRecorder,
        *,
        load: bool = False,
        ignore_no_migrations: bool = False,
    ) -> None:
        """
        Args:
            apps_config: Every configured app.
            recorder: Reads the applied migrations.
            load: Unsupported - build_graph() is async.
            ignore_no_migrations: Drop a "__first__" dependency on an app that has no migration
                yet instead of failing - makemigrations is about to write that app's first one.
        """
        self.apps_config = apps_config
        self.ignore_no_migrations = ignore_no_migrations
        self.recorder = recorder
        self.disk_migrations: dict[MigrationKey, Migration] = {}
        self.applied_migrations: set[MigrationKey] = set()
        self.unmigrated_apps: set[str] = set()
        self.migrated_apps: set[str] = set()
        self.graph = MigrationGraph()
        if load:
            raise RuntimeError("MigrationLoader.build_graph is async; call it explicitly.")

    def migrations_module(self, app_label: str) -> str | None:
        return self.apps_config.get(app_label, {}).get("migrations")

    def load_disk(self) -> None:
        self.disk_migrations = {}
        self.unmigrated_apps = set()
        self.migrated_apps = set()
        # Migration files added since the last load must be importable, not hidden by the
        # import system's cached directory listings.
        invalidate_caches()
        for app_label in self.apps_config:
            module_name = self.migrations_module(app_label)
            if not module_name:
                self.unmigrated_apps.add(app_label)
                continue

            was_loaded = module_name in sys.modules
            try:
                module = import_module(module_name)
            except ModuleNotFoundError as exc:
                raise MigrationLoadError(
                    f"Cannot import migrations module {module_name} for app {app_label}: {exc}"
                ) from exc
            else:
                if not hasattr(module, "__path__"):
                    self.unmigrated_apps.add(app_label)
                    continue
                if was_loaded:
                    reload(module)

            self.migrated_apps.add(app_label)
            migration_names = [
                name for _, name, is_pkg in pkgutil.iter_modules(module.__path__) if not is_pkg and name[0] not in "_~"
            ]
            for migration_name in migration_names:
                migration_path = f"{module_name}.{migration_name}"
                try:
                    # A migration file replays a schema that was already applied - fields it
                    # declares must load even if today's ForeignKeyField rules would reject them.
                    with ForeignKeyFieldInstance.replaying_migration_scope():
                        migration_module = self._import_migration_module(migration_path)
                except Exception as exc:
                    raise MigrationLoadError(
                        f"Cannot load migration {app_label}.{migration_name} "
                        f"(module {migration_path}): {type(exc).__name__}: {exc}"
                    ) from exc
                if not hasattr(migration_module, "Migration"):
                    raise MigrationLoadError(f"Migration {migration_name} in app {app_label} has no Migration class")
                migration_cls = migration_module.Migration
                migration_obj = migration_cls(migration_name, app_label)
                key = MigrationKey(app_label=app_label, name=migration_name)
                self.disk_migrations[key] = migration_obj

    @classmethod
    def _import_migration_module(cls, migration_path: str) -> ModuleType:
        """Imports a migration module, re-executing it when its source changed since this
        process last loaded it, so an edited migration file is picked up by a later load.

        Args:
            migration_path: Dotted path of the migration module.

        Returns:
            The imported module.
        """
        module = sys.modules.get(migration_path)
        if module is None:
            module = import_module(migration_path)
            source = cls._read_module_source(module)
            if source is not None:
                cls.MIGRATION_SOURCE_DIGESTS[migration_path] = hashlib.sha256(source).digest()
            return module
        source = cls._read_module_source(module)
        if source is None:
            return module
        source_digest = hashlib.sha256(source).digest()
        if cls.MIGRATION_SOURCE_DIGESTS.get(migration_path) == source_digest:
            return module
        # Compiled from the source directly - a cached .pyc is only validated by whole-second
        # mtime and size, so a same-size edit within the same second would otherwise be ignored.
        code = compile(source, cast("str", module.__file__), "exec")
        exec(code, module.__dict__)  # nosec B102
        cls.MIGRATION_SOURCE_DIGESTS[migration_path] = source_digest
        return module

    @staticmethod
    def _read_module_source(module: ModuleType) -> bytes | None:
        """Returns a module's current source bytes, or None for a module loaded without a
        readable .py file."""
        source_path = getattr(module, "__file__", None)
        if not source_path or not source_path.endswith(".py"):
            return None
        try:
            return Path(source_path).read_bytes()
        except OSError:
            return None

    async def build_graph(self) -> None:
        self.load_disk()
        self.graph = MigrationGraph()
        self.applied_migrations = set(await self.recorder.applied_migrations())

        for key, migration in self.disk_migrations.items():
            self.graph.add_node(key, migration)

        for key, migration in self.disk_migrations.items():
            self._add_internal_dependencies(key, migration)

        for key, migration in self.disk_migrations.items():
            self._add_external_dependencies(key, migration)

        self.graph.validate_consistency()

    def _check_key(self, key: MigrationKey, current_app: str) -> MigrationKey | None:
        if (key.name != FIRST_MIGRATION and key.name != LATEST_MIGRATION) or key in self.graph.nodes:
            return key
        if key.app_label == current_app:
            return None
        if key.app_label in self.unmigrated_apps:
            return None
        if key.app_label in self.migrated_apps:
            if key.name == FIRST_MIGRATION:
                try:
                    return self.graph.root_nodes(key.app_label)[0]
                except IndexError as exc:
                    if self.ignore_no_migrations:
                        return None
                    raise MigrationLoadError(f"Dependency on app with no migrations: {key.app_label}") from exc
            # get_single_leaf() raises for an unresolved fork instead of picking a branch.
            leaf_key = self.graph.get_single_leaf(key.app_label)
            if leaf_key is None:
                raise MigrationLoadError(f"Dependency on app with no migrations: {key.app_label}")
            return leaf_key
        raise MigrationLoadError(f"Dependency on unknown app: {key.app_label}")

    def _add_internal_dependencies(self, key: MigrationKey, migration: Migration) -> None:
        for parent in migration.dependencies:
            parent_key = MigrationKey(app_label=parent[0], name=parent[1])
            if parent_key.app_label == key.app_label and parent_key.name != FIRST_MIGRATION:
                self.graph.add_dependency(key, key, parent_key, skip_validation=True)

    def _add_external_dependencies(self, key: MigrationKey, migration: Migration) -> None:
        for parent in migration.dependencies:
            parent_key = MigrationKey(app_label=parent[0], name=parent[1])
            if key.app_label == parent_key.app_label:
                continue
            checked = self._check_key(parent_key, key.app_label)
            if checked is not None:
                self.graph.add_dependency(key, key, checked, skip_validation=True)
        for child in migration.run_before:
            child_key = MigrationKey(app_label=child[0], name=child[1])
            checked = self._check_key(child_key, key.app_label)
            if checked is not None:
                self.graph.add_dependency(key, checked, key, skip_validation=True)
