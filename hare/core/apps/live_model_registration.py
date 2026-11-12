from __future__ import annotations

from collections.abc import Iterable
from types import ModuleType
from typing import TYPE_CHECKING

from hare.core.caching.caches import Caches
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:
    from hare.core.apps.declarations import ModelReference
    from hare.core.hare_context import HareContext
    from hare.models import Model


class LiveModelRegistration:
    """Registers and removes runtime models atomically, including relation bindings and model caches."""

    @staticmethod
    def register_live_models(
        context: HareContext,
        models: Iterable[type[Model]],
        app_label: str,
        connection_alias: str = DEFAULT_CONNECTION_NAME,
        *,
        managed: bool | None = None,
    ) -> None:
        """Registers models built while the application runs - after ``init()``, e.g. from the
        introspection of a live database - under ``app_label`` on ``connection_alias``, binding
        them as ``init()`` binds the configured ones. Models whose relations point at each other
        are registered together. Atomic: when any model fails, none of them stays registered and
        no model keeps a backward relation added by them.

        Args:
            models: The model classes.
            app_label: The app label to register them under.
            connection_alias: The connection their queries use.
            managed: Written to each model's ``_meta.managed`` before its relations are set up -
                False keeps migrations and drift checks away from its table; None keeps what its
                ``Meta.managed`` declares.

        Raises:
            ConfigurationError: ``app_label`` is no name a model's label can start with, ``connection_alias``
                is not a configured connection, two of ``models`` share a name, a different class with
                the same name is already registered under ``app_label``, a model belongs to another app,
                or a relation can't be set up.
        """
        # A label of a model is "<app>.<Model>": an empty app or one with a dot reads as another.
        if not isinstance(app_label, str) or not app_label or "." in app_label:
            raise ConfigurationError(f"register_live_models() needs an app label without a dot, got {app_label!r}")
        # Local import: the registry imports the models, which import the transactions and this module.
        from hare.core.apps import Apps
        from hare.core.apps.cross_connection_checks import CrossConnectionChecks
        from hare.core.apps.enum_member_checks import EnumMemberChecks
        from hare.core.apps.model_binding_graph import ModelBindingGraph
        from hare.core.apps.relation_linking import RelationLinking

        if connection_alias not in context.connections.db_config:
            raise ConfigurationError(f"Unknown connection {connection_alias!r}")
        if context._apps is None:
            context._apps = Apps({}, context.connections, context._table_name_generator)
        context._models_bound = True
        apps = context._apps
        models = list(dict.fromkeys(models))
        registered_models = apps.apps.get(app_label, {})
        seen_model_names: set[str] = set()
        for model in models:
            if model.__name__ in seen_model_names:
                raise ConfigurationError(f"Two models named {model.__name__!r} were passed for app {app_label!r}.")
            seen_model_names.add(model.__name__)
            registered_model = registered_models.get(model.__name__)
            if registered_model is not None and registered_model is not model:
                raise ConfigurationError(
                    f'A different model named "{app_label}.{model.__name__}" is already registered - '
                    "call unregister_live_models() on it first."
                )
            if model._meta.app and model._meta.app != app_label:
                raise ConfigurationError(
                    f"Model {model.__name__!r} belongs to app {model._meta.app!r} - it can't be "
                    f"registered under {app_label!r}."
                )
        new_models = [model for model in models if registered_models.get(model.__name__) is not model]
        module = ModuleType(f"hare.register_live_models.{app_label}")
        module.__models__ = models  # type: ignore[attr-defined]
        # Like loading the configured apps: the models are registered first (their relations
        # set up later), then bound to the connection, then the relations are set up - so the
        # table-name collision check sees the models' real connection.
        with ModelBindingGraph(apps, models):
            if managed is not None:
                for model in models:
                    model._meta.managed = managed
            apps.init_app(app_label, [module], init_relations=False)
            for model in models:
                model._meta.default_connection = connection_alias
                model._meta.registered_live = True
            CrossConnectionChecks.check_cross_connection_constraints(apps)
            apps.init_relations()
            EnumMemberChecks.add_to_models(models, context.connections)
            CrossConnectionChecks.check_change_capture_connections(apps)
            apps.build_initial_querysets()
        # The targets gained backward relations; caches of the registered model set are stale too,
        # including when a newly registered model has no relations.
        Caches.forget_model_caches(
            set(new_models) | {target for model in models for target in RelationLinking.get_relation_targets(model)}
        )

    @staticmethod
    def unregister_live_models(context: HareContext, models: Iterable[ModelReference]) -> None:
        """Reverses ``register_live_models()``: removes the models from the registry together with
        the backward relations they added and every cache built for them - relations between the
        models themselves don't block it. A model with the same name, or the same class, can be
        registered again afterwards; an unregistered class runs no queries until then.

        Args:
            models: Models registered in this context - classes, ``"app.Model"`` labels or
                ``swappable()`` settings.

        Raises:
            ConfigurationError: One of ``models`` isn't registered, or a model outside ``models``
                still has a relation pointing at one of them.
        """
        references = list(models)
        if context._apps is None:
            names = ", ".join(getattr(reference, "__name__", str(reference)) for reference in references)
            raise ConfigurationError(f"Model(s) {names!r} not registered.")
        context._apps.remove_models([context._apps.get_model(reference) for reference in references])
