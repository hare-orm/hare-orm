"""Multi-tenancy for models with ``Meta.tenant_field``: the default manager limits every query to the
tenants the active scope (``Tenancy.scope()``) gives the model.
"""

from __future__ import annotations

import contextvars
from collections.abc import Collection, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.caching.model_cache import ModelCache
from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import QueryError, ValidationError
from hare.models.tenancy.constants import TENANT_RELATION_CHECK_BATCH_SIZE
from hare.query.key_columns import KeyColumns
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.query.scopes.tenants.tenant_values import TenantValues

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class Tenancy:
    """Scopes ``Meta.tenant_field``-enabled models' default-manager queries to the tenants active
    for the current task - see ``Tenancy.scope()``."""

    #: The tenant scope active for the current task, None when none is: a tenant value for every
    #: model, a ``TenantValues``, ``Tenancy.ALL``, or a ``ModelTenants`` holding one of those per
    #: model.
    current: contextvars.ContextVar[Any | None] = RowVisibility.active_tenant
    #: Every tenant - as the whole scope, or as one model's entry of a scope given model by model.
    ALL: ClassVar[AllTenants] = AllTenants()

    @staticmethod
    def any_of(*values: Any) -> TenantValues:
        """Several tenant values a scope allows at once - ``Tenancy.scope(Tenancy.any_of("msk",
        "kzn"))``.

        Args:
            values: The values.

        Returns:
            The values as a scope.

        Raises:
            QueryError: No value is given, or one of them is None.
        """
        return TenantValues(*values)

    @classmethod
    def get_scope(cls, model: type[Model]) -> Any:
        """The tenant scope of one model in the current task.

        Args:
            model: The model.

        Returns:
            A tenant value, a ``TenantValues`` (several values), ``Tenancy.ALL``, or None when
            no scope is active, the active one doesn't name the model, or the model has no
            ``Meta.tenant_field``.
        """
        if not model._meta.tenant_field:
            return None
        scope = cls.current.get()
        return scope.get_for_model(model) if type(scope) is ModelTenants else scope

    @classmethod
    def allows(cls, model: type[Model], scope: Any, value: Any) -> bool:
        """Whether a model's scope allows a value of its ``Meta.tenant_field``.

        Args:
            model: The model.
            scope: The model's scope (``get_scope(model)``), not None.
            value: The value.

        Returns:
            True when the value is the scope's, one of its values, or the scope is ``Tenancy.ALL``.
        """
        scope_class = type(scope)
        if scope_class is TenantValues:
            return any(cls.is_same_tenant(model, value, allowed_value) for allowed_value in scope.values)
        if scope_class is AllTenants:
            return True
        return cls.is_same_tenant(model, value, scope)

    @staticmethod
    def has_one_value(scope: Any) -> bool:
        """Whether a model's scope is one tenant value - the value a row written without one gets.

        Args:
            scope: The model's scope, not None.
        """
        return type(scope) not in {TenantValues, AllTenants}

    @staticmethod
    def get_values(scope: Any) -> tuple[Any, ...] | None:
        """The tenant values a model's scope limits its rows to.

        Args:
            scope: The model's scope.

        Returns:
            The values; None when the rows aren't limited (no scope, or ``Tenancy.ALL``).
        """
        scope_class = type(scope)
        if scope is None or scope_class is AllTenants:
            return None
        if scope_class is TenantValues:
            return cast("tuple[Any, ...]", scope.values)
        return (scope,)

    @classmethod
    def set(cls, tenant: Any) -> contextvars.Token[Any | None]:
        """Sets the tenant scope active for the current task - what ``scope()`` takes. Prefer the
        scope() context manager unless you need to manage the token's lifetime yourself (e.g.
        across an await boundary a single `with` block can't span).

        Raises:
            QueryError: A scope given model by model names something that isn't a model with
                ``Meta.tenant_field``, or gives a model None; or ``tenant`` is a list, a tuple
                or a set - several values are ``Tenancy.any_of(*values)``.
        """
        if isinstance(tenant, dict):
            tenant = ModelTenants(tenant)
        elif type(tenant) is TenantValues:
            tenant = TenantValues.get_single_value_or_scope(tenant)
        elif isinstance(tenant, (list, tuple, set, frozenset)):
            raise QueryError(
                f"Tenancy.scope() takes one tenant value, got {tenant!r} - give several values as "
                "Tenancy.any_of(*values)"
            )
        return cls.current.set(tenant)

    @classmethod
    def reset(cls, token: contextvars.Token[Any | None]) -> None:
        """Undoes a set() call, restoring whatever was active before it."""
        cls.current.reset(token)

    @staticmethod
    def get_given_tenant(model: type[Model], values: dict[str, Any]) -> tuple[bool, Any, str]:
        """Finds the ``Meta.tenant_field`` value ``values`` gives - directly, or, when the tenant
        field is a foreign key's shadow column, through that relation's instance.

        Args:
            model: The tenant-scoped model.
            values: The field values of a new row.

        Returns:
            ``(whether a value is given, the value, a description of the entry it came from)``.
        """
        tenant_field = cast("str", model._meta.tenant_field)
        if tenant_field in values:
            return True, values[tenant_field], f"{tenant_field}={values[tenant_field]!r}"
        relation_key = model._meta.foreign_key_shadow_columns.get(tenant_field)
        relation_name = relation_key[1:] if relation_key else None
        if relation_name is None or relation_name not in values:
            return False, None, ""
        related_instance = values[relation_name]
        if related_instance is None:
            return True, None, f"{relation_name}=None"
        relation_field = cast("RelationalField[Any]", model._meta.fields_map[relation_name])
        to_field = relation_field.to_field_instances[relation_field.source_fields.index(tenant_field)]
        return True, getattr(related_instance, to_field.model_field_name), f"{relation_name}={related_instance!r}"

    @classmethod
    def fill_create_values(cls, model: type[Model], values: dict[str, Any]) -> None:
        """Puts the model's one tenant into the values of a row being created, unless they name one.

        A tenant named with no scope for the model is trusted as given, like any tenant-scoped
        object constructed outside a ``Tenancy.scope()`` block (seed data).

        Args:
            model: The tenant-scoped model.
            values: The field values of the new row, changed in place.

        Raises:
            QueryError: The tenant named is outside the model's scope; or none is named and the
                model has no scope or a scope of several values.
        """
        tenant_field = cast("str", model._meta.tenant_field)
        scope: Any = cls.current.get()
        scope_class = type(scope)
        if scope_class is ModelTenants:
            scope = scope.get_for_model(model)
            scope_class = type(scope)
        tenant_given, given_tenant, tenant_source = cls.get_given_tenant(model, values)
        if tenant_given:
            if scope is not None and not cls.allows(model, scope, given_tenant):
                raise QueryError(
                    f"create() on {model.__name__} received {tenant_source}, giving {tenant_field}="
                    f"{given_tenant!r}, which does not match the active tenant scope ({scope!r})"
                )
        elif scope is None:
            raise QueryError(
                f"{model.__name__} has Meta.tenant_field '{tenant_field}' set but no tenant is "
                "active and no explicit value was given - wrap this call in "
                "Tenancy.scope(...), or pass it directly"
            )
        elif scope_class is TenantValues or scope_class is AllTenants:
            raise QueryError(cls.get_several_values_message(model, scope, "create"))
        else:
            values[tenant_field] = scope

    @staticmethod
    def get_several_values_message(model: type[Model], scope: Any, operation: str) -> str:
        """The message of a write that names no tenant under a scope of several values.

        Args:
            model: The tenant-scoped model.
            scope: The model's scope.
            operation: The writing method.
        """
        return (
            f"{operation}() on {model.__name__} names no '{model._meta.tenant_field}' and the active tenant "
            f"scope ({scope!r}) has no single value to give it - pass it directly"
        )

    @classmethod
    @contextmanager
    def scope(cls, tenant: Any) -> Generator[None]:
        """Scopes the default-manager queries of every ``Meta.tenant_field`` model to the given tenants
        for the block. A nested block replaces the outer scope until it exits.

        ``tenant`` is a tenant value (for every model alike), ``Tenancy.any_of(...)`` (several
        values), ``Tenancy.ALL`` (every tenant), or a dict of those by model - a model takes the
        entry of its own class, else of its nearest base class with the same ``Meta.tenant_field``.

        A row written without a tenant gets the model's value when its scope is one value; otherwise
        the write has to name it. ``<Model>.objects.all_tenants()`` spans every tenant for one
        query.

        Raises:
            QueryError: A dict names something that isn't a model with ``Meta.tenant_field``, or
                gives a model None.
        """
        token = cls.set(tenant)
        try:
            yield
        finally:
            cls.reset(token)

    @staticmethod
    def is_same_tenant(model: type[Model], first: Any, second: Any) -> bool:
        """Whether two values of ``model``'s ``Meta.tenant_field`` name the same tenant, compared as
        the field converts them - a ``str`` and a ``uuid.UUID`` of a ``UUIDField`` tenant are equal.

        Args:
            model: The model whose ``Meta.tenant_field`` both values belong to.
            first: One tenant value.
            second: The other tenant value.

        Returns:
            True if both name the same tenant.
        """
        if first == second:
            return True
        if first is None or second is None:
            return False
        tenant_field = model._meta.fields_map[cast("str", model._meta.tenant_field)]
        try:
            return SQL_DIALECT.types.get_python_value(tenant_field, first) == SQL_DIALECT.types.get_python_value(
                tenant_field, second
            )
        except (ValidationError, TypeError, ValueError):
            return False

    @staticmethod
    @ModelCache.fact()
    def has_tenant_scoped_relations(model: type[Model]) -> bool:
        """Whether a forward relation of ``model`` points at a model with ``Meta.tenant_field``."""
        meta = model._meta
        return any(
            cast("RelationalField[Model]", meta.fields_map[field_name]).related_model._meta.tenant_field
            for field_name in meta.foreign_key_fields | meta.one_to_one_fields
        )

    @staticmethod
    def get_relation_row(obj: Model) -> dict[str, Any]:
        """The values ``check_relation_targets()`` reads off a written obj - its forward
        relations' key columns and its own ``Meta.tenant_field``."""
        meta = obj._meta
        names = [
            source_field
            for field_name in meta.foreign_key_fields | meta.one_to_one_fields
            for source_field in cast("RelationalField[Model]", meta.fields_map[field_name]).source_fields
        ]
        if meta.tenant_field:
            names.append(meta.tenant_field)
        return {name: getattr(obj, name, None) for name in names}

    @classmethod
    async def check_objects_relation_targets(
        cls,
        model: type[Model],
        objects: Sequence[Model],
        connection: DatabaseClient,
        field_names: Iterable[str] | None = None,
    ) -> None:
        """Rejects written instances whose relation names a row of another tenant - see
        ``check_relation_targets()``.

        Raises:
            QueryError: A relation names another tenant's row.
        """
        if cls.has_tenant_scoped_relations(model):
            rows = [cls.get_relation_row(obj) for obj in objects]
            await cls.check_relation_targets(
                model, rows, connection, None if field_names is None else set(field_names)
            )

    @classmethod
    async def check_relation_targets(
        cls,
        model: type[Model],
        rows: Sequence[Mapping[str, Any]],
        connection: DatabaseClient,
        field_names: Collection[str] | None = None,
    ) -> None:
        """Rejects written rows whose forward relation names a row of a tenant-scoped model its scope
        doesn't show. A related model with no scope, or with ``Tenancy.ALL``, isn't checked; a key
        naming no row is left to the database.

        Args:
            model: The model the rows are written to.
            rows: Each row's relation key columns and tenant, by attribute name.
            connection: The client the related rows are read through.
            field_names: The fields written, None for every field.

        Raises:
            QueryError: A relation names another tenant's row.
        """
        meta = model._meta
        if cls.current.get() is None:
            return
        for field_name in sorted(meta.foreign_key_fields | meta.one_to_one_fields):
            field = cast("RelationalField[Model]", meta.fields_map[field_name])
            related_model = field.related_model
            related_tenant_field = related_model._meta.tenant_field
            if related_tenant_field is None:
                continue
            related_scope = cls.get_scope(related_model)
            if related_scope is None or related_scope is cls.ALL:
                continue
            if (
                field_names is not None
                and field_name not in field_names
                and not set(field.source_fields) & set(field_names)
            ):
                continue
            keys: dict[tuple[Any, ...], None] = {}
            for row in rows:
                values = [row.get(source_field) for source_field in field.source_fields]
                if any(value is None for value in values):
                    continue
                key = tuple(
                    connection.dialect.types.get_db_value(to_field, value, related_model)
                    for value, to_field in zip(values, field.to_field_instances, strict=True)
                )
                keys[key] = None
            if keys:
                await cls._check_related_tenants(model, field_name, field, keys, related_scope, connection)

    @classmethod
    async def _check_related_tenants(
        cls,
        model: type[Model],
        field_name: str,
        field: RelationalField[Model],
        keys: Collection[tuple[Any, ...]],
        related_scope: Any,
        connection: DatabaseClient,
    ) -> None:
        """Reads the tenant of each related row a relation names and compares it with the related
        model's scope."""
        related_model = field.related_model
        related_meta = related_model._meta
        table = related_meta.basetable
        key_columns = [related_meta.fields_db_projection[name] for name in field.to_field_names]
        tenant_column = related_meta.fields_db_projection[cast("str", related_meta.tenant_field)]
        ordered_keys = list(keys)
        for start in range(0, len(ordered_keys), TENANT_RELATION_CHECK_BATCH_SIZE):
            batch = ordered_keys[start : start + TENANT_RELATION_CHECK_BATCH_SIZE]
            query = (
                connection.query_class.from_(table)
                .select(table[tenant_column].as_("tenant"))
                .where(KeyColumns.row_is_in([table[column] for column in key_columns], batch))
            )
            _, related_rows = await connection.execute(*query.get_parameterized_sql())
            tenant_field = related_meta.fields_map[cast("str", related_meta.tenant_field)]
            for related_row in related_rows:
                related_tenant = connection.dialect.types.get_python_value(tenant_field, related_row["tenant"])
                if not cls.allows(related_model, related_scope, related_tenant):
                    raise QueryError(
                        f"{model.__name__}.{field_name}: the {related_model.__name__} row it points at "
                        f"belongs to tenant {related_tenant!r}, not the active {related_scope!r} - a "
                        "relation can't cross tenants."
                    )
