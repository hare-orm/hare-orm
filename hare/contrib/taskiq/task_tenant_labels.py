from __future__ import annotations

from typing import Any
from uuid import UUID

from hare.contrib.taskiq.constants import (
    TENANT_LABEL_ALL_KEY,
    TENANT_LABEL_ANY_OF_KEY,
    TENANT_LABEL_MODELS_KEY,
)
from hare.core.hare_context import HareContext
from hare.exceptions import ConfigurationError, UnSupportedError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.query.scopes.tenants.tenant_values import TenantValues


class TaskTenantLabels:
    """A tenant scope as the value of a task's label, and back - one tenant value as it is (a UUID as
    its text), several as ``{"any_of": [...]}``, every tenant as ``{"all": true}``, a scope given
    model by model as ``{"models": {"app.Model": ...}}``."""

    @staticmethod
    def get_label_value(scope: Any) -> Any:
        """The label value of a tenant scope.

        Args:
            scope: The scope active when the task is sent, not None.

        Returns:
            The value.

        Raises:
            UnSupportedError: A tenant value is neither a string, an integer nor a UUID, or a scope
                is given for a model registered in no app (an abstract base model).
        """
        if type(scope) is ModelTenants:
            label_values: dict[str, Any] = {}
            for model, model_scope in scope.scopes.items():
                if model._meta.app is None:
                    raise UnSupportedError(
                        f"The tenant scope of {model.__name__} can't go into a task label - it is no model of an "
                        "app; give the scope for each model of an app instead"
                    )
                label_values[model._meta.full_name] = TaskTenantLabels.get_label_value(model_scope)
            return {TENANT_LABEL_MODELS_KEY: label_values}
        if type(scope) is AllTenants:
            return {TENANT_LABEL_ALL_KEY: True}
        if type(scope) is TenantValues:
            return {TENANT_LABEL_ANY_OF_KEY: [TaskTenantLabels.get_value_label(value) for value in scope.values]}
        return TaskTenantLabels.get_value_label(scope)

    @staticmethod
    def get_value_label(value: Any) -> Any:
        """The label value of one tenant value.

        Args:
            value: The tenant value.

        Returns:
            The value - a UUID as its text.

        Raises:
            UnSupportedError: The value is neither a string, an integer nor a UUID.
        """
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            return value
        raise UnSupportedError(
            f"The tenant {value!r} can't go into a task label - a label carries a string, an integer or a UUID; "
            "set the label by hand (task.kicker().with_labels(...))"
        )

    @staticmethod
    def get_scope(label_value: Any) -> Any:
        """The tenant scope a label value names.

        Args:
            label_value: The value of the task's label, not None.

        Returns:
            The scope, as ``Tenancy.scope()`` takes it.

        Raises:
            ConfigurationError: A scope given model by model names a model no app registers.
        """
        if not isinstance(label_value, dict):
            return label_value
        if TENANT_LABEL_ALL_KEY in label_value:
            return Tenancy.ALL
        if TENANT_LABEL_ANY_OF_KEY in label_value:
            return Tenancy.any_of(*label_value[TENANT_LABEL_ANY_OF_KEY])
        if TENANT_LABEL_MODELS_KEY in label_value:
            context = HareContext.require_current()
            scopes = {}
            for full_name, model_label_value in label_value[TENANT_LABEL_MODELS_KEY].items():
                app_label, _separator, model_name = full_name.rpartition(".")
                scopes[context.get_model(app_label, model_name)] = TaskTenantLabels.get_scope(model_label_value)
            return scopes
        raise ConfigurationError(f"The task's tenant label {label_value!r} names no tenant scope")
