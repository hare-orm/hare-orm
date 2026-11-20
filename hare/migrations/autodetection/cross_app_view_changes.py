from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from hare.ddl.schema_objects.materialized_view import MaterializedView
from hare.ddl.schema_objects.view_creation_order import ViewCreationOrder
from hare.declarations.declaration_values import DeclarationValues
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.operation_sorter import OperationSorter
from hare.migrations.autodetection.split_migration import SplitMigration
from hare.migrations.operations import (
    AddMaterializedView,
    AddView,
    AlterMaterializedView,
    AlterView,
    AppOperation,
    RemoveMaterializedView,
    RemoveView,
    RenameMaterializedView,
    RenameView,
)
from hare.models.enums import ModelOption
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:
    from hare.ddl.schema_objects.view import View
    from hare.migrations.operations import HareOperation
    from hare.migrations.operations.operation import Operation
    from hare.migrations.state.state import State

type ViewKey = tuple[str | None, str | None, str]
type ViewEntry = tuple[tuple[str, str], View]
type ColumnKey = tuple[str | None, str, str]


class CrossAppViewChanges:
    """Keep DDL and the declared views it invalidates together on their database."""

    def __init__(
        self,
        old_state: State,
        new_state: State,
        connection_by_app: Mapping[str, str | None],
        changed_columns: Mapping[str, set[ColumnKey]],
    ) -> None:
        self.old_state = old_state
        self.new_state = new_state
        self.connection_by_app = connection_by_app
        self.old_views = self.get_views(old_state)
        self.new_views = self.get_views(new_state)
        self.changed_columns = changed_columns
        self.groups: dict[str, tuple[set[str], set[ViewKey]]] = {}
        self.changed_owners: dict[str, set[str]] = {}
        self.renamed_views: dict[ViewKey, ViewKey] = {}

    def get_view_renames(self, operations_by_app: Mapping[str, list[HareOperation]]) -> dict[ViewKey, ViewKey]:
        """Use the names paired by the ordinary declaration diff."""
        renames: dict[ViewKey, ViewKey] = {}
        for app, operations in operations_by_app.items():
            for operation in operations:
                if not isinstance(operation, (RenameView, RenameMaterializedView)) or operation.old_name is None:
                    continue
                model_key = (app, operation.model_name)
                old_model = self.old_state.models.get(model_key)
                new_model = self.new_state.models.get(model_key)
                if old_model is not None and new_model is not None:
                    alias = self.connection_by_app.get(app)
                    old_key = (alias, old_model.options.get(ModelOption.SCHEMA), operation.old_name)
                    new_key = (alias, new_model.options.get(ModelOption.SCHEMA), operation.new_name)
                    renames[old_key] = new_key
        return renames

    def get_new_key(self, old_key: ViewKey) -> ViewKey:
        return self.renamed_views.get(old_key, old_key)

    def get_views(self, state: State) -> dict[ViewKey, ViewEntry]:
        views: dict[ViewKey, ViewEntry] = {}
        for model_key, model in state.models.items():
            if model.options.get(ModelOption.MANAGED) is False:
                continue
            alias = self.connection_by_app.get(model_key[0])
            schema = model.options.get(ModelOption.SCHEMA)
            for option in (ModelOption.VIEWS, ModelOption.MATERIALIZED_VIEWS):
                for view in model.get_option_list(option):
                    views[(alias, schema, view.name)] = (model_key, view)
        return views

    def get_affected_apps(self) -> dict[ViewKey, set[str]]:
        changed = {
            (self.connection_by_app.get(app), schema, table, column): app
            for app, columns in self.changed_columns.items()
            for schema, table, column in columns
        }
        causes: dict[ViewKey, set[str]] = {}
        dependencies: dict[ViewKey, set[ViewKey]] = {}
        for key, (model_key, view) in self.old_views.items():
            alias, schema, _name = key
            read_columns = {
                (alias, *Identifiers.get_schema_and_name(table, schema), column) for table, column in view.read_columns
            }
            dependencies[key] = {(alias, read_schema, table) for alias, read_schema, table, _column in read_columns}
            causes[key] = {changed[column] for column in read_columns if column in changed}
            after = self.new_views.get(key)
            if key not in self.renamed_views and not self.keeps_view((model_key, view), after):
                owners = {model_key[0]} if after is None else {model_key[0], after[0][0]}
                causes[key].update(owners & self.changed_columns.keys())
        return ViewCreationOrder.get_affected_sources(dependencies, causes)

    @staticmethod
    def keeps_view(before: ViewEntry, after: ViewEntry | None) -> bool:
        """Whether the same model still declares the same view."""
        return (
            after is not None
            and before[0] == after[0]
            and DeclarationValues.get_declaration_key(before[1]) == DeclarationValues.get_declaration_key(after[1])
        )

    def prepare(self, operations_by_app: dict[str, list[HareOperation]]) -> None:
        """Combine changes sharing a view before migration dependency sorting."""
        groups: list[set[str]] = []
        self.renamed_views = self.get_view_renames(operations_by_app)
        causes = self.get_affected_apps()
        for key, apps in causes.items():
            if not apps:
                continue
            before = self.old_views[key]
            owner_app = before[0][0]
            affected_apps = apps | {owner_app}
            after = self.new_views.get(self.get_new_key(key))
            if after is not None and not self.keeps_view(before, after):
                affected_apps.add(after[0][0])
                affected_apps.update(
                    self.get_view_sources(self.new_state, self.new_views, self.get_new_key(key))
                    & operations_by_app.keys()
                )
            if len(affected_apps) == 1:
                continue
            overlaps = [group for group in groups if group & affected_apps]
            groups = [group for group in groups if group not in overlaps]
            groups.append(affected_apps.union(*overlaps))
        for apps in groups:
            active_apps = apps & operations_by_app.keys()
            anchor = min(active_apps, key=lambda app: (not self.changed_columns.get(app), app))
            keys = {
                key
                for key, owners in causes.items()
                if owners and owners <= apps and self.old_views[key][0][0] in apps
            }
            self.groups[anchor] = (apps, keys)
            self.changed_owners[anchor] = active_apps - {anchor}
            self.prepare_group(anchor, apps, keys, operations_by_app)

    def prepare_group(
        self,
        anchor: str,
        apps: set[str],
        keys: set[ViewKey],
        operations_by_app: dict[str, list[HareOperation]],
    ) -> None:
        """Move the group's schema operations under one app on the shared connection."""
        source = self.new_state.environment.get_schema_client(anchor, self.new_state.default_connections)
        if source is not None and not source.features.can_rollback_ddl:
            raise ConfigurationError("Rebuilding a declared view across apps requires transactional DDL.")
        models = []
        for key in keys:
            before = self.old_views[key]
            after = self.new_views.get(self.get_new_key(key))
            models.append(self.old_state.models[before[0]])
            if after is not None:
                models.append(self.new_state.models[after[0]])
            if not self.keeps_view(before, after):
                owners = {before[0][0]} if after is None else {before[0][0], after[0][0]}
                if missing := owners - self.changed_columns.keys():
                    raise ConfigurationError(
                        f"Include app {min(missing)!r} when generating the migration that changes view {key[2]!r}."
                    )
        models.extend(
            model
            for (app, _name), model in self.old_state.models.items()
            if app in self.changed_columns
            and any(
                model.options.get(ModelOption.SCHEMA) == schema and model.db_table == table
                for schema, table, _column in self.changed_columns[app]
            )
            and app in apps
        )
        if len({bool(model.options.get(ModelOption.TENANT_SCHEMA)) for model in models}) > 1:
            raise ConfigurationError("An atomic view rebuild cannot mix shared and tenant schema changes.")
        old_objects = {(self.old_views[key][0], key[2]) for key in keys}
        new_keys = {self.get_new_key(key) for key in keys}
        new_objects = {(self.new_views[key][0], key[2]) for key in new_keys if key in self.new_views}
        grouped: list[tuple[str, Operation]] = [
            (app, operation)
            for app in sorted(apps)
            for operation in operations_by_app.pop(app, ())
            if not self.is_rebuilt_view_operation(app, operation, old_objects, new_objects)
        ]
        ordered = OperationSorter(self.old_state, self.connection_by_app).sort(grouped)
        operations_by_app[anchor] = [
            operation if app == anchor else AppOperation(app, operation)  # type: ignore[misc]
            for app, operation in ordered
        ]

    @staticmethod
    def is_rebuilt_view_operation(
        app: str,
        operation: HareOperation,
        old_objects: set[tuple[tuple[str, str], str]],
        new_objects: set[tuple[tuple[str, str], str]],
    ) -> bool:
        if isinstance(operation, (RemoveView, RemoveMaterializedView)):
            return ((app, operation.model_name), operation.name) in old_objects
        if isinstance(operation, (RenameView, RenameMaterializedView)) and operation.old_name is not None:
            return ((app, operation.model_name), operation.old_name) in old_objects
        if isinstance(operation, (AddView, AddMaterializedView, AlterView, AlterMaterializedView)):
            return ((app, operation.model_name), operation.schema_object.name) in new_objects
        return False

    def surround(self, anchor: str, operations: list[HareOperation]) -> list[HareOperation]:
        """Drop readers before their sources change and restore them after, in the same migration."""
        _apps, keys = self.groups[anchor]
        before = ViewCreationOrder.get_ordered(
            sorted(keys, key=repr), lambda key: self.old_views[key][1], get_schema=lambda key: key[1]
        )
        after = ViewCreationOrder.get_ordered(
            [new_key for key in before if (new_key := self.get_new_key(key)) in self.new_views],
            lambda key: self.new_views[key][1],
            get_schema=lambda key: key[1],
        )
        result: list[HareOperation] = []
        for key in reversed(before):
            (app, model), view = self.old_views[key]
            cls = RemoveMaterializedView if isinstance(view, MaterializedView) else RemoveView
            operation = cls(model, view.name)
            result.append(operation if app == anchor else AppOperation(app, operation))
        result.extend(operations)
        for key in after:
            (app, model), view = self.new_views[key]
            addition: HareOperation = (
                AddMaterializedView(model, view, replacing=True)
                if isinstance(view, MaterializedView)
                else AddView(model, view)
            )
            result.append(addition if app == anchor else AppOperation(app, addition))
        return result

    def finalize(
        self, migrations: list[SplitMigration], get_migration_leaves: Callable[[str], list[tuple[str, str]]]
    ) -> None:
        """Attach each atomic rebuild to one migration and retain prerequisite app history."""
        for anchor, (apps, _keys) in self.groups.items():
            matches = [migration for migration in migrations if migration.app_label == anchor]
            if len(matches) > 1:
                raise ConfigurationError(f"The view changes of app {anchor!r} cannot run in one atomic migration.")
            if not matches:
                matches = [SplitMigration(anchor)]
                migrations.extend(matches)
            [migration] = matches
            migration.operations = self.surround(anchor, migration.operations)
            for app in (apps | self.get_source_apps(self.groups[anchor][1])) - {anchor}:
                migration.existing_dependencies.update(get_migration_leaves(app))
            # Changed declarations still advance their owner's history. A future migration of that
            # app must find the state recorded in the atomic DDL migration.
            position = migrations.index(migration)
            for app in sorted(self.changed_owners[anchor]):
                migrations.append(SplitMigration(app, split_dependencies={position}))

    def get_source_apps(self, keys: set[ViewKey]) -> set[str]:
        """Applications whose physical tables or views the affected declarations read."""
        return set().union(
            *(
                self.get_view_sources(state, declarations, view_key)
                for key in keys
                for state, declarations, view_key in (
                    (self.old_state, self.old_views, key),
                    (self.new_state, self.new_views, self.get_new_key(key)),
                )
                if view_key in declarations
            )
        )

    def get_view_sources(self, state: State, views: dict[ViewKey, ViewEntry], key: ViewKey) -> set[str]:
        read_tables = {
            (key[0], *Identifiers.get_schema_and_name(table, key[1])) for table, _column in views[key][1].read_columns
        }
        return {
            app
            for (app, _name), model in state.models.items()
            if (
                self.connection_by_app.get(app),
                model.options.get(ModelOption.SCHEMA),
                model.db_table,
            )
            in read_tables
        } | {views[table][0][0] for table in read_tables if table in views}
