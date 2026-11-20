"""Multi-tenancy for models with ``Meta.tenant_field``: the default manager limits every query to the
tenants the active scope (``Tenancy.scope()``) gives the model.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Collection, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.model_cache import ModelCache
from hare.dialects.base.constants import SQL_DIALECT
from hare.exceptions import QueryError, ValidationError
from hare.models.tenancy.constants import TENANT_RELATION_CHECK_BATCH_SIZE
from hare.query.key_columns import KeyColumns
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.model_tenants import ModelTenants
from hare.query.scopes.tenants.tenant_values import TenantValues

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.models.model_binding import ModelBinding


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
    def get_scope(cls, binding: ModelBinding) -> Any:
        """The tenant scope of one model in the current task.

        Args:
            binding: The model.

        Returns:
            A tenant value, a ``TenantValues`` (several values), ``Tenancy.ALL``, or None when
            no scope is active, the active one doesn't name the model, or the model has neither
            ``Meta.tenant_field`` nor ``Meta.tenant_schema``.
        """
        if not binding.tenant_field and not binding.tenant_schema:
            return None
        scope = cls.current.get()
        return scope.get_for_model(binding) if type(scope) is ModelTenants else scope

    @classmethod
    def allows(cls, binding: ModelBinding, scope: Any, value: Any) -> bool:
        """Whether a model's scope allows a value of its ``Meta.tenant_field``.

        Args:
            binding: The model.
            scope: The model's scope (``get_scope(model)``), not None.
            value: The value.

        Returns:
            True when the value is the scope's, one of its values, or the scope is ``Tenancy.ALL``.
        """
        scope_class = type(scope)
        if scope_class is TenantValues:
            return any(cls.is_same_tenant(binding, value, allowed_value) for allowed_value in scope.values)
        if scope_class is AllTenants:
            return True
        return cls.is_same_tenant(binding, value, scope)

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
            return scope.values
        return (scope,)

    @classmethod
    def set(cls, tenant: Any) -> contextvars.Token[Any | None]:
        """Sets the tenant scope active for the current task - what ``scope()`` takes. Prefer the
        scope() context manager unless you need to manage the token's lifetime yourself (e.g.
        across an await boundary a single `with` block can't span).

        Raises:
            QueryError: A scope given model by model names something that isn't a model with
                ``Meta.tenant_field`` or ``Meta.tenant_schema``, gives a model None, or gives a
                ``Meta.tenant_schema`` model several values or ``Tenancy.ALL``; or ``tenant`` is a list,
                a tuple or a set - several values are ``Tenancy.any_of(*values)``.
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
    def get_tenant_relation(binding: ModelBinding, tenant_field: str) -> tuple[str, str] | None:
        """Find the relation and target field mapped to the tenant column."""
        relation_key = binding.foreign_key_shadow_columns.get(tenant_field)
        if not relation_key:
            return None
        relation_name = relation_key[1:]
        relation: RelationalField[Any] = binding.fields_map[relation_name]  # type: ignore[assignment]
        position = relation.source_fields.index(tenant_field)
        return relation_name, relation.to_field_instances[position].model_field_name

    @staticmethod
    def get_given_tenant(binding: ModelBinding, values: Mapping[str, Any]) -> tuple[bool, Any, str]:
        """Finds the ``Meta.tenant_field`` value ``values`` gives - directly, or, when the tenant
        field is a foreign key's shadow column, through that relation's instance.

        Args:
            binding: The tenant-scoped model.
            values: The field values of a new row.

        Returns:
            ``(whether a value is given, the value, a description of the entry it came from)``.
        """
        tenant_field: str = binding.tenant_field  # type: ignore[assignment]
        if tenant_field in values:
            return True, values[tenant_field], f"{tenant_field}={values[tenant_field]!r}"
        relation = Tenancy.get_tenant_relation(binding, tenant_field)
        if relation is None:
            return False, None, ""
        relation_name, target_field = relation
        if relation_name not in values:
            return False, None, ""
        related_instance = values[relation_name]
        if related_instance is None:
            return True, None, f"{relation_name}=None"
        return True, getattr(related_instance, target_field), f"{relation_name}={related_instance!r}"

    @staticmethod
    def get_several_values_message(binding: ModelBinding, scope: Any, operation: str) -> str:
        """The message of a write that names no tenant under a scope of several values.

        Args:
            binding: The tenant-scoped model.
            scope: The model's scope.
            operation: The writing method.
        """
        model = binding._model
        tenant_field: str = binding.tenant_field  # type: ignore[assignment]
        return (
            f"{operation}() on {model.__name__} names no '{tenant_field}' and the active tenant "
            f"scope ({binding.fields_map[tenant_field].get_value_for_message(scope)}) "
            "has no single value to give it - pass it directly"
        )

    @classmethod
    @contextmanager
    def scope_if_unset(cls, binding: ModelBinding, tenant: Any) -> Generator[None]:
        """Fill a model's missing tenant scope without changing any other model's scope.

        An existing scalar or model-specific scope retains its precedence. Otherwise the
        new model entry extends the current model mapping for this block only.
        """
        model = binding._model
        if cls.get_scope(binding) is not None:
            yield
            return
        current = cls.current.get()
        scopes = current.scopes if type(current) is ModelTenants else {}
        with cls.scope({**scopes, model: tenant}):
            yield

    @classmethod
    @contextmanager
    def scope(cls, tenant: Any) -> Generator[None]:
        """Scopes the default-manager queries of every ``Meta.tenant_field`` model to the given tenants
        for the block. A nested block replaces the outer scope until it exits.

        ``tenant`` is a tenant value (for every model alike), ``Tenancy.any_of(...)`` (several
        values), ``Tenancy.ALL`` (every tenant), or a dict of those by model - its class or its
        ``"app.Model"`` label; a model takes the entry of its own class, else of its nearest base class
        with the same ``Meta.tenant_field`` (with ``Meta.tenant_schema`` for a ``Meta.tenant_schema``
        model without one). A ``Meta.tenant_schema`` model's entry is one value, the tenant whose schema
        its connection works in.

        A row written without a tenant gets the model's value when its scope is one value; otherwise
        the write has to name it. ``<Model>.objects.all_tenants()`` spans every tenant for one
        query.

        Raises:
            QueryError: A dict names something that isn't a model with ``Meta.tenant_field`` or
                ``Meta.tenant_schema``, gives a model None, or gives a ``Meta.tenant_schema`` model
                several values or ``Tenancy.ALL``.
        """
        token = cls.set(tenant)
        try:
            yield
        finally:
            cls.reset(token)

    @staticmethod
    def is_same_tenant(binding: ModelBinding, first: Any, second: Any) -> bool:
        """Whether two values of ``model``'s ``Meta.tenant_field`` name the same tenant, compared as
        the field converts them - a ``str`` and a ``uuid.UUID`` of a ``UUIDField`` tenant are equal.

        Args:
            binding: The model whose ``Meta.tenant_field`` both values belong to.
            first: One tenant value.
            second: The other tenant value.

        Returns:
            True if both name the same tenant.
        """
        if first == second:
            return True
        if first is None or second is None:
            return False
        tenant_field = binding.fields_map[binding.tenant_field]  # type: ignore[index]
        try:
            return SQL_DIALECT.types.get_python_value(tenant_field, first) == SQL_DIALECT.types.get_python_value(
                tenant_field, second
            )
        except (ValidationError, TypeError, ValueError):
            return False

    @staticmethod
    @ModelCache.fact()
    def has_tenant_scoped_relations(binding: ModelBinding) -> bool:
        """Whether a forward relation of ``model`` points at a model with ``Meta.tenant_field``."""
        return any(
            binding.fields_map[field_name].get_related_binding().tenant_field  # type: ignore[attr-defined]
            for field_name in binding.forward_key_relation_names
        )

    @staticmethod
    def get_relation_field_names(binding: ModelBinding) -> tuple[str, ...]:
        """The forward relation source columns and tenant read for one operation."""
        names = [
            source_field
            for field_name in binding.forward_key_relation_names
            for source_field in binding.fields_map[field_name].source_fields  # type: ignore[attr-defined]
        ]
        if binding.tenant_field:
            names.append(binding.tenant_field)
        return tuple(names)

    @staticmethod
    def get_relation_row(obj: Model, field_names: tuple[str, ...]) -> dict[str, Any]:
        """Read a relation-check row, preserving unloaded fields as NULL."""
        return {name: getattr(obj, name, None) for name in field_names}

    @classmethod
    async def check_objects_relation_targets(
        cls,
        binding: ModelBinding,
        objects: Sequence[Model],
        connection: DatabaseClient,
        field_names: Iterable[str] | None = None,
    ) -> None:
        """Rejects written instances whose relation names a row of another tenant - see
        ``check_relation_targets()``.

        Raises:
            QueryError: A relation names another tenant's row.
        """
        if cls.has_tenant_scoped_relations(binding):
            relation_names = cls.get_relation_field_names(binding)
            rows = [cls.get_relation_row(obj, relation_names) for obj in objects]
            await cls.check_relation_targets(
                binding, rows, connection, None if field_names is None else set(field_names)
            )

    @classmethod
    async def check_relation_targets(
        cls,
        binding: ModelBinding,
        rows: Sequence[Mapping[str, Any]],
        connection: DatabaseClient,
        field_names: Collection[str] | None = None,
    ) -> None:
        """Rejects written rows whose forward relation names a row of a tenant-scoped model its scope
        doesn't show. A related model with no scope, or with ``Tenancy.ALL``, isn't checked; a key
        naming no row is left to the database - except on a table of a ``TenantCondition`` policy of a
        connection with ``tenant_row_level_security``, where the policy hides the rows of other tenants
        from the check while the database's foreign key still accepts them.

        Args:
            binding: The model the rows are written to.
            rows: Each row's relation key columns and tenant, by attribute name.
            connection: The source write client. Related rows inherit it when their configured
                route matches the source model's; otherwise their own route is selected before
                key encoding and tenant checks.
            field_names: The fields written, None for every field.

        Raises:
            QueryError: A relation names another tenant's row.
        """
        if cls.current.get() is None:
            return
        from hare.core.connections.model_connection_choice import ModelConnectionChoice
        from hare.core.hare_context import HareContext

        selected_names = None if field_names is None else frozenset(field_names)
        for field_name in sorted(binding.forward_key_relation_names):
            field: RelationalField[Model] = binding.fields_map[field_name]  # type: ignore[assignment]
            related_model = field.related_model
            related_binding = field.get_related_binding()
            related_tenant_field = related_binding.tenant_field
            if related_tenant_field is None:
                continue
            related_scope = cls.get_scope(related_binding)
            if related_scope is None or related_scope is cls.ALL:
                continue
            if (
                selected_names is not None
                and field_name not in selected_names
                and selected_names.isdisjoint(field.source_fields)
            ):
                continue
            source_fields = field.source_fields
            keys: dict[tuple[Any, ...], None] = {}
            related_connection = connection
            writers: tuple[Callable[[Any, Any], Any], ...] | None = None
            for row in rows:
                values = [row.get(source_field) for source_field in source_fields]
                if any(value is None for value in values):
                    continue
                if writers is None:
                    # A nullable relation with no key needs neither a target client nor writers.
                    related_connection = ModelConnectionChoice.get_related_write_client(
                        HareContext.require_current(), related_binding, binding, connection
                    ) or ModelConnectionChoice.get(HareContext.require_current(), related_binding, True)
                    writers = tuple(
                        related_connection.dialect.types.get_db_writer(column) for column in field.to_field_instances
                    )
                key = tuple(write(value, related_model) for value, write in zip(values, writers, strict=True))
                keys[key] = None
            if keys:
                await cls._check_related_tenants(binding, field_name, field, keys, related_scope, related_connection)

    @classmethod
    async def _check_related_tenants(
        cls,
        binding: ModelBinding,
        field_name: str,
        field: RelationalField[Model],
        keys: Collection[tuple[Any, ...]],
        related_scope: Any,
        connection: DatabaseClient,
    ) -> None:
        """Reads the tenant of each related row a relation names and compares it with the related
        model's scope - a key of a row of the scope passes, also where a database keeping no key unique
        holds another tenant's row of it too. Under row level security a key whose row the read doesn't
        see is refused."""
        model = binding._model
        related_model = field.related_model
        related_meta = field.get_related_binding()
        table = related_meta.basetable
        key_columns = [related_meta.fields_db_projection[name] for name in field.to_field_names]
        tenant_column = related_meta.fields_db_projection[related_meta.tenant_field]  # type: ignore[index]
        rows_hidden_by_policy = related_meta.tenant_row_level_security and connection.tenant_row_level_security
        ordered_keys = list(keys)
        tenant_field = related_meta.fields_map[related_meta.tenant_field]  # type: ignore[index]
        read_tenant = connection.dialect.types.get_python_reader(tenant_field)
        for start in range(0, len(ordered_keys), TENANT_RELATION_CHECK_BATCH_SIZE):
            batch = ordered_keys[start : start + TENANT_RELATION_CHECK_BATCH_SIZE]
            query = (
                connection.query_class.from_(table)
                .select(
                    table[tenant_column].as_("tenant"),
                    *(table[column].as_(f"key_{position}") for position, column in enumerate(key_columns)),
                )
                .where(KeyColumns.row_is_in([table[column] for column in key_columns], batch))
            )
            _, related_rows = await connection._execute_built(*query.get_parameterized_sql())
            allowed_keys: set[tuple[Any, ...]] = set()
            refused_tenants: dict[tuple[Any, ...], Any] = {}
            for related_row in related_rows:
                row_key = tuple(related_row[f"key_{position}"] for position in range(len(key_columns)))
                related_tenant = read_tenant(related_row["tenant"])
                if cls.allows(field.get_related_binding(), related_scope, related_tenant):
                    allowed_keys.add(row_key)
                else:
                    refused_tenants.setdefault(row_key, related_tenant)
            for row_key, related_tenant in refused_tenants.items():
                if row_key not in allowed_keys:
                    raise QueryError(
                        f"{model.__name__}.{field_name}: the {related_model.__name__} row it points at "
                        f"belongs to tenant {related_tenant!r}, not the active {related_scope!r} - a "
                        "relation can't cross tenants."
                    )
            # A key of a policy's table is unique: fewer rows than keys - a row the policy hides.
            if rows_hidden_by_policy and len(allowed_keys) < len(batch):
                raise QueryError(
                    f"{model.__name__}.{field_name}: a {related_model.__name__} row it points at isn't one "
                    f"the active {related_scope!r} shows - row level security hides the rows of other "
                    "tenants, and a relation can't cross tenants."
                )
