from __future__ import annotations

from collections.abc import Iterable
from types import ModuleType
from typing import TYPE_CHECKING

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
            context: The application context whose graph and live model registrations change.
            models: The model classes.
            app_label: The app label to register them under.
            connection_alias: The connection their queries use.
            managed: Applied to each prepared binding before its relations are set up -
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
        from hare.core.apps.model_binding_preparation import ModelBindingPreparation

        if connection_alias not in context.connections.db_config:
            raise ConfigurationError(f"Unknown connection {connection_alias!r}")
        if context._apps is None:
            context._apps = Apps({}, context.connections, context._table_name_generator, context=context)
        apps = context._apps
        registered_models = apps.apps.get(app_label, {})
        new_models = []
        seen_model_names: set[str] = set()
        for model in dict.fromkeys(models):
            if model.__name__ in seen_model_names:
                raise ConfigurationError(f"Two models named {model.__name__!r} were passed for app {app_label!r}.")
            seen_model_names.add(model.__name__)
            registered_model = registered_models.get(model.__name__)
            if registered_model is model:
                continue
            if registered_model is not None:
                raise ConfigurationError(
                    f'A different model named "{app_label}.{model.__name__}" is already registered - '
                    "call unregister_live_models() on it first."
                )
            declared_app = model._declaration.app
            registered_binding = apps.binding_graph.bindings.get(model)
            bound_app = registered_binding.app if registered_binding is not None else declared_app
            if bound_app and bound_app != app_label:
                raise ConfigurationError(
                    f"Model {model.__name__!r} belongs to app {bound_app!r} - it can't be "
                    f"registered under {app_label!r}."
                )
            new_models.append(model)
        if not new_models:
            return
        module = ModuleType(f"hare.register_live_models.{app_label}")
        module.__models__ = new_models  # type: ignore[attr-defined]
        # Like loading the configured apps: the models are registered first (their relations
        # set up later), then bound to the connection, then the relations are set up - so the
        # table-name collision check sees the models' real connection.
        with ModelBindingPreparation(apps, new_models):
            if managed is not None:
                for model in new_models:
                    apps.get_binding(model).managed = managed
            apps.init_app(app_label, [module], init_relations=False)
            for model in new_models:
                binding = apps.get_binding(model)
                binding.default_connection = connection_alias
                binding.registered_live = True
            apps.finish_model_binding()

    @staticmethod
    def unregister_live_models(context: HareContext, models: Iterable[ModelReference]) -> None:
        """Reverses ``register_live_models()``: removes the models from the registry together with
        the backward relations they added and every cache built for them - relations between the
        models themselves don't block it. A model with the same name, or the same class, can be
        registered again afterwards; an unregistered class runs no queries until then.

        Args:
            context: The application context whose graph and live model registrations change.
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
        context._apps.remove_models(
            [
                reference if isinstance(reference, type) else context._apps.get_model(reference)
                for reference in references
            ]
        )
