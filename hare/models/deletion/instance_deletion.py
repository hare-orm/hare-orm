from __future__ import annotations

from typing import TYPE_CHECKING

from hare.exceptions import CascadeDepthLimitError, IncompleteInstanceError, IntegrityError, QueryError
from hare.models.deletion.cascade.cascade_deletion import CascadeDeletion
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.protect_constraint_deferral import ProtectConstraintDeferral
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.deletion.soft_deletion import SoftDeletion
from hare.models.instances.instance_saving import InstanceSaving
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.instance_writer import InstanceWriter

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


class InstanceDeletion:
    """The delete of an instance's row: the checks that it may be deleted within the active tenant, its
    on_delete cascade, and the row deleted for good."""

    @staticmethod
    def check_deletable(obj: Model) -> None:
        """Checks the obj names one saved row.

        Args:
            obj: The model obj.

        Raises:
            QueryError: The obj was never saved, or its primary key isn't set (after
                ``bulk_create()`` without ``returning=True``).
            IncompleteInstanceError: The obj was loaded without its primary key.
        """
        if not obj._saved_in_db:
            raise QueryError("Can't delete unpersisted record")
        if not all(hasattr(obj, pk_name) for pk_name in obj._meta.primary_key_attribute_names):
            raise IncompleteInstanceError(
                f"{obj.__class__.__name__} is a partial model without primary key fetched. Can't delete it."
            )
        if any(getattr(obj, pk_name) is None for pk_name in obj._meta.primary_key_attribute_names):
            raise QueryError(
                f"Can't delete a {obj.__class__.__name__} whose primary key isn't set - bulk_create() leaves a "
                "generated primary key unset unless returning=True"
            )

    @staticmethod
    def check_active_tenant_scope_for_write(obj: Model, operation: str) -> None:
        """Raises if this obj's ``Meta.tenant_field`` value conflicts with the model's tenant
        scope. Never fills an unset tenant field - the row exists already.

        Args:
            obj: The model obj.
            operation: The calling method, for the message.

        Raises:
            QueryError: The scope doesn't allow the obj's tenant, or the tenant is unset with
                no scope to check against.
        """
        tenant_field = obj._meta.tenant_field
        if not tenant_field:
            return
        active_tenant = Tenancy.get_scope(obj.__class__)
        current_value = getattr(obj, tenant_field, None)
        if current_value is None:
            if active_tenant is None:
                raise QueryError(
                    f"{obj.__class__.__name__} has Meta.tenant_field '{tenant_field}' set but it's "
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
    async def delete_with_cascade(
        obj: Model, connection: DatabaseClient, *, apply_active_tenant_scope_guard: bool
    ) -> None:
        """Everything ``delete()`` does after its active-tenant-scope check.

        Args:
            obj: The model obj.
            connection: Connection the delete and its cascade run through.
            apply_active_tenant_scope_guard: ``False`` for a row a cascade found through a real FK
                match - it may belong to another tenant than the active scope (see
                ``SoftDeletion.persist_soft_delete()``).

        Raises:
            Same as ``delete()``, except the tenant-scope ``QueryError``.
        """
        InstanceDeletion.check_deletable(obj)
        soft_delete_field = obj._meta.soft_delete_field
        if await SoftDeletion.is_already_soft_deleted(obj, connection):
            # Already soft-deleted - nothing to do; its deletion time stays.
            return
        if soft_delete_field:
            InstanceSaving.check_optimistic_lock_field_loaded(obj, "delete")
            if DeletionGraph.is_unreferenced(type(obj)) and await SoftDeletion.write_soft_delete_field(
                obj,
                connection,
                CascadeDeletion.get_deleted_at(),
                "delete",
                apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                only_live_row=True,
            ):
                # Nothing cascades from or protects the row - one UPDATE of it while it's live. A
                # row it didn't match (soft-deleted meanwhile, gone, a stale version) is settled by
                # the full delete below.
                return
        await DeletionCollector.check_protected(type(obj), [obj.pk], connection)
        if not obj._meta.soft_delete_field:
            # The database's own cascade is invisible to Python - a PROTECT further down it is
            # looked for up front.
            await DeletionCollector.check_protected_transitively(type(obj), [obj.pk], connection)
        if obj._meta.soft_delete_field:
            # One transaction for the cascade and this obj's own UPDATE - they succeed or fail
            # together.
            deleted_at = CascadeDeletion.get_deleted_at()
            async with connection._in_transaction() as transaction_connection:
                soft_delete_values = await RelatedRows.lock_soft_delete_values(
                    type(obj), [obj.pk], transaction_connection
                )
                if (current_soft_delete_value := soft_delete_values.get(obj.pk)) is not None:
                    # Soft-deleted since this obj was read (a stale copy, or a concurrent
                    # delete this one waited for) - a no-op like any already-deleted row.
                    SoftDeletion.adopt_soft_delete_value(obj, transaction_connection, current_soft_delete_value)
                    return
                await CascadeDeletion.run_below(
                    obj,
                    transaction_connection,
                    only_unconstrained=False,
                    persist_as_hard_delete=False,
                    deleted_at=deleted_at,
                )
                await SoftDeletion.persist_soft_delete(
                    obj,
                    transaction_connection,
                    apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                    deleted_at=deleted_at,
                )
        else:
            await InstanceDeletion.delete_row_permanently(
                obj, connection, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard
            )

    @staticmethod
    async def delete_row_permanently(
        obj: Model, connection: DatabaseClient, *, apply_active_tenant_scope_guard: bool
    ) -> None:
        """Issues the real ``DELETE`` shared by ``delete()`` (no soft delete) and ``hard_delete()``,
        cascading in Python whatever the database can't cascade on its own.

        Args:
            obj: The model obj.
            connection: Connection the delete and its cascade run through.
            apply_active_tenant_scope_guard: See ``delete_with_cascade()``.
        """
        if DeletionGraph.needs_python_cascade(type(obj)):
            # A relation the database doesn't enforce, or rows of a captured model it reaches: its
            # on_delete is carried out in Python, in one transaction with the DELETE.
            async with (
                connection._in_transaction() as transaction_connection,
                ProtectConstraintDeferral.defer(type(obj), transaction_connection),
            ):
                root_reached_again = await CascadeDeletion.run_below(
                    obj, transaction_connection, only_unconstrained=True, persist_as_hard_delete=True
                )
                await InstanceDeletion.persist_hard_delete(
                    obj,
                    transaction_connection,
                    apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                    allow_already_deleted=root_reached_again,
                )
        elif connection.features.cascade_depth_limit is not None and (
            DeletionGraph.has_self_cascading_constrained_relations(type(obj))
        ):
            # Where the database's cascade stops at a recursion depth, a deep cascade cycle fails
            # the DELETE half-done - tried in a transaction, and on CascadeDepthLimitError rolled
            # back and carried out in Python, deepest rows first.
            try:
                async with connection._in_transaction() as transaction_connection:
                    await InstanceDeletion.persist_hard_delete(
                        obj,
                        transaction_connection,
                        apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                    )
            except CascadeDepthLimitError:
                async with (
                    connection._in_transaction() as transaction_connection,
                    ProtectConstraintDeferral.defer(type(obj), transaction_connection),
                ):
                    root_reached_again = await CascadeDeletion.run_below(
                        obj,
                        transaction_connection,
                        only_unconstrained=False,
                        persist_as_hard_delete=True,
                        bottom_up_persist=True,
                    )
                    await InstanceDeletion.persist_hard_delete(
                        obj,
                        transaction_connection,
                        apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                        allow_already_deleted=root_reached_again,
                    )
        elif ProtectConstraintDeferral.is_needed(type(obj), connection):
            # A protector that this same cascade removes as well must not fail the DELETE - see
            # ProtectConstraintDeferral.
            async with (
                connection._in_transaction() as transaction_connection,
                ProtectConstraintDeferral.defer(type(obj), transaction_connection),
            ):
                await InstanceDeletion.persist_hard_delete(
                    obj, transaction_connection, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard
                )
        else:
            await InstanceDeletion.persist_hard_delete(
                obj, connection, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard
            )

    @staticmethod
    async def persist_hard_delete(
        obj: Model,
        connection: DatabaseClient,
        apply_active_tenant_scope_guard: bool = True,
        allow_already_deleted: bool = False,
    ) -> None:
        """Issues this already-cascaded obj's own ``DELETE``.

        Args:
            obj: The model obj.
            connection: The connection the delete runs on.
            apply_active_tenant_scope_guard: See ``SoftDeletion.persist_soft_delete()``.
            allow_already_deleted: The same cascade may already have removed the row - a DELETE
                matching nothing is fine.

        Raises:
            IntegrityError: The row no longer exists and ``allow_already_deleted`` is not set.
        """
        writer = InstanceWriter(obj.__class__, connection)
        rows = await writer.execute_delete(obj, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard)
        if rows == 0 and not allow_already_deleted:
            raise IntegrityError(f"Can't delete object that doesn't exist. PK: {obj.pk}")
