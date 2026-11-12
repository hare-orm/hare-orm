from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeGuard

from hare.core.apps.model_binding_graph import ModelBindingGraph
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.migrations.state.state_apps import StateApps
from hare.models import Model

if TYPE_CHECKING:
    from hare.core.apps.declarations import TableIdentity
    from hare.migrations.state.migration_environment import MigrationEnvironment
    from hare.migrations.state.model_state import ModelState


class StateRendering:
    """The model classes rendered from a state's model states - drawn in full the first time they
    are asked for, then kept up to date model by model: an operation only marks the models it
    changed, and the marks are applied together the next time the classes are asked for.

    Args:
        default_connections: Each app's default connection alias.
        environment: Connections and application declarations captured by the state.
    """

    __slots__ = ("added_keys", "apps", "changed_keys", "default_connections", "environment", "removed_keys")

    def __init__(self, default_connections: dict[str, str], environment: MigrationEnvironment) -> None:
        self.default_connections = default_connections
        self.environment = environment
        self.apps: StateApps | None = None
        self.added_keys: set[tuple[str, str]] = set()
        self.changed_keys: set[tuple[str, str]] = set()
        self.removed_keys: set[tuple[str, str]] = set()

    def mark_added(self, key: tuple[str, str]) -> None:
        """Marks a model added to the state.

        Args:
            key: The model's (app, model name) key.
        """
        if self.apps is None:
            return
        if key in self.removed_keys:
            # Removed and added again before the classes were asked for - rendered anew.
            self.removed_keys.discard(key)
            self.changed_keys.add(key)
        else:
            self.added_keys.add(key)

    def mark_changed(self, key: tuple[str, str]) -> None:
        """Marks a model of the state changed.

        Args:
            key: The model's (app, model name) key.
        """
        if self.apps is None or key in self.added_keys:
            return
        self.changed_keys.add(key)

    def mark_removed(self, key: tuple[str, str]) -> None:
        """Marks a model removed from the state.

        Args:
            key: The model's (app, model name) key.
        """
        if self.apps is None:
            return
        if key in self.added_keys:
            # Never rendered - nothing to take out.
            self.added_keys.discard(key)
            return
        self.changed_keys.discard(key)
        self.removed_keys.add(key)

    def get_apps(self, model_states: dict[tuple[str, str], ModelState]) -> StateApps:
        """The rendered classes of the model states, brought up to date.

        Args:
            model_states: The state's model states.

        Returns:
            The registry of the rendered classes.
        """
        if self.apps is None:
            prepared_apps = StateApps(
                default_connections=dict(self.default_connections), environment=self.environment
            ).clone(model_states=model_states)
            self.apps = prepared_apps
            return self.apps
        if self.added_keys or self.changed_keys or self.removed_keys:
            self.apply_marks(model_states)
        return self.apps

    def apply_marks(self, model_states: dict[tuple[str, str], ModelState]) -> None:
        """Brings the rendered classes up to date with the marked changes: a changed model and the models
        it reaches are rendered again, as is a removed one something left still points at; other removed
        models are taken out and added ones rendered and linked in place, nothing else again.

        Args:
            model_states: The state's model states.
        """
        apps: StateApps = self.apps  # type: ignore[assignment]
        added_keys, changed_keys, removed_keys = self.added_keys, self.changed_keys, self.removed_keys
        with ModelBindingGraph(apps):
            models_by_table = self.get_models_by_table() if changed_keys or removed_keys else {}
            if not changed_keys and self.removes_in_place(removed_keys, models_by_table):
                self.remove_models(removed_keys)
                self.add_models([key for key in added_keys if key in model_states], model_states)
            else:
                # Find dependants while the previous classes are still registered.
                related_keys = self.get_related_model_keys(changed_keys | removed_keys, models_by_table)
                for app_label, model_name in removed_keys:
                    apps.unregister_model(app_label, model_name)
                reloaded_keys = {key for key in related_keys | changed_keys | added_keys if key in model_states}
                self.reload_models(reloaded_keys, model_states)
        self.added_keys, self.changed_keys, self.removed_keys = set(), set(), set()

    def removes_in_place(
        self, keys: set[tuple[str, str]], models_by_table: dict[TableIdentity, list[type[Model]]]
    ) -> bool:
        """Whether removed models can be taken out of the rendered classes in place - no model left is a
        subclass of one of them or points at one of them.

        Args:
            keys: The removed models' (app, model name) keys.
            models_by_table: Historical names grouped by physical table.
        """
        apps: StateApps = self.apps  # type: ignore[assignment]
        removed_models = {apps.get_model(".".join(key)) for key in keys if StateRendering.is_rendered(apps, key)}
        for model in removed_models:
            table_key = apps.get_table_identity(model, model._meta.schema, model._meta.db_table)
            if any(alias not in removed_models for alias in models_by_table[table_key]):
                # Its reverse accessors must be rebuilt for the surviving historical name.
                return False
            for subclass in model.__subclasses__():
                subclass_app = getattr(subclass._meta, "app", None) if issubclass(subclass, Model) else None
                if subclass not in removed_models and subclass_app is not None:
                    return False
            meta = model._meta
            for name in meta.fetch_fields:
                field = meta.fields_map[name]
                if StateRendering.is_relation_side(field) and field.related_model not in removed_models:
                    # The other side of a relation declared by a model left.
                    return False
            if any(
                relation.related_model not in removed_models for relation in meta.hidden_backward_relations.values()
            ):
                return False
            for name in meta.fetch_fields:
                target = meta.fields_map[name].related_model  # type: ignore[attr-defined]
                if target is None or target in removed_models:
                    continue
                # A through model points at the model whose many-to-many relation goes through it.
                for target_field_name in target._meta.many_to_many_fields:
                    target_field = target._meta.fields_map[target_field_name]
                    if getattr(target_field, "through_model_class", None) in removed_models:
                        return False
        return True

    def remove_models(self, keys: set[tuple[str, str]]) -> None:
        """Takes removed models out of the rendered classes in place - the other sides their relations put
        on the models they point at are taken off those models, nothing is rendered again.

        Args:
            keys: The removed models' (app, model name) keys.
        """
        apps: StateApps = self.apps  # type: ignore[assignment]
        removed_models = {apps.get_model(".".join(key)) for key in keys if StateRendering.is_rendered(apps, key)}
        targets: dict[type[Model], None] = {}
        for model in removed_models:
            for field_name in model._meta.fetch_fields:
                target = model._meta.fields_map[field_name].related_model  # type: ignore[attr-defined]
                if target is not None and target not in removed_models:
                    targets[target] = None
        for target in targets:
            meta = target._meta
            for name, field in list(meta.fields_map.items()):
                if StateRendering.is_relation_side(field) and field.related_model in removed_models:
                    meta.remove_field(name)
            for hidden_key, relation in list(meta.hidden_backward_relations.items()):
                if relation.related_model in removed_models:
                    meta.remove_hidden_backward_relation(hidden_key)
        for app_label, model_name in keys:
            apps.unregister_model(app_label, model_name)
        for target in targets:
            target._meta.finalise_model()
        apps.build_querysets(targets)

    @staticmethod
    def is_relation_side(field: Field[Any]) -> TypeGuard[RelationalField[Any]]:
        """Whether a field is the side a relation of another model puts on the model it points at - a
        backward relation, a generated many-to-many field.

        Args:
            field: A field of a rendered model.
        """
        return isinstance(field, BackwardForeignKeyRelation) or (
            isinstance(field, ManyToManyFieldInstance) and field._generated
        )

    @staticmethod
    def is_rendered(apps: StateApps, key: tuple[str, str]) -> bool:
        """Whether a model is among the rendered classes.

        Args:
            apps: The rendered classes.
            key: The model's (app, model name) key.
        """
        return key[1] in apps.apps.get(key[0], {})

    def get_models_by_table(self) -> dict[TableIdentity, list[type[Model]]]:
        """Group rendered historical names so shared reverse accessors do not hide dependencies."""
        apps: StateApps = self.apps  # type: ignore[assignment]
        models_by_table: dict[TableIdentity, list[type[Model]]] = {}
        for model in apps.get_models_iterable():
            table_key = apps.get_table_identity(model, model._meta.schema, model._meta.db_table)
            models_by_table.setdefault(table_key, []).append(model)
        return models_by_table

    def get_related_model_keys(
        self, keys: set[tuple[str, str]], models_by_table: dict[TableIdentity, list[type[Model]]]
    ) -> set[tuple[str, str]]:
        """Find dependants of all changed roots before changing the rendered registry.

        A single visited set covers overlapping relation graphs, including inheritance,
        hidden reverse relations and models sharing a physical table. Missing roots remain
        in the result so the caller can reconcile them with the new model states.
        """
        apps: StateApps = self.apps  # type: ignore[assignment]
        related_keys: set[tuple[str, str]] = set()
        pending_models: list[type[Model]] = []
        for key in keys:
            try:
                pending_models.append(apps.get_model(".".join(key)))
            except KeyError:
                related_keys.add(key)
        while pending_models:
            related_model = pending_models.pop()
            app_label = related_model._meta.app
            if app_label is None:
                continue
            related_key = (app_label, related_model.__name__)
            if related_key in related_keys:
                continue
            related_keys.add(related_key)
            pending_models.extend(self.get_related_models(related_model))
            table_key = apps.get_table_identity(
                related_model, related_model._meta.schema, related_model._meta.db_table
            )
            pending_models.extend(models_by_table.get(table_key, ()))
        return related_keys | keys

    @staticmethod
    def get_related_models(model: type[Model]) -> list[type[Model]]:
        """The models directly reachable from a model through subclassing or a relation field.

        Args:
            model: A rendered model.

        Returns:
            The models.
        """
        related_models = [subclass for subclass in model.__subclasses__() if issubclass(subclass, Model)]
        for field_name in model._meta.fetch_fields:
            field: RelationalField[Model] = model._meta.fields_map[field_name]  # type: ignore[assignment]
            # A target not rendered yet (a CreateModel still to come) has no class.
            if field.related_model is not None:
                related_models.append(field.related_model)
        # A relation declared related_name=False has no field on its target - the model declaring it is
        # reached through the target's hidden backward relations, or rendering the target again loses them.
        related_models.extend(relation.related_model for relation in model._meta.hidden_backward_relations.values())
        return related_models

    def reload_models(self, keys: set[tuple[str, str]], model_states: dict[tuple[str, str], ModelState]) -> None:
        """Renders models again from their model states and sets their relations up.

        Args:
            keys: The models' (app, model name) keys.
            model_states: The state's model states.
        """
        apps: StateApps = self.apps  # type: ignore[assignment]
        for app_label, model_name in keys:
            apps.unregister_model(app_label, model_name)
            apps.register_model(app_label, model_states[(app_label, model_name)].render())
        apps.init_relations()
        apps.build_initial_querysets()

    def add_models(self, keys: list[tuple[str, str]], model_states: dict[tuple[str, str], ModelState]) -> None:
        """Renders models just added to the state and links them in place, as ``Apps`` links models
        when it starts - no model is rendered again. A model their relations reach gets the other
        side of the relation added and its base queries built again, as does a model waiting for one
        of them to be added.

        Args:
            keys: The added models' (app, model name) keys.
            model_states: The state's model states.
        """
        if not keys:
            return
        apps: StateApps = self.apps  # type: ignore[assignment]
        for app_label, model_name in keys:
            apps.register_model(app_label, model_states[(app_label, model_name)].render())
        uninited_models = [model for app in apps.apps.values() for model in app.values() if not model._meta._inited]
        # Only the models these link to are finalised - not every rendered model again.
        apps.link_relations()
        linked_models: dict[type[Model], None] = {}
        for linked_model in uninited_models:
            if not linked_model._meta._inited:
                continue
            linked_models[linked_model] = None
            for field_name in linked_model._meta.fetch_fields:
                related_model = linked_model._meta.fields_map[field_name].related_model  # type: ignore[attr-defined]
                if related_model is not None:
                    linked_models[related_model] = None
        for linked_model in linked_models:
            linked_model._meta.finalise_model()
        apps.build_querysets(linked_models)
