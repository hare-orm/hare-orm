from __future__ import annotations

from itertools import chain
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import (
    QueryError,
)
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.write.write_steps import WriteSteps
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.execution.dependent_condition import DependentCondition
from hare.query.key_columns import KeyColumns
from hare.query.key_comparison import KeyComparison
from hare.query.lookup_info.lookup_paths import LookupPaths
from hare.query.queryset.relations.many_to_many.linked_keys import LinkedKeys
from hare.query.queryset.relations.many_to_many.linked_rows_dependency import LinkedRowsDependency
from hare.query.queryset.relations.many_to_many.many_to_many_members import ManyToManyMembers
from hare.query.queryset.relations.many_to_many.through_defaults import ThroughDefaults
from hare.query.queryset.relations.many_to_many.through_row_inserting import ThroughRowInserting
from hare.query.queryset.relations.many_to_many.through_row_unlinking import ThroughRowUnlinking
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.statements.write.create_or_update import CreateOrUpdate

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many.through_values import ThroughValues

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
            many_to_many_field.related_model,
            (many_to_many_field.related_name,),
            obj,
            (PRIMARY_KEY_ALIAS,),
            relation_rows,
        )
        self.field = many_to_many_field

    def _bind(self) -> None:
        """Choose the query declaration when binding, leaving fetched-row access independent of routing.

        Custom manager querysets are preserved in both cases. Cross-connection reads declare
        their through-table keys as an execution dependency of the ordinary queryset.
        """
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(self._context):
            reads_linked_rows = (
                any(
                    isinstance(dependency, LinkedRowsDependency) and dependency.relation is self
                    for dependency in self._execution_dependencies
                )
                if self._is_bound
                else not self.field._generated and LookupPaths.crosses_connections(self.field)
            )
            if reads_linked_rows:
                queryset = self._binding.manager.create_queryset(binding=self._binding, context=self._context)
                dependency = LinkedRowsDependency(self)
                queryset._execution_dependencies = (*queryset._execution_dependencies, dependency)
                queryset._q_objects.append(DependentCondition(dependency))
                queryset._call_signature = None
                self._copy_bound_settings(queryset, None)
                return
            super()._bind()

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
            using: The connection - an alias or a client - to run on instead of the model's routed one.

        Raises:
            QueryError: An object isn't saved; ``through_defaults`` doesn't fit the through model;
                or a tenant-scoped side has no active tenant or belongs to another one.
            ValidationError: One of ``objs`` is not an instance of the related model.
            IntegrityError: ``self.instance`` is soft-deleted, or the related model's default scope
                doesn't show one of ``objs``.
        """
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(self._context):
            if not objs:
                return
            connection = ThroughRows.through_table_connection(self, for_write=True, using=using)
            prepared = ThroughDefaults.prepare(self, through_defaults, connection.dialect.types)
            await self._add_prepared(objs, prepared, connection)

    async def _add_prepared(self, objs: tuple[Any, ...], prepared: ThroughValues, connection: DatabaseClient) -> None:
        """Write and report links using assignments bound by the enclosing operation."""
        through_binding = self.field.get_through_binding()
        if through_binding is not None and through_binding.change_capture_needs is not None:
            # The links' captured changes are written with them.
            async with ChangeCapturing.transaction(connection) as transaction_connection:
                await WriteSteps.get_whole_run(
                    transaction_connection,
                    self._add_and_report(objs, prepared, transaction_connection),
                )
            return
        await WriteSteps.get_whole_run(connection, self._add_and_report(objs, prepared, connection))

    async def _add_and_report(
        self,
        objs: tuple[Any, ...],
        prepared: ThroughValues,
        connection: DatabaseClient,
    ) -> None:
        """Adds the related instances and reports the links (``ChangeEvents``).

        Args:
            objs: See ``add()``.
            prepared: Explicit through assignments of the enclosing operation.
            connection: The connection the through rows are written on.
        """
        related_instances, inserts_through_rows = await ThroughRowInserting.add_links(self, objs, prepared, connection)
        # A pair linked already adds no link - nothing is reported; None names no related row.
        if related_instances != [] and ChangeEvents.is_observed():
            await ThroughRows.report_links(
                self,
                connection,
                related_instances,
                RowOperation.INSERT,
                reports_through_rows=inserts_through_rows,
            )

    async def clear(self, using: str | DatabaseClient | None = None, *, all_tenants: bool = False) -> None:
        """Clears every link of the relation. The rows of a ``through=Model`` model with
        ``Meta.soft_delete_field`` are soft-deleted instead.

        Args:
            using: The connection - an alias or a client - to run on instead of the model's routed one.
            all_tenants: Clear the links to rows of every tenant - by default only those to the
                active tenant's rows.
        """
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(self._context):
            await ThroughRowUnlinking.remove_or_clear(
                self,
                using=using,
                check_tenant_scope=not all_tenants,
                visibility=RowVisibility(all_tenants=all_tenants),
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
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(self._context):
            if not objs:
                raise QueryError("remove() called on no instances")
            related_instances = tuple(member for member in objs if ManyToManyMembers.is_model_instance(member))
            if len(related_instances) == len(objs):
                await ThroughRowUnlinking.remove_or_clear(self, related_instances, using)
                return
            connection = ThroughRows.through_table_connection(self, for_write=True, using=using)
            writers = KeyColumns.get_instance_writers(self._binding, connection.dialect.types)
            forward_pk_values = [
                ManyToManyMembers.get_member_pk_db_values(self, member, writers=writers)
                for member in objs
                if not ManyToManyMembers.is_model_instance(member)
            ]
            await WriteSteps.get_whole_run(
                connection, self._remove_members(related_instances, forward_pk_values, connection)
            )

    async def _remove_members(
        self,
        related_instances: tuple[Any, ...],
        forward_pk_values: list[tuple[Any, ...]],
        connection: DatabaseClient,
    ) -> None:
        """Removes related instances and members named by their primary key values, in one transaction.

        Args:
            related_instances: The related instances.
            forward_pk_values: The DB-ready primary key values of the members named by them.
            connection: The connection of the through table.
        """
        async with connection._in_transaction() as transaction_connection:
            if related_instances:
                await ThroughRowUnlinking.remove_or_clear(self, related_instances, transaction_connection)
            await ThroughRowUnlinking.remove_or_clear(
                self, using=transaction_connection, forward_pk_values=forward_pk_values
            )

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
            using: The connection - an alias or a client - to run on instead of the model's routed one.
            through_defaults: Passed on to ``add()``.
            kwargs: Model parameters for the new object.

        Returns:
            The created object.

        Raises:
            QueryError: ``self.instance`` is not saved.
        """
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(self._context):
            ManyToManyMembers.validate_owner_saved(self)
            connection = ThroughRows.through_table_connection(self, for_write=True, using=using)
            # Checked before the object is written - a database without transactions keeps what a failure
            # leaves.
            prepared = ThroughDefaults.prepare(self, through_defaults, connection.dialect.types)
            ThroughDefaults.get_default_fields(self, prepared.given_values, collect_defaults=False)
            async with connection._in_transaction() as transaction_connection:
                created_instance = await self._get_model_queryset(
                    ManyToManyMembers.get_connection_of(self, self.model, transaction_connection)
                ).create(**kwargs)
                await self._add_prepared((created_instance,), prepared, transaction_connection)
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
            using: The connection - an alias or a client - to run on instead of the model's routed one.

        Raises:
            QueryError: ``self.instance`` or one of ``objs`` is not saved.
            ValidationError: One of ``objs`` is not an instance of the related model.
        """
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(self._context):
            members = await ManyToManyMembers.get_set_members(self, objs)
            ManyToManyMembers.validate_owner_saved(self)
            connection = ThroughRows.through_table_connection(self, for_write=True, using=using)
            writers = KeyColumns.get_instance_writers(self._binding, connection.dialect.types)
            members_by_pk_values = {
                ManyToManyMembers.get_member_pk_db_values(self, member, writers=writers): member for member in members
            }
            # On a database without transactions the removals and additions are one step a cancel can't split.
            await WriteSteps.get_whole_run(
                connection, self._set_members(members_by_pk_values, through_defaults, clear, connection)
            )

    async def _set_members(
        self,
        members_by_pk_values: dict[tuple[Any, ...], Any],
        through_defaults: dict[str, Any] | None,
        clear: bool,
        connection: DatabaseClient,
    ) -> None:
        """Replaces the relation's members, in one transaction - see ``set()``.

        Args:
            members_by_pk_values: The members by their DB-ready primary key values.
            through_defaults: Explicit through assignments to prepare before the first write.
            clear: Clear the whole relation first and add every member afresh.
            connection: The connection of the through table.
        """
        async with connection._in_transaction() as transaction_connection:
            if not members_by_pk_values:
                ThroughDefaults.prepare(self, through_defaults, connection.dialect.types, fills_tenant=False)
                await self.clear(using=transaction_connection)
                return
            linked_pk_values = await LinkedKeys.get_linked_pk_values(self, transaction_connection)
            read_key = await KeyComparison.get_key_reader(
                transaction_connection, self._binding.pk_fields, chain(members_by_pk_values, linked_pk_values)
            )
            members_by_key = members_by_pk_values
            linked_keys = linked_pk_values
            if read_key is not KeyComparison.unchanged:
                members_by_key = {read_key(values): member for values, member in members_by_pk_values.items()}
                linked_keys = frozenset(map(read_key, linked_pk_values))
            new_members = [member for key, member in members_by_key.items() if key not in linked_keys]
            prepared = ThroughDefaults.prepare(
                self, through_defaults, connection.dialect.types, fills_tenant=bool(new_members) or clear
            )
            # The new members are added before a link is removed: a member that can't be added - a key of
            # no row - leaves the relation as it was where no transaction undoes the removal.
            if new_members:
                await self._add_prepared(tuple(new_members), prepared, transaction_connection)
            # clear=True adds every member afresh - the ones linked already are removed and added again.
            if removed_pk_values := [
                values for values in linked_pk_values if clear or read_key(values) not in members_by_key
            ]:
                await ThroughRowUnlinking.remove_or_clear(
                    self, using=transaction_connection, forward_pk_values=removed_pk_values
                )
            if clear and (kept_members := [member for key, member in members_by_key.items() if key in linked_keys]):
                await self._add_prepared(tuple(kept_members), prepared, transaction_connection)

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
            using: The connection - an alias or a client - to run on instead of the model's routed one.
            kwargs: Query parameters.

        Raises:
            QueryError: ``self.instance`` is not saved.
            MultipleObjectsReturned: More than one member matches ``kwargs``.
            QueryError: ``defaults`` conflicts with ``kwargs``.
        """
        return await self._get_or_create_member(
            None, defaults, through_defaults, using, kwargs, updates_existing=False
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
            using: The connection - an alias or a client - to run on instead of the model's routed one.
            create_defaults: Values for a created object instead of ``defaults``.
            kwargs: Query parameters.

        Raises:
            QueryError: ``self.instance`` is not saved.
            MultipleObjectsReturned: More than one member matches ``kwargs``.
            QueryError: ``defaults`` conflicts with ``kwargs``.
        """
        return await self._get_or_create_member(
            defaults,
            defaults if create_defaults is None else create_defaults,
            through_defaults,
            using,
            kwargs,
            updates_existing=True,
        )

    async def _get_or_create_member(
        self,
        defaults: dict[str, Any] | None,
        creation_defaults: dict[str, Any] | None,
        through_defaults: dict[str, Any] | None,
        using: str | DatabaseClient | None,
        filters: dict[str, Any],
        *,
        updates_existing: bool,
    ) -> tuple[TModel, bool]:
        """Find a linked member or create and link one in the through transaction."""
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(self._context):
            ManyToManyMembers.validate_owner_saved(self)
            connection = ThroughRows.through_table_connection(self, for_write=True, using=using)
            async with connection._in_transaction() as transaction_connection:
                related_connection = ManyToManyMembers.get_connection_of(self, self.model, transaction_connection)
                member = await self.filter(**filters).using(related_connection).get(does_not_exist_exception=None)
                if member is not None:
                    if updates_existing:
                        member, _ = await self._get_model_queryset(related_connection).update_or_create(
                            defaults, pk=member.pk
                        )
                        self._invalidate_local_cache()
                    return member, False
                obj = await self.create(
                    using=transaction_connection,
                    through_defaults=through_defaults,
                    **CreateOrUpdate.get_create_values(self._binding, creation_defaults or {}, filters),
                )
                return obj, True


# The Python type of a many-to-many field's value - set here: this module imports the field
# modules, not the other way round.
ManyToManyFieldInstance.field_type = ManyToManyRelation
