from __future__ import annotations

from hare.core.apps.model_binding_preparation import ModelBindingPreparation
from hare.migrations.state.state_apps import StateApps
from hare.models import Model


class ModelBindings:
    """Prepare explicit test declarations together, without selecting an ambient registry."""

    @staticmethod
    def prepare(*models: type[Model], app_label: str = "models") -> StateApps:
        """Link one complete graph; callers retain it for all metadata and instances."""
        apps = StateApps()
        with ModelBindingPreparation(apps, models):
            for model in models:
                apps.register_model(model._declaration.app or app_label, model)
            apps.init_relations()
        return apps
