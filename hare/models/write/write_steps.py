from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.instrumentation.change_events import ChangeEvents
from hare.models.tenancy.tenancy import Tenancy
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.tenant_values import TenantValues

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.instrumentation.enums import RowOperation
    from hare.models import Model


class WriteSteps:
    """The checks and reports every write of instances runs."""

    @staticmethod
    def scope_to_active_tenant(
        model: type[Model], objs: Iterable[Model], operation: str, *, fills_missing: bool, requires_active: bool
    ) -> Any:
        """Checks that objs of a ``Meta.tenant_field`` model belong to the model's tenant scope.

        Args:
            model: The tenant-scoped model.
            objs: The objs written.
            operation: The writing method, for the messages.
            fills_missing: Give an instance without a tenant the scope's one value (an insert).
            requires_active: Refuse to write when the model has no scope - otherwise an instance's
                own tenant is trusted then (seed data written outside a ``Tenancy.scope()``).

        Returns:
            The model's scope, None when it has none.

        Raises:
            QueryError: An instance belongs to a tenant outside the scope; an instance has none
                and the scope has several values; or the model has no scope and either
                ``requires_active`` is set or an instance has no tenant.
        """
        tenant_field = model._meta.tenant_field
        scope = Tenancy.get_scope(model)
        scope_class = type(scope)
        if scope is None and requires_active:
            raise QueryError(
                f"{model.__name__} has Meta.tenant_field '{tenant_field}' set but no tenant is active - "
                f"wrap this call in Tenancy.scope(...), or use {model.__name__}.objects.all_tenants() to "
                f"intentionally {operation}() across every tenant"
            )
        has_one_value = scope is not None and scope_class is not TenantValues and scope_class is not AllTenants
        mismatched_pks = []
        for instance in objs:
            current_value = getattr(instance, tenant_field, None)  # type: ignore[arg-type]
            if current_value is None:
                if not fills_missing:
                    mismatched_pks.append(instance.pk)
                elif scope is None:
                    raise QueryError(
                        f"{model.__name__} has Meta.tenant_field '{tenant_field}' set but it's unset on "
                        "this instance and no tenant is active either - wrap this call in "
                        "Tenancy.scope(...), or set it directly"
                    )
                elif not has_one_value:
                    raise QueryError(Tenancy.get_several_values_message(model, scope, operation))
                else:
                    setattr(instance, tenant_field, scope)  # type: ignore[arg-type]
            elif has_one_value:
                if not Tenancy.is_same_tenant(model, current_value, scope):
                    mismatched_pks.append(instance.pk)
            elif scope is not None and not Tenancy.allows(model, scope, current_value):
                mismatched_pks.append(instance.pk)
        if mismatched_pks:
            raise QueryError(
                f"{operation}() on {model.__name__} would write {tenant_field}= of a tenant other than the "
                f"active one ({scope!r}): pk(s) {mismatched_pks}"
            )
        return scope

    @staticmethod
    async def report(
        connection: DatabaseClient,
        model: type[Model],
        operation: RowOperation,
        *,
        instances: Sequence[Model] | None = None,
        pks: Sequence[Any] | None = None,
        fields: Iterable[str] | None = None,
    ) -> None:
        """Reports rows a write changed (``ChangeEvents``) - nothing without a listener.

        Args:
            connection: The connection the write ran on.
            model: The model.
            operation: The operation.
            instances: The written instances - their primary keys name the rows.
            pks: The primary keys of the rows, instead of ``instances``.
            fields: The fields written, None when not known.
        """
        if not ChangeEvents.is_observed(model):
            return
        if instances is not None:
            if not instances:
                return
            pks = (
                [instance.pk for instance in instances]
                if model._meta.has_primary_key and all(instance.pk is not None for instance in instances)
                else None
            )
        await ChangeEvents.report(connection, model, operation, pks=pks, fields=fields)
