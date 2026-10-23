from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import TYPE_CHECKING, Any, cast

from hare.exceptions import QueryError
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions.expression import Expression
from hare.query.scopes.tenants.all_tenants import AllTenants
from hare.query.scopes.tenants.tenant_values import TenantValues
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model


class TenantWriteRules:
    """The tenant rules of every write of a ``Meta.tenant_field`` model: a new row takes the active scope's
    tenant, an existing row is written only inside the scope, a row moves only to a tenant the scope allows,
    and a tenant hare can't check under a scope - read from source rows, written from an expression - is
    refused."""

    @staticmethod
    def scope_objects(
        model: type[Model], objs: Iterable[Model], operation: str, *, fills_missing: bool, requires_active: bool
    ) -> Any:
        """Checks that objects of a ``Meta.tenant_field`` model belong to the model's tenant scope.

        Args:
            model: The tenant-scoped model.
            objs: The objects written.
            operation: The writing method, for the messages.
            fills_missing: Give an object without a tenant the scope's one value (an insert).
            requires_active: Refuse to write when the model has no scope - otherwise an object's own
                tenant is trusted then (seed data written outside a ``Tenancy.scope()``).

        Returns:
            The model's scope, None when it has none.

        Raises:
            QueryError: An object belongs to a tenant outside the scope; an object has none and the scope
                has several values; or the model has no scope and either ``requires_active`` is set or an
                object has no tenant.
        """
        tenant_field = cast("str", model._meta.tenant_field)
        scope = Tenancy.get_scope(model)
        scope_class = type(scope)
        if scope is None and requires_active:
            raise QueryError(
                f"{model.__name__} has Meta.tenant_field {tenant_field!r} set but no tenant is active - "
                f"wrap this call in Tenancy.scope(...), or use {model.__name__}.objects.all_tenants() to "
                f"intentionally {operation}() across every tenant"
            )
        has_one_value = scope is not None and scope_class is not TenantValues and scope_class is not AllTenants
        mismatched_pks = []
        for obj in objs:
            current_value = getattr(obj, tenant_field, None)
            if current_value is None:
                if not fills_missing:
                    mismatched_pks.append(obj.pk)
                elif scope is None:
                    raise QueryError(
                        f"{model.__name__} has Meta.tenant_field {tenant_field!r} set but it's unset on "
                        "this instance and no tenant is active either - wrap this call in "
                        "Tenancy.scope(...), or set it directly"
                    )
                elif not has_one_value:
                    raise QueryError(Tenancy.get_several_values_message(model, scope, operation))
                else:
                    setattr(obj, tenant_field, scope)
            elif has_one_value:
                if not Tenancy.is_same_tenant(model, current_value, scope):
                    mismatched_pks.append(obj.pk)
            elif scope is not None and not Tenancy.allows(model, scope, current_value):
                mismatched_pks.append(obj.pk)
        if mismatched_pks:
            raise QueryError(
                f"{operation}() on {model.__name__} would write {tenant_field}= of a tenant other than the "
                f"active one ({scope!r}): pk(s) {mismatched_pks}"
            )
        return scope

    @staticmethod
    def check_existing_object(obj: Model, operation: str) -> None:
        """Raises if an existing object's ``Meta.tenant_field`` value conflicts with the model's tenant scope.
        Never fills an unset tenant field - the row exists already.

        Args:
            obj: The object.
            operation: The calling method, for the message.

        Raises:
            QueryError: The scope doesn't allow the object's tenant, or the tenant is unset with no scope to
                check against.
        """
        tenant_field = obj._meta.tenant_field
        if not tenant_field:
            return
        active_tenant = Tenancy.get_scope(obj.__class__)
        current_value = getattr(obj, tenant_field, None)
        if current_value is None:
            if active_tenant is None:
                raise QueryError(
                    f"{obj.__class__.__name__} has Meta.tenant_field {tenant_field!r} set but it's "
                    f"unset on this instance and no tenant is active either - wrap this {operation}() "
                    "call in Tenancy.scope(...)"
                )
            return
        if active_tenant is not None and not Tenancy.allows(obj.__class__, active_tenant, current_value):
            raise QueryError(
                f"{operation}() on {obj.__class__.__name__} would {operation} a row scoped to "
                f"{tenant_field}={current_value!r}, which does not match the active tenant scope "
                f"({active_tenant!r})"
            )

    @staticmethod
    def raise_if_rows_leave_scope(
        model: type[Model], values: Mapping[str, Any], all_tenants: bool, method_name: str, *, requires_scope: bool
    ) -> None:
        """Rejects an update setting ``Meta.tenant_field`` to anything but a tenant value the model's scope
        allows - rows move only between the tenants the scope allows.

        Args:
            model: The updated model.
            values: The written values, by field name - the tenant field, or the relation it is the key of.
            all_tenants: Whether the update reaches every tenant (``all_tenants()``).
            method_name: The writing method, for the message.
            requires_scope: Whether an update without a scope moves no row either (``QuerySet.update()``);
                otherwise its values are written as given (``merge()``).

        Raises:
            QueryError: The tenant field is set to a value outside the scope, to None or an expression, or
                the update has no tenant scope while one is required.
        """
        from hare.models import Model

        tenant_field = model._meta.tenant_field
        if tenant_field is None:
            return
        tenant_relation_key = model._meta.foreign_key_shadow_columns.get(tenant_field)
        tenant_relation_name = tenant_relation_key[1:] if tenant_relation_key else None
        if tenant_field in values:
            tenant = values[tenant_field]
        elif tenant_relation_name is not None and tenant_relation_name in values:
            tenant = values[tenant_relation_name]
            if isinstance(tenant, Model):
                relation_field = cast("RelationalField[Any]", model._meta.fields_map[tenant_relation_name])
                to_field = relation_field.to_field_instances[relation_field.source_fields.index(tenant_field)]
                tenant = getattr(tenant, to_field.model_field_name)
        else:
            return
        scope = None if all_tenants else Tenancy.get_scope(model)
        if scope is None and not requires_scope:
            return
        if (
            scope is None
            or tenant is None
            or isinstance(tenant, (Expression, Term))
            or not Tenancy.allows(model, scope, tenant)
        ):
            raise QueryError(
                f"Cannot set {tenant_field!r} via {method_name} - rows move only to a tenant the active "
                f"Tenancy.scope(...) allows {model.__name__}, given as a plain value"
            )

    @staticmethod
    def fill_new_values(
        model: type[Model], values: dict[str, Any], operation: str, *, all_tenants: bool = False
    ) -> None:
        """Gives the values of a new row the active scope's tenant, unless they name one. A tenant named with
        no scope for the model is trusted as given, like an object constructed outside a ``Tenancy.scope()``
        block (seed data); one written from an expression under a scope, which hare can't check, is refused.

        Args:
            model: The written model.
            values: The values of the new row, by field name - changed in place.
            operation: The writing method, for the messages.
            all_tenants: Whether the write reaches every tenant (``all_tenants()``) - the values are written as
                given.

        Raises:
            QueryError: The tenant is written from an expression under the scope or is outside it; or none is
                given and the model has no scope or a scope of several values.
        """
        tenant_field = model._meta.tenant_field
        if tenant_field is None or all_tenants:
            return
        scope = Tenancy.get_scope(model)
        tenant_given, given_tenant, tenant_source = Tenancy.get_given_tenant(model, values)
        if tenant_given and isinstance(given_tenant, (Expression, Term)):
            if scope is not None:
                raise QueryError(
                    f"{operation}() writes {tenant_field!r} from an expression under the active tenant scope - give "
                    "a plain value, or call all_tenants() on the target"
                )
            return
        if tenant_given:
            if scope is not None and not Tenancy.allows(model, scope, given_tenant):
                raise QueryError(
                    f"{operation}() on {model.__name__} received {tenant_source}, giving {tenant_field}="
                    f"{given_tenant!r}, which does not match the active tenant scope ({scope!r})"
                )
        elif scope is None:
            raise QueryError(
                f"{model.__name__} has Meta.tenant_field {tenant_field!r} set but no tenant is "
                "active and no explicit value was given - wrap this call in "
                "Tenancy.scope(...), or pass it directly"
            )
        elif not Tenancy.has_one_value(scope):
            raise QueryError(Tenancy.get_several_values_message(model, scope, operation))
        else:
            values[tenant_field] = scope

    @staticmethod
    def raise_if_unchecked_relations(
        model: type[Model], values: Mapping[str, Any], all_tenants: bool, method_name: str
    ) -> None:
        """Rejects a relation to a tenant-scoped model written from an expression under the related model's
        active scope - by its name or by a column of its key - or by some columns of its key alone: the rows it
        points at can't be checked.

        Args:
            model: The written model.
            values: The written values, by field name.
            all_tenants: Whether the write reaches every tenant.
            method_name: The writing method, for the message.

        Raises:
            QueryError: See above.
        """
        if all_tenants:
            return
        meta = model._meta
        for relation_name in meta.foreign_key_fields | meta.one_to_one_fields:
            relation = cast("RelationalField[Any]", meta.fields_map[relation_name])
            key_names = relation.source_fields
            written_names = [name for name in (relation_name, *key_names) if name in values]
            if not written_names:
                continue
            related_model = relation.related_model
            if not related_model._meta.tenant_field or Tenancy.get_scope(related_model) is None:
                continue
            for name in written_names:
                if isinstance(values[name], (Expression, Term)):
                    raise QueryError(
                        f"{method_name} writes {name!r} from an expression under the active tenant scope of "
                        f"{related_model.__name__} - the rows it points at can't be checked; call all_tenants() on "
                        "the target"
                    )
            written_key_names = [name for name in key_names if name in values]
            if written_key_names and len(written_key_names) != len(key_names):
                raise QueryError(
                    f"{method_name} writes {', '.join(written_key_names)} of the key of {relation_name!r} without "
                    f"the rest under the active tenant scope of {related_model.__name__} - the rows it points at "
                    "can't be checked; write every column of the key, or call all_tenants() on the target"
                )

    @staticmethod
    def raise_if_source_tenants_unchecked(
        model: type[Model], written_columns: Collection[str], all_tenants: bool, method_name: str
    ) -> None:
        """Rejects tenants read from source rows while a scope is active - a written tenant, a relation to a
        tenant-scoped model - which a write of rows selected in the database can't check row by row.

        Args:
            model: The written model.
            written_columns: The columns written from the source rows.
            all_tenants: Whether the write reaches every tenant.
            method_name: The writing method, for the message.

        Raises:
            QueryError: A tenant would be read from the source rows under an active scope.
        """
        if all_tenants:
            return
        meta = model._meta
        tenant_field = meta.tenant_field
        if (
            tenant_field is not None
            and meta.fields_db_projection[tenant_field] in written_columns
            and Tenancy.get_scope(model) is not None
        ):
            raise QueryError(
                f"{method_name} writes {tenant_field!r} from the source rows under the active tenant scope - "
                "leave it out to write the active tenant, or call all_tenants() on the target"
            )
        for relation_name in meta.foreign_key_fields | meta.one_to_one_fields:
            relation = cast("RelationalField[Any]", meta.fields_map[relation_name])
            related_model = relation.related_model
            if (
                related_model._meta.tenant_field
                and meta.fields_db_projection[relation.source_fields[0]] in written_columns
                and Tenancy.get_scope(related_model) is not None
            ):
                raise QueryError(
                    f"{method_name} writes {relation_name!r} from the source rows under the active tenant scope of "
                    f"{related_model.__name__} - the rows it points at can't be checked; call all_tenants() on the "
                    "target"
                )
