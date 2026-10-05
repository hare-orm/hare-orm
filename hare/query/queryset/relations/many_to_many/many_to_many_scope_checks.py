from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import IntegrityError, QueryError
from hare.query.key_columns import KeyColumns
from hare.query.queryset.relations.many_to_many.many_to_many_members import ManyToManyMembers
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.builder.queries.query_builder import QueryBuilder
from hare.sql.builder.tables.table import Table
from hare.sql.identifiers import Identifiers
from hare.sql.terms.star import Star

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.sql.terms.criteria.criterion import Criterion


class ManyToManyScopeChecks:
    """The checks that a many-to-many write stays within the active tenant and the rows the related
    model's default scope shows, and the condition the related rows of a link must meet to count as
    linked."""

    @staticmethod
    async def check_tenant_scope(
        relation: ManyToManyRelation[Any], instances: tuple[TModel, ...] | None, connection: DatabaseClient
    ) -> None:
        """Verifies ``self.instance`` and every one of ``instances`` belong to the active tenant, on
        each side of the relation that has ``Meta.tenant_field``.

        Args:
            relation: The many-to-many relation of an instance.
            instances: The related instances added or removed - None for ``clear()``.
            connection: The connection the write runs on.
        """
        owning_tenant_field = type(relation.instance)._meta.tenant_field
        if owning_tenant_field:
            await ManyToManyScopeChecks.check_instance_tenant(relation.instance, owning_tenant_field, connection)
        if not instances:
            return
        related_tenant_field = type(instances[0])._meta.tenant_field
        if related_tenant_field:
            for instance in instances:
                await ManyToManyScopeChecks.check_instance_tenant(instance, related_tenant_field, connection)

    @staticmethod
    async def check_visible_scope(
        relation: ManyToManyRelation[Any], instances: tuple[TModel, ...], connection: DatabaseClient
    ) -> None:
        """Verifies, against the database, that ``self.instance`` isn't soft-deleted and that the
        related model's default scope shows every one of ``instances`` - an instance in hand may be
        stale or built by hand.

        Args:
            relation: The many-to-many relation of an instance.
            instances: The related instances being added.
            connection: Connection to read on.

        Raises:
            IntegrityError: ``self.instance`` is soft-deleted, or one of ``instances`` is hidden.
        """
        from hare.query.scopes.row_scopes import RowScopes

        owning_meta = type(relation.instance)._meta
        if owning_meta.soft_delete_field:
            owning_table = owning_meta.basetable
            soft_delete_column = owning_meta.fields_db_projection[owning_meta.soft_delete_field]
            query = (
                connection.query_class.from_(owning_table)
                .where(
                    KeyColumns.row_equality(
                        [owning_table[column] for column in KeyColumns.get_source_columns(owning_meta)],
                        KeyColumns.get_db_values(owning_meta, relation.instance, connection.dialect.types),
                    )
                    & owning_table[soft_delete_column].isnull()
                )
                .select(*KeyColumns.get_source_columns(owning_meta))
            )
            _, rows = await connection.execute(*query.get_parameterized_sql())
            if not rows:
                raise IntegrityError(
                    f"{type(relation.instance).__name__} instance (pk={relation.instance.pk!r}) is soft-deleted - "
                    "can't add a relation from it."
                )
        related_meta = relation.model._meta
        related_table = related_meta.basetable
        visible_criterion = RowScopes.of(relation.model).get_criterion(
            related_table, dialect=connection.dialect, connection=connection
        )
        if visible_criterion is None:
            return
        pk_columns = KeyColumns.get_source_columns(related_meta)
        instances_by_pk_values = {
            KeyColumns.get_db_values(related_meta, instance, connection.dialect.types): instance
            for instance in instances
        }
        query = (
            connection.query_class.from_(related_table)
            .where(
                connection.dialect.filter_operators.get_row_membership_criterion(
                    [related_table[column] for column in pk_columns],
                    list(instances_by_pk_values),
                    ManyToManyMembers.get_forward_key_fields(relation),
                )
                & visible_criterion
            )
            .select(*pk_columns)
        )
        _, rows = await connection.execute(*query.get_parameterized_sql())
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        visible_pk_values = {
            tuple(
                connection.dialect.types.get_db_value(pk_field, row[column], relation.model)
                for pk_field, column in zip(related_pk_fields, pk_columns, strict=True)
            )
            for row in rows
        }
        for pk_values, instance in instances_by_pk_values.items():
            if pk_values not in visible_pk_values:
                raise IntegrityError(
                    f"{type(instance).__name__} instance (pk={instance.pk!r}) is not shown by "
                    f"{type(instance).__name__}'s default scope (soft-deleted, or filtered out by its "
                    "Meta.manager) - can't add it to this relation."
                )

    @staticmethod
    async def check_instance_tenant(obj: Model, tenant_field: str, connection: DatabaseClient) -> None:
        # Local import: hare.models (Tenancy's own package) imports back from hare.fields.
        # relational at package-init time - importing it back at module level here would be
        # circular (same reasoning as QueryConditions.get_distinct()'s own local Manager import).
        from hare.models.tenancy.tenancy import Tenancy

        active_tenant = Tenancy.get_scope(type(obj))
        if active_tenant is None:
            raise QueryError(
                f"{type(obj).__name__} has Meta.tenant_field '{tenant_field}' set but no "
                "tenant is active - wrap this call in Tenancy.scope(...)."
            )
        if active_tenant is Tenancy.ALL:
            return
        # The tenant is read from the database by primary key - an obj built by hand can claim
        # any tenant.
        meta = type(obj)._meta
        table = meta.basetable
        tenant_db_column = meta.fields_db_projection[tenant_field]
        pk_columns = [table[column] for column in KeyColumns.get_source_columns(meta)]
        condition = KeyColumns.row_equality(pk_columns, KeyColumns.get_db_values(meta, obj, connection.dialect.types))
        query = connection.query_class.from_(table).where(condition).select(tenant_db_column)
        _, rows = await connection.execute(*query.get_parameterized_sql())
        stored_tenant = (
            connection.dialect.types.get_python_value(meta.fields_map[tenant_field], rows[0][tenant_db_column])
            if rows
            else None
        )
        if not rows or not Tenancy.allows(type(obj), active_tenant, stored_tenant):
            raise QueryError(
                f"{type(obj).__name__} instance (pk={obj.pk!r}) belongs to a tenant "
                f"other than the active one ({active_tenant!r}) according to the database."
            )

    @staticmethod
    def get_visible_target_criterion(
        relation: ManyToManyRelation[Any],
        through_table: Table,
        *,
        visibility: RowVisibility = RowVisibility.DEFAULT,
        dialect: Dialect,
        connection: DatabaseClient | None,
    ) -> Criterion | None:
        """Builds an ``EXISTS`` condition on ``through_table`` that holds only for rows linking to a
        related row the related model's default scope (soft-delete/tenant/``Meta.manager`` filters)
        shows, so ``clear()``/``set()`` leave links to other tenants' or hidden rows untouched.

        Args:
            relation: The many-to-many relation of an instance.
            through_table: The through table the condition is built against.
            visibility: Which rows of the related model its default scope shows.
            dialect: The dialect the query compiles for.
            connection: The connection the query runs on; None when compiling for no particular
                database.

        Returns:
            The condition, or ``None`` if the related model has no default scope.

        Raises:
            QueryError: If the related model has ``Meta.tenant_field`` set and no tenant is
                active.
        """
        from hare.query.scopes.row_scopes import RowScopes
        from hare.sql.terms.subqueries.exists_term import ExistsTerm

        related_meta = relation.model._meta
        target_table = related_meta.basetable.as_(
            Identifiers.get_within_limit(f"{related_meta.db_table}__visible_target")
        )
        visible_target_criterion = RowScopes.of(relation.model).get_criterion(
            target_table,
            visibility=visibility,
            dialect=dialect,
            connection=connection,
        )
        if visible_target_criterion is None:
            return None
        link_criterion = KeyColumns.row_equality(
            [through_table[column] for column in relation.field.forward_keys],
            [target_table[column] for column in KeyColumns.get_source_columns(related_meta)],
        )
        return ExistsTerm(
            QueryBuilder().from_(target_table).select(Star()).where(link_criterion & visible_target_criterion)
        )
