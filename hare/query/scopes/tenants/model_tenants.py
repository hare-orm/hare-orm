from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.cache import Cache
from hare.core.caching.model_cache import ModelCache
from hare.exceptions import QueryError
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.tenant_values import TenantValues

if TYPE_CHECKING:
    from hare.core.apps.declarations import ModelReference
    from hare.core.hare_context import HareContext
    from hare.models import Model


class ModelTenants:
    """A tenant scope given model by model - ``Tenancy.scope({Order: Tenancy.any_of("msk", "kzn"),
    "shop.Faq": Tenancy.ALL})``. A model takes the entry of its own class, else of its nearest base class
    with the same ``Meta.tenant_field`` - for a ``Meta.tenant_schema`` model without one, with
    ``Meta.tenant_schema``. A ``Meta.tenant_schema`` model's entry is one value: its tables are read in
    one tenant's schema.

    Args:
        scopes: Each model's tenant value, ``Tenancy.any_of(...)`` or ``Tenancy.ALL`` - a model given by
            its class, its ``"app.Model"`` label or a ``swappable()`` setting.

    Raises:
        QueryError: A key isn't a model with ``Meta.tenant_field`` or ``Meta.tenant_schema``, an entry
            is None or a collection, or a ``Meta.tenant_schema`` model's entry is several values or
            ``Tenancy.ALL``.
        ConfigurationError: A label names no model of the current context.
    """

    __slots__ = ("scopes", "schema_tenants", "policy_tenants")

    #: Connection results depend on every registered model, including models added after the scope.
    schema_tenant_cache: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True
    )
    policy_tenant_cache: ClassVar[Cache[Any]] = Cache(
        holds_sql=False, keyed_by_model=False, depends_on_other_models=True
    )

    def __init__(self, scopes: Mapping[ModelReference, Any]) -> None:
        self.scopes: dict[type[Model], Any] = {}
        #: The tenant schema by context and connection alias; an alias alone doesn't identify a registry.
        self.schema_tenants: dict[tuple[HareContext | None, str], Any] | None = None
        #: Each ``TenantCondition`` table's tenants, by context and connection alias.
        self.policy_tenants: dict[tuple[HareContext | None, str], dict[str, Any]] | None = None
        for reference, scope in scopes.items():
            model = ModelTenants.get_model(reference)
            meta = getattr(model, "_meta", None)
            if not isinstance(model, type) or meta is None or not (meta.tenant_field or meta.tenant_schema):
                raise QueryError(
                    f"Tenancy.scope(): {model!r} is not a model with Meta.tenant_field or Meta.tenant_schema"
                )
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
            scope = TenantValues.get_single_value_or_scope(scope)
            if meta.tenant_schema and type(scope) in {TenantValues, AllTenants}:
                raise QueryError(
                    f"Tenancy.scope(): {model.__name__} has Meta.tenant_schema and is given {scope!r} - its "
                    "tables are read in the schema of one tenant; give it one tenant value"
                )
            self.scopes[model] = scope

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
        """The base classes of ``model`` that are models with the same ``Meta.tenant_field`` - of a
        ``Meta.tenant_schema`` model without one, with ``Meta.tenant_schema`` - nearest first: a scope
        given for one of them is the model's too."""
        meta = model._meta
        if meta.tenant_field is None:
            return tuple(
                cast("type[Model]", base_class)
                for base_class in model.__mro__[1:]
                if hasattr(base_class, "_meta") and base_class._meta.tenant_schema
            )
        return tuple(
            cast("type[Model]", base_class)
            for base_class in model.__mro__[1:]
            if hasattr(base_class, "_meta") and base_class._meta.tenant_field == meta.tenant_field
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
        tenant_bases = ModelTenants.get_tenant_bases(model)
        for base_class in tenant_bases:
            if base_class in scopes:
                return scopes[base_class]
        for candidate in (model, *tenant_bases):
            for scoped_model, scope in scopes.items():
                if scoped_model._meta.full_name == candidate._meta.full_name:
                    return scope
        return None

    def get_schema_tenant(self, connection_alias: str) -> Any:
        """The tenant whose schema a connection works in under the scope - the one value the scope gives
        the connection's ``Meta.tenant_schema`` models. Results are reused until the model registry
        changes; a newly registered model participates in the next check. Contexts with the same
        connection alias keep separate results.

        Args:
            connection_alias: The connection.

        Returns:
            The tenant value; None when the scope names none of the connection's ``Meta.tenant_schema``
            models - only its shared tables are reached.

        Raises:
            QueryError: The scope gives the connection's ``Meta.tenant_schema`` models different values - a
                connection works in one schema.
        """
        # Local import: the context imports the query package.
        from hare.core.hare_context import HareContext

        key = (HareContext.get_current(), connection_alias)
        schema_tenants = self.schema_tenants
        if schema_tenants is None:
            schema_tenants = self.schema_tenants = self.schema_tenant_cache.new_shared_bucket()
        if key in schema_tenants:
            return schema_tenants[key]
        tenants: dict[Any, list[str]] = {}
        for model in ModelTenants.get_context_models():
            meta = model._meta
            if not meta.tenant_schema or meta.abstract or meta.default_connection != connection_alias:
                continue
            tenant = self.get_for_model(model)
            if tenant is not None:
                # Integers and their text name the same schema (1 and "1" both select tenant_1).
                schema_tenant = str(tenant) if type(tenant) is int else tenant
                tenants.setdefault(schema_tenant, []).append(model.__name__)
        if len(tenants) > 1:
            given = "; ".join(f"{', '.join(names)}: {tenant!r}" for tenant, names in tenants.items())
            raise QueryError(
                f"Tenancy.scope() gives the Meta.tenant_schema models of connection {connection_alias!r} "
                f"different tenants ({given}) - a connection works in the schema of one tenant"
            )
        tenant = next(iter(tenants), None)
        schema_tenants[key] = tenant
        return tenant

    def get_policy_tenants(self, connection_alias: str) -> dict[str, Any]:
        """The tenants the scope gives each table of a connection with a ``TenantCondition`` policy - what
        a transaction of the connection sets when it begins. Model registration changes invalidate
        the cached table mapping before the next transaction uses it. Each context has its own mapping
        even when its connection alias matches another context's.

        Args:
            connection_alias: The connection.

        Returns:
            Each table's scope - a tenant value, a ``TenantValues`` or ``Tenancy.ALL`` - by the name
            the policy reads it under (``TenantCondition.get_table_key()``); a table of a model the
            scope doesn't name is left out.
        """
        # Local import: the context imports the query package.
        from hare.core.hare_context import HareContext

        key = (HareContext.get_current(), connection_alias)
        connection_tenants = self.policy_tenants
        if connection_tenants is None:
            connection_tenants = self.policy_tenants = self.policy_tenant_cache.new_shared_bucket()
        policy_tenants = connection_tenants.get(key)
        if policy_tenants is None:
            # Local import: the conditions import the models, which import the query package.
            from hare.ddl.conditions.tenant_condition import TenantCondition

            policy_tenants = {}
            for model in ModelTenants.get_context_models():
                meta = model._meta
                if not meta.tenant_row_level_security or meta.abstract or meta.default_connection != connection_alias:
                    continue
                tenant = self.get_for_model(model)
                if tenant is not None:
                    policy_tenants[TenantCondition.get_table_key(model)] = tenant
            connection_tenants[key] = policy_tenants
        return policy_tenants

    @staticmethod
    def get_context_models() -> list[type[Model]]:
        """The models of the current context.

        Returns:
            The models; none outside a context.
        """
        # Local import: the context imports the query package.
        from hare.core.hare_context import HareContext

        context = HareContext.get_current()
        apps = None if context is None else context.apps
        return [] if apps is None else list(apps.get_models_iterable())

    def __eq__(self, other: object) -> bool:
        # A scope of the same entries is the same scope - entered again inside a transaction begun in it.
        return type(other) is ModelTenants and other.scopes == self.scopes

    def __hash__(self) -> int:
        return hash(frozenset(self.scopes.items()))

    def __repr__(self) -> str:
        entries = ", ".join(f"{model.__name__}: {scope!r}" for model, scope in self.scopes.items())
        return f"{{{entries}}}"
