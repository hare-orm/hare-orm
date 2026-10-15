from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.core.connections.connections import Connections
from hare.exceptions import (
    QueryError,
)
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.query.queryset.relations.many_to_many.many_to_many_members import ManyToManyMembers
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.statements.write.create_or_update import CreateOrUpdate

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model

TModel = TypeVar("TModel", bound="Model")


class ManyToManyRelation(RelatedQuerySet[TModel]):
    """
    The relation of a ``ManyToManyField``.
    """

    __slots__ = ("field",)

    def __init__(
        self,
        obj: Model,
        many_to_many_field: ManyToManyFieldInstance[TModel],
        relation_rows: RelationRows | None = None,
    ) -> None:
        super().__init__(
            many_to_many_field.related_model, (many_to_many_field.related_name,), obj, ("pk",), relation_rows
        )
        self.field = many_to_many_field

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        state["field"] = self.field
        return state

    async def add(
        self,
        *objs: Any,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None:
        """Adds related instances, or their primary key values (a tuple for a composite key), to the
        relation. A pair already linked is left as is; one whose through row was soft-deleted gets a
        new row - or, where the through model declares the pair unique, that row restored, with
        ``through_defaults`` written into it.

        Args:
            objs: Related instances or their primary key values.
            through_defaults: Values for the extra fields of a ``through=Model`` model, by field
                name - applied to the rows this call inserts.
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: An object isn't saved; ``through_defaults`` doesn't fit the through model;
                or a tenant-scoped side has no active tenant or belongs to another one.
            ValidationError: One of ``objs`` is not an instance of the related model.
            IntegrityError: ``self.instance`` is soft-deleted, or the related model's default scope
                doesn't show one of ``objs``.
        """
        if not objs:
            return
        through_model = self.field.through_model_class
        if through_model is not None and through_model._meta.change_capture_needs is not None:
            # The links' captured changes are written with them.
            connection = Connections.get_client(using) or ThroughRows.through_table_connection(self, for_write=True)
            if ChangeCapturing.needs_transaction(connection):
                async with connection._in_transaction() as transaction_connection:
                    await self.add(*objs, through_defaults=through_defaults, using=transaction_connection)
                return
        related_instances = await ThroughRows.add_links(self, objs, through_defaults, using)
        if ChangeEvents.is_observed():
            await ThroughRows.report_links(
                self,
                Connections.get_client(using) or ThroughRows.through_table_connection(self, for_write=True),
                related_instances,
                RowOperation.INSERT,
            )

    async def clear(self, using: str | DatabaseClient | None = None, *, all_tenants: bool = False) -> None:
        """Clears every link of the relation. The rows of a ``through=Model`` model with
        ``Meta.soft_delete_field`` are soft-deleted instead.

        Args:
            using: Specific DB connection to use instead of default bound.
            all_tenants: Clear the links to rows of every tenant - by default only those to the
                active tenant's rows.
        """
        await ThroughRows.remove_or_clear(
            self, using=using, check_tenant_scope=not all_tenants, visibility=RowVisibility(all_tenants=all_tenants)
        )

    async def remove(self, *objs: Any, using: str | DatabaseClient | None = None) -> None:
        """Removes related instances, or their primary key values, from the relation - every through
        row of each pair. A link named by a primary key value is removed only when the related
        model's default scope shows its row. The rows of a soft-delete through model are
        soft-deleted.

        Raises:
            QueryError: No instances were given, or ``self.instance`` or one of ``objs`` is not
                saved.
            ValidationError: One of ``objs`` is not an instance of the related model.
        """
        if not objs:
            raise QueryError("remove() called on no instances")
        related_instances = tuple(member for member in objs if ManyToManyMembers.is_model_instance(member))
        if len(related_instances) == len(objs):
            await ThroughRows.remove_or_clear(self, related_instances, using)
            return
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(self, for_write=True)
        forward_pk_values = [
            ManyToManyMembers.get_member_pk_db_values(self, member, connection.dialect.types)
            for member in objs
            if not ManyToManyMembers.is_model_instance(member)
        ]
        async with connection._in_transaction() as transaction_connection:
            if related_instances:
                await ThroughRows.remove_or_clear(self, related_instances, transaction_connection)
            await ThroughRows.remove_or_clear(self, using=transaction_connection, forward_pk_values=forward_pk_values)

    async def create(
        self,
        *,
        using: str | DatabaseClient | None = None,
        through_defaults: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> TModel:
        """
        Creates an object of the related model and adds it to the relation, in one transaction.

        Args:
            using: Specific DB connection to use instead of default bound.
            through_defaults: Passed on to ``add()``.
            kwargs: Model parameters for the new object.

        Returns:
            The created object.

        Raises:
            QueryError: ``self.instance`` is not saved.
        """
        ManyToManyMembers.validate_owner_saved(self)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(self, for_write=True)
        async with connection._in_transaction() as transaction_connection:
            created_instance = await self._get_model_queryset(
                ManyToManyMembers.get_connection_of(self.model, transaction_connection)
            ).create(**kwargs)
            await self.add(created_instance, through_defaults=through_defaults, using=transaction_connection)
        return created_instance

    async def set(
        self,
        *objs: Any,
        through_defaults: dict[str, Any] | None = None,
        clear: bool = False,
        using: str | DatabaseClient | None = None,
    ) -> None:
        """Replaces the relation's members with ``objs``, in one transaction: removes the missing
        members and adds the new ones; the through rows of members that stay are untouched.

        Args:
            objs: Related instances or their primary key values - as arguments, or one iterable
                or queryset.
            through_defaults: Passed on to ``add()`` for the added members.
            clear: Clear the whole relation first and add every one of ``objs`` afresh.
            using: Specific DB connection to use instead of default bound.

        Raises:
            QueryError: ``self.instance`` or one of ``objs`` is not saved.
            ValidationError: One of ``objs`` is not an instance of the related model.
        """
        members = await self._get_set_members(objs)
        ManyToManyMembers.validate_owner_saved(self)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(self, for_write=True)
        members_by_pk_values = {
            ManyToManyMembers.get_member_pk_db_values(self, member, connection.dialect.types): member
            for member in members
        }
        async with connection._in_transaction() as transaction_connection:
            if clear or not members:
                await self.clear(using=transaction_connection)
                if members:
                    await self.add(*members, through_defaults=through_defaults, using=transaction_connection)
                return
            linked_pk_values = await ThroughRows.get_linked_pk_values(self, transaction_connection)
            if unlinked_pk_values := [
                pk_values for pk_values in linked_pk_values if pk_values not in members_by_pk_values
            ]:
                await ThroughRows.remove_or_clear(
                    self, using=transaction_connection, forward_pk_values=unlinked_pk_values
                )
            if new_members := [
                member for pk_values, member in members_by_pk_values.items() if pk_values not in linked_pk_values
            ]:
                await self.add(*new_members, through_defaults=through_defaults, using=transaction_connection)

    async def get_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        *,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]:
        """
        Fetches the member matching ``kwargs``, else creates an object of the related model and
        adds it to the relation, in one transaction - like Django.

        Args:
            defaults: Values for a created object, on top of ``kwargs``' exact values.
            through_defaults: Passed on to ``add()``.
            using: Specific DB connection to use instead of default bound.
            kwargs: Query parameters.

        Raises:
            QueryError: ``self.instance`` is not saved.
            MultipleObjectsReturned: More than one member matches ``kwargs``.
            QueryError: ``defaults`` conflicts with ``kwargs``.
        """
        ManyToManyMembers.validate_owner_saved(self)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(self, for_write=True)
        async with connection._in_transaction() as transaction_connection:
            related_connection = ManyToManyMembers.get_connection_of(self.model, transaction_connection)
            if (
                member := await self.filter(**kwargs).using(related_connection).get(does_not_exist_exception=None)
            ) is not None:
                return member, False
            return (
                await self.create(
                    using=transaction_connection,
                    through_defaults=through_defaults,
                    **CreateOrUpdate.get_create_values(self.model, defaults or {}, kwargs),
                ),
                True,
            )

    async def update_or_create(
        self,
        defaults: dict[str, Any] | None = None,
        create_defaults: dict[str, Any] | None = None,
        *,
        through_defaults: dict[str, Any] | None = None,
        using: str | DatabaseClient | None = None,
        **kwargs: Any,
    ) -> tuple[TModel, bool]:
        """
        Updates the member matching ``kwargs`` with ``defaults``, else creates an object of the
        related model and adds it to the relation, in one transaction - like Django.

        Args:
            defaults: Values to update the member with, or for a created object on top of
                ``kwargs``' exact values when ``create_defaults`` isn't given.
            through_defaults: Passed on to ``add()``.
            using: Specific DB connection to use instead of default bound.
            create_defaults: Values for a created object instead of ``defaults``.
            kwargs: Query parameters.

        Raises:
            QueryError: ``self.instance`` is not saved.
            MultipleObjectsReturned: More than one member matches ``kwargs``.
            QueryError: ``defaults`` conflicts with ``kwargs``.
        """
        ManyToManyMembers.validate_owner_saved(self)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(self, for_write=True)
        async with connection._in_transaction() as transaction_connection:
            related_connection = ManyToManyMembers.get_connection_of(self.model, transaction_connection)
            if (
                member := await self.filter(**kwargs).using(related_connection).get(does_not_exist_exception=None)
            ) is not None:
                updated_member, _ = await self._get_model_queryset(related_connection).update_or_create(
                    defaults, pk=member.pk
                )
                self._invalidate_local_cache()
                return updated_member, False
            creation_values = defaults if create_defaults is None else create_defaults
            return (
                await self.create(
                    using=transaction_connection,
                    through_defaults=through_defaults,
                    **CreateOrUpdate.get_create_values(self.model, creation_values or {}, kwargs),
                ),
                True,
            )


# The Python type of a many-to-many field's value - set here: this module imports the field
# modules, not the other way round.
ManyToManyFieldInstance.field_type = ManyToManyRelation
