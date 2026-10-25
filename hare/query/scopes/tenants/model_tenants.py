from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from hare.core.caching.model_cache import ModelCache
from hare.exceptions import QueryError
from hare.query.scopes.tenants.tenant_values import TenantValues

if TYPE_CHECKING:
    from hare.core.apps.declarations import ModelReference
    from hare.models import Model


class ModelTenants:
    """A tenant scope given model by model - ``Tenancy.scope({Order: Tenancy.any_of("msk", "kzn"),
    "shop.Faq": Tenancy.ALL})``. A model takes the entry of its own class, else of its nearest base class
    with the same ``Meta.tenant_field``.

    Args:
        scopes: Each model's tenant value, ``Tenancy.any_of(...)`` or ``Tenancy.ALL`` - a model given by
            its class, its ``"app.Model"`` label or a ``swappable()`` setting.

    Raises:
        QueryError: A key isn't a model with ``Meta.tenant_field``, or an entry is None or a
            collection.
        ConfigurationError: A label names no model of the current context.
    """

    __slots__ = ("scopes",)

    def __init__(self, scopes: Mapping[ModelReference, Any]) -> None:
        self.scopes: dict[type[Model], Any] = {}
        for reference, scope in scopes.items():
            model = ModelTenants.get_model(reference)
            meta = getattr(model, "_meta", None)
            if not isinstance(model, type) or meta is None or not meta.tenant_field:
                raise QueryError(f"Tenancy.scope(): {model!r} is not a model with Meta.tenant_field")
            if scope is None:
                raise QueryError(
                    f"Tenancy.scope(): {model.__name__} is given None - leave the model out for no "
                    "tenant scope, or give it Tenancy.ALL for every tenant"
                )
            if isinstance(scope, (list, tuple, set, frozenset, dict)):
                raise QueryError(
                    f"Tenancy.scope(): {model.__name__} is given {scope!r} - give several values as "
                    "Tenancy.any_of(*values)"
                )
            self.scopes[model] = TenantValues.get_single_value_or_scope(scope)

    @staticmethod
    def get_model(reference: ModelReference) -> type[Model]:
        """The model a key of the scope names - a class as it is, a label of the current context's models.

        Args:
            reference: The key.

        Returns:
            The model.
        """
        if isinstance(reference, type):
            return reference
        # Local import: the context imports the query package.
        from hare.core.hare_context import HareContext

        return HareContext.require_current().get_model(reference)

    @staticmethod
    @ModelCache.fact()
    def get_tenant_bases(model: type[Model]) -> tuple[type[Model], ...]:
        """The base classes of ``model`` that are models with the same ``Meta.tenant_field``,
        nearest first - a scope given for one of them is the model's too."""
        tenant_field = model._meta.tenant_field
        return tuple(
            cast("type[Model]", base_class)
            for base_class in model.__mro__[1:]
            if hasattr(base_class, "_meta") and base_class._meta.tenant_field == tenant_field
        )

    def get_for_model(self, model: type[Model]) -> Any:
        """The scope of one model.

        Args:
            model: The model.

        Returns:
            A tenant value, a ``TenantValues``, ``Tenancy.ALL``, or None when the scope names
            neither the model nor a base class of it with the same ``Meta.tenant_field``.
        """
        scopes = self.scopes
        if model in scopes:
            return scopes[model]
        for base_class in ModelTenants.get_tenant_bases(model):
            if base_class in scopes:
                return scopes[base_class]
        return None

    def __repr__(self) -> str:
        entries = ", ".join(f"{model.__name__}: {scope!r}" for model, scope in self.scopes.items())
        return f"{{{entries}}}"
