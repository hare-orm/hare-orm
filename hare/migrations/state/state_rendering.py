from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, TypeGuard, cast

from hare.ddl.indexes.index import Index
from hare.fields.field import Field
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.migrations.state.state_apps import StateApps
from hare.models import Model

if TYPE_CHECKING:
    from hare.migrations.state.model_state import ModelState


class StateRendering:
    """The model classes rendered from a state's model states - drawn in full the first time they
    are asked for, then kept up to date model by model: an operation only marks the models it
    changed, and the marks are applied together the next time the classes are asked for.

    Args:
        default_connections: Each app's default connection alias.
    """

    __slots__ = ("added_keys", "apps", "changed_keys", "default_connections", "removed_keys")

    def __init__(self, default_connections: dict[str, str]) -> None:
        self.default_connections = default_connections
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
            self.apps = StateApps(default_connections=dict(self.default_connections)).clone(model_states=model_states)
            self.build_index_expressions(model for app in self.apps.apps.values() for model in app.values())
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
        apps = cast("StateApps", self.apps)
        added_keys, changed_keys, removed_keys = self.added_keys, self.changed_keys, self.removed_keys
        self.added_keys, self.changed_keys, self.removed_keys = set(), set(), set()
        if not changed_keys and self.removes_in_place(removed_keys):
            self.remove_models(removed_keys)
            self.add_models([key for key in added_keys if key in model_states], model_states)
            return
        # The models the old classes reach still carry the backward relations those registered on
        # them - they are rendered again too, found while the old classes are still registered.
        related_keys: set[tuple[str, str]] = set()
        for key in changed_keys | removed_keys:
            related_keys |= self.find_related_models(key)
        for app_label, model_name in removed_keys:
            apps.unregister_model(app_label, model_name)
        reloaded_keys = {key for key in related_keys | changed_keys | added_keys if key in model_states}
        self.reload_models(reloaded_keys, model_states)

    def removes_in_place(self, keys: set[tuple[str, str]]) -> bool:
        """Whether removed models can be taken out of the rendered classes in place - no model left is a
        subclass of one of them or points at one of them.

        Args:
            keys: The removed models' (app, model name) keys.
        """
        apps = cast("StateApps", self.apps)
        removed_models = {apps.get_model(".".join(key)) for key in keys if StateRendering.is_rendered(apps, key)}
        for model in removed_models:
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
                target = cast("RelationalField[Model]", meta.fields_map[name]).related_model
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
        apps = cast("StateApps", self.apps)
        removed_models = {apps.get_model(".".join(key)) for key in keys if StateRendering.is_rendered(apps, key)}
        targets: dict[type[Model], None] = {}
        for model in removed_models:
            for field_name in model._meta.fetch_fields:
                target = cast("RelationalField[Model]", model._meta.fields_map[field_name]).related_model
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

    def find_related_models(self, key: tuple[str, str]) -> set[tuple[str, str]]:
        """A model and every model it reaches through subclassing or a relation, transitively, as
        rendered now.

        Args:
            key: The model's (app, model name) key.

        Returns:
            The keys, the model's own among them.
        """
        apps = cast("StateApps", self.apps)
        related_keys: set[tuple[str, str]] = {key}
        try:
            model = apps.get_model(".".join(key))
        except KeyError:
            return related_keys
        pending_models = self.get_related_models(model)
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
        return related_keys

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
            field = cast("RelationalField[Model]", model._meta.fields_map[field_name])
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
        apps = cast("StateApps", self.apps)
        for app_label, model_name in keys:
            apps.unregister_model(app_label, model_name)
            apps.register_model(app_label, model_states[(app_label, model_name)].render(apps))
        apps.init_relations()
        apps.build_initial_querysets()
        self.build_index_expressions(apps.get_model(".".join(key)) for key in keys)

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
        apps = cast("StateApps", self.apps)
        for app_label, model_name in keys:
            apps.register_model(app_label, model_states[(app_label, model_name)].render(apps))
        uninited_models = [model for app in apps.apps.values() for model in app.values() if not model._meta._inited]
        # Only the models these link to are finalised - not every rendered model again.
        apps.link_relations()
        linked_models: dict[type[Model], None] = {}
        for linked_model in uninited_models:
            if not linked_model._meta._inited:
                continue
            linked_models[linked_model] = None
            for field_name in linked_model._meta.fetch_fields:
                related_model = cast("RelationalField[Model]", linked_model._meta.fields_map[field_name]).related_model
                if related_model is not None:
                    linked_models[related_model] = None
        for linked_model in linked_models:
            linked_model._meta.finalise_model()
        apps.build_querysets(linked_models)
        self.build_index_expressions(linked_models)

    @staticmethod
    def build_index_expressions(models: Iterable[type[Model]]) -> None:
        """Builds the key expressions of the models' indexes from their declared form against the rendered models -
        a migration file keeps them as declared, ``ModelState.make_from_model()`` resolves a live
        model's, so an index's generated name and its comparisons work the same on both sides.

        Args:
            models: The rendered models.
        """
        for model in models:
            if not model._meta._inited:
                continue
            for index in model._meta.indexes:
                if isinstance(index, Index) and index.declared_expressions:
                    index.get_expressions(model)
