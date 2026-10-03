from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from hare.migrations.state.project.model_state import ModelState

from hare.core.apps import Apps
from hare.core.connection_handler import ConnectionHandler
from hare.core.context import HareContext
from hare.exceptions import ConfigurationError
from hare.fields.swappable import SwappableModelReference
from hare.models import Model
from hare.sql import Query, Table


class StateApps(Apps):
    REJECTS_SWAPPED_RELATION_TARGETS = False

    def __init__(
        self,
        default_connections: dict[str, str] | None = None,
        connections: ConnectionHandler | None = None,
    ) -> None:
        if connections is None:
            ctx = HareContext.get_current()
            connections = ctx.connections if ctx is not None else ConnectionHandler()

        super().__init__({}, connections)
        self._default_connections = default_connections or {}

    def register_model(self, app_label: str, model: type[Model]) -> None:
        if app_label not in self.apps:
            self.apps[app_label] = {}

        if model._meta.app and model._meta.app != app_label:
            raise ConfigurationError(f"Given model is already registered with label {model._meta.app}")

        self.apps[app_label][model.__name__] = model
        model._meta.app = app_label
        if app_label in self._default_connections:
            model._meta.default_connection = self._default_connections[app_label]

    def get_swappable_label(self, setting: str) -> str:
        """The label a ``swappable`` setting points at, read from the live app registry - a
        migration state has no configuration of its own. Falls back to this state's own models
        outside an initialized Hare.

        Raises:
            ConfigurationError: The setting is neither configured nor declared by any model.
        """
        live_context = HareContext.get_current()
        live_apps = live_context.apps if live_context is not None else None
        if live_apps is not None and live_apps is not self:
            return live_apps.get_swappable_label(setting)
        return super().get_swappable_label(setting)

    def _reference_is_missing(self, reference: Any) -> bool:
        """Whether a relation's ``"app.Model"`` reference names a model this state can't load
        yet - a model class, or a reference of another form, never is.

        Args:
            reference: The reference.

        Returns:
            True for an unloadable ``"app.Model"`` string.
        """
        if not isinstance(reference, str):
            return False
        parts = reference.split(".")
        return len(parts) == 2 and not self._reference_is_loadable(*parts)

    def _reference_is_loadable(self, ref_app: str, ref_model: str) -> bool:
        """Whether a relation's target can be resolved now: tracked in this state, or a ``Meta.managed
        = False`` model, which never gets a ``CreateModel`` - a state-only clone of it is
        registered.
        """
        if ref_app in self.apps and ref_model in self.apps[ref_app]:
            return True
        live_context = HareContext.get_current()
        live_apps = live_context.apps if live_context is not None else None
        if live_apps is None:
            return False
        try:
            live_model = live_apps.get_model(ref_app, ref_model)
        except ConfigurationError:
            return False
        if live_model._meta.managed is False:
            self.register_model(ref_app, self._clone_unmanaged_model(ref_app, live_model))
            return True
        return False

    def _clone_unmanaged_model(self, app_label: str, live_model: type[Model]) -> type[Model]:
        """A state-only copy of an unmanaged model, rendered like ``CreateModel`` renders a model -
        relation setup changes the copy, not the live class.
        """
        from hare.migrations.state.project.model_state import ModelState

        model_state = ModelState.make_from_model(app_label, live_model)
        return model_state.render(self, deepcopy_fields=False)

    def _check_table_name_collisions(self) -> None:
        """No-op - a model moved to another app is recorded under both apps for the span of
        migrations between its state-only CreateModel and DeleteModel, one table for both."""

    def _init_relations(self) -> None:
        """Sets up relations, skipping those whose target isn't registered yet (``CreateModel``
        operations come in any order) - such a model stays not inited until the target exists.
        """
        if all(model._meta._inited for app in self.apps.values() for model in app.values()):
            return
        super()._init_relations()

    def _link_relations(self) -> None:
        uninited_models = [model for app in self.apps.values() for model in app.values() if not model._meta._inited]
        if not uninited_models:
            return

        models_with_missing_refs: set[type[Model]] = set()

        for model in uninited_models:
            for field_name in (*model._meta.fk_fields, *model._meta.o2o_fields):
                fk_object = model._meta.fields_map[field_name]
                reference = SwappableModelReference.get_model_reference(fk_object.model_name)  # type: ignore[attr-defined]
                if self._reference_is_missing(reference):
                    models_with_missing_refs.add(model)
                    break  # No need to check remaining fields

            if model in models_with_missing_refs:
                continue

            for field_name in model._meta.m2m_fields:
                m2m_object = model._meta.fields_map[field_name]
                references = [SwappableModelReference.get_model_reference(m2m_object.model_name)]  # type: ignore[attr-defined]
                through_reference = SwappableModelReference.get_model_reference(m2m_object.through_model)  # type: ignore[attr-defined]
                if through_reference is not None:
                    # A through model referenced as "app.Model" must be registered too.
                    references.append(through_reference)
                if any(self._reference_is_missing(reference) for reference in references):
                    models_with_missing_refs.add(model)
                    break

        for model in models_with_missing_refs:
            model._meta._inited = True

        super()._link_relations()

        for model in models_with_missing_refs:
            model._meta._inited = False

    def _build_initial_querysets(self) -> None:
        self.build_querysets(model for app in self.apps.values() for model in app.values())

    def build_querysets(self, models: Iterable[type[Model]]) -> None:
        """Finalises the inited models of ``models`` and builds their base queries.

        Args:
            models: The models.
        """
        # Skip building querysets when no DB config is available (state-only mode)
        # This allows pure state operations to work without database connections
        if self._connections._db_config is None:
            return

        for model in models:
            if model._meta.default_connection is None:
                continue
            if not model._meta._inited:
                continue
            model._meta.finalise_model()
            model._meta.basetable = Table(name=model._meta.db_table, schema=model._meta.schema)
            basequery = model._meta.db.query_class.from_(model._meta.basetable)
            model._meta.basequery = cast("Query", basequery)
            model._meta.basequery_all_fields = cast("Query", basequery.select(*model._meta.db_fields))

    def unregister_model(self, app_label: str, model_name: str) -> None:
        try:
            model = self.apps[app_label].pop(model_name)
            model._meta.app = None
        except KeyError:
            return

    def split_reference(self, reference: str | type[Model] | SwappableModelReference) -> tuple[str, str]:
        if isinstance(reference, SwappableModelReference):
            reference = reference.get_label()
        if not isinstance(reference, str):
            model_class = reference
            app_label = model_class._meta.app
            if app_label is None:
                raise ConfigurationError(f"Model {model_class} is not registered in any app")
            return app_label, model_class.__name__
        if len(items := reference.split(".")) != 2:
            raise ConfigurationError(f"'{reference}' is not a valid model reference. Should be <app>.<model>.")
        return items[0], items[1]

    def get_model(self, app_label: str, model_name: str | None = None) -> type[Model]:
        if model_name is None:
            app_label, model_name = self.split_reference(app_label)
        return self.apps[app_label][model_name]

    def clone(
        self,
        model_states: dict[tuple[str, str], ModelState] | None = None,
    ) -> StateApps:
        # hare.migrations.state.project itself imports StateApps at module level, so a
        # top-level import here would be circular - deferred to first use, same pattern as the
        # documented hare.query.queryset<->hare.query.expressions cycle.
        from hare.migrations.state.project.model_state import ModelState

        state_apps = self.__class__(
            default_connections=dict(self._default_connections),
            connections=self._connections,
        )
        if model_states is not None:
            for (app_label, _model_name), model_state in model_states.items():
                # Rendered from copies: relation setup fills derived attributes into the fields,
                # which must not leak into the state.
                model = model_state.render(state_apps)
                state_apps.register_model(app_label, model)
        else:
            for app_label, app in self.apps.items():
                for model in app.values():
                    model_clone = ModelState.make_from_model(app_label, model).render(state_apps)
                    state_apps.register_model(app_label, model_clone)

        state_apps._init_relations()
        state_apps._build_initial_querysets()
        return state_apps
