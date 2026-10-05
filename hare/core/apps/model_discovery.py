from __future__ import annotations

import importlib
import warnings
from inspect import isclass
from types import ModuleType
from typing import TYPE_CHECKING, Any

from hare.core.apps.swappable_models import SwappableModels
from hare.core.apps.table_name_checks import TableNameChecks
from hare.core.constants import DEFAULT_CONNECTION_NAME
from hare.exceptions import ConfigurationError
from hare.models import Model

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.core.apps.apps import Apps


class ModelDiscovery:
    """The models of an app found in its modules - the classes a module declares itself, not the ones
    it imports - and the apps of the configuration loaded."""

    @staticmethod
    def discover_models(models_path: ModuleType | str, app_label: str) -> list[type[Model]]:
        if isinstance(models_path, ModuleType):
            module = models_path
        else:
            try:
                module = importlib.import_module(models_path)
            except ImportError:
                raise ConfigurationError(f'Module "{models_path}" not found')
            except (NameError, AttributeError, SyntaxError) as error:
                # The module exists but raised while its body executed (typo, bad reference,
                # broken syntax) - a different failure mode than "not found" above, so the
                # message says what actually happened instead of implying a missing module.
                raise ConfigurationError(f'Module "{models_path}" failed to import: {error}') from error
        discovered_models: list[type[Model]] = []
        # The models skipped as another app's - for the error below when every candidate is one.
        models_excluded_for_other_app: list[type[Model]] = []
        if possible_models := getattr(module, "__models__", None):
            try:
                possible_models = [*possible_models]
            except TypeError:
                possible_models = None
        if not possible_models:
            possible_models = [getattr(module, attribute_name) for attribute_name in dir(module)]
            if module.__spec__ is not None:
                # A module's models are the ones it declares, itself or in a submodule; a model
                # imported from elsewhere belongs to its own module's app. A module assembled at
                # runtime has no declarations - its attributes count.
                possible_models = [
                    attribute
                    for attribute in possible_models
                    if ModelDiscovery.is_declared_in_module(attribute, module.__name__)
                ]
        for attribute in possible_models:
            if isclass(attribute) and issubclass(attribute, Model) and not attribute._meta.abstract:
                if attribute._meta.app and attribute._meta.app != app_label:
                    models_excluded_for_other_app.append(attribute)
                    continue
                attribute._meta.app = app_label
                discovered_models.append(attribute)
        if not discovered_models:
            if models_excluded_for_other_app:
                # Every candidate belongs to another app - usually a module whose classes kept the
                # app label of an earlier context.
                raise ConfigurationError(
                    f'Module "{models_path}" has no models for app label "{app_label}" - every '
                    f"candidate model in it ({', '.join(model.__name__ for model in models_excluded_for_other_app)}) "
                    f"is already registered under a different app label. If this module is meant "
                    f'to be shared between apps, re-registering it under "{app_label}" only works '
                    "for models that were never registered under a different label before."
                )
            warnings.warn(f'Module "{models_path}" has no models', RuntimeWarning, stacklevel=4)
        return discovered_models

    @staticmethod
    def is_declared_in_module(attribute: Any, module_name: str) -> bool:
        """Returns whether a module attribute was declared in that module or one of its submodules.

        Args:
            attribute: The attribute.
            module_name: The module's name.

        Returns:
            True for a class whose ``__module__`` is the module or a submodule of it.
        """
        declaring_module_name = getattr(attribute, "__module__", None)
        return isinstance(declaring_module_name, str) and (
            declaring_module_name == module_name or declaring_module_name.startswith(f"{module_name}.")
        )

    @staticmethod
    def load_from_config(apps: Apps) -> None:
        if apps._connections is None:
            raise ConfigurationError("ConnectionHandler is required to load from config")
        for name, info in apps._config.items():
            default_connection = info.get("default_connection", DEFAULT_CONNECTION_NAME)
            if default_connection not in apps._connections.db_config:
                raise ConfigurationError(f'Unknown connection "{default_connection}" for app "{name}"')
            if apps._validate_connections:
                apps._connections.get(default_connection)

            apps.init_app(name, info["models"], init_relations=False)
            apps.assign_default_connection(name, default_connection)

        SwappableModels.check_swappable_settings(apps)
        SwappableModels.init_swapped_models(apps)
        TableNameChecks.check_cross_connection_constraints(apps)
        apps.init_relations()
        TableNameChecks.check_change_capture_connections(apps)
        if apps._validate_connections:
            apps.build_initial_querysets()
