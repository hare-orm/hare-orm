from __future__ import annotations

import importlib
from inspect import isclass
from types import ModuleType
from typing import TYPE_CHECKING

from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.apps.apps import Apps


class SwappableModels:
    """Swappable models: the settings checked to name models of the right apps, every model declaring
    Meta.swappable marked with the label its setting points at, and the settings naming the targets
    of a generic foreign key."""

    @staticmethod
    def check_swappable_settings(apps: Apps) -> None:
        """Checks every configured ``swappable`` setting points at a registered concrete model
        that isn't itself swapped for another one.

        Args:
            apps: The registry.

        Raises:
            ConfigurationError: A setting's model isn't registered (or is abstract), or is swapped
                by its own ``Meta.swappable`` setting - a chain of swaps.
        """
        for setting, value in apps.swappable_settings.items():
            if isinstance(value, dict):
                for label in value.values():
                    SwappableModels.check_swappable_target(apps, setting, label)
                continue
            label = value
            app_label, model_name = label.split(".", 1)
            target_model = apps.apps.get(app_label, {}).get(model_name)
            if target_model is None:
                reason = (
                    "it is an abstract model (Meta.abstract = True)"
                    if SwappableModels.is_abstract_model_of_app(apps, app_label, model_name)
                    else f'app "{app_label}" has no such model'
                )
                raise ConfigurationError(f'Swappable setting "{setting}" points at "{label}", but {reason}')
            target_setting = target_model._meta.swappable
            if target_setting is not None and target_setting != setting:
                target_label = apps.swappable_settings.get(target_setting, label)
                if target_label != label:
                    raise ConfigurationError(
                        f'Swappable setting "{setting}" points at "{label}", which the {target_setting} setting '
                        f'swaps for "{target_label}" - point {setting} at the final model directly'
                    )

    @staticmethod
    def check_swappable_target(apps: Apps, setting: str, label: str) -> None:
        """Checks a target of a ``swappable`` setting naming the targets of a generic foreign key.

        Args:
            apps: The registry.
            setting: The setting name.
            label: The ``"app_label.ModelName"`` label of a target.

        Raises:
            ConfigurationError: The model isn't registered, or is abstract.
        """
        app_label, model_name = label.split(".", 1)
        if apps.apps.get(app_label, {}).get(model_name) is None:
            reason = (
                "it is an abstract model (Meta.abstract = True)"
                if SwappableModels.is_abstract_model_of_app(apps, app_label, model_name)
                else f'app "{app_label}" has no such model'
            )
            raise ConfigurationError(f'Swappable setting "{setting}" points at "{label}", but {reason}')

    @staticmethod
    def get_swappable_targets(apps: Apps) -> dict[str, dict[str, str]]:
        """The ``swappable`` settings naming the targets of a generic foreign key.

        Args:
            apps: The registry.

        Returns:
            Setting name to its targets - branch name to ``"app_label.ModelName"``.
        """
        return {setting: value for setting, value in apps.swappable_settings.items() if isinstance(value, dict)}

    @staticmethod
    def is_abstract_model_of_app(apps: Apps, app_label: str, model_name: str) -> bool:
        """Whether one of ``app_label``'s configured model modules declares ``model_name`` as an
        abstract model.

        Args:
            apps: The registry.
            app_label: The app's label.
            model_name: The model's class name.
        """
        for models_path in apps._config.get(app_label, {}).get("models", ()):
            module = models_path if isinstance(models_path, ModuleType) else importlib.import_module(models_path)
            candidate = getattr(module, model_name, None)
            if isclass(candidate) and issubclass(candidate, Model) and candidate._meta.abstract:
                return True
        return False

    @staticmethod
    def init_swapped_models(apps: Apps) -> None:
        """Marks every model declaring ``Meta.swappable`` with the label its setting points at
        instead of it (``_meta.swapped``), or None while it is the model in use.

        Args:
            apps: The registry.
        """
        for app_label, app in apps.apps.items():
            for model_name, model in app.items():
                setting = model._meta.swappable
                label = apps.get_swappable_label(setting) if setting else None
                model._meta.swapped = label if label is not None and label != f"{app_label}.{model_name}" else None
