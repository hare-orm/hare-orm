from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any, cast

from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.constants import (
    CASCADE_LOOKUP_BIND_PARAMS_HEADROOM,
    CASCADE_LOOKUP_MAX_COMPOSITE_ROWS,
    DELETION_TREE_CTE_NAME,
)
from hare.query.composite import KeyColumns
from hare.query.enums import Connector
from hare.query.expressions import Q, RawSQL
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.queries.tables.table import Table
from hare.sql.terms.criteria.criterion import Criterion

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet


class RelatedRows:
    """Reading the rows a delete touches: the rows pointing at the deleted ones through a relation,
    of every tenant and soft-deleted ones included, on the connection the delete runs on - in
    batches within the backend's bind-parameter ceiling."""

    @staticmethod
    def get_connection_for(model: type[Model], db: DatabaseClient | None) -> DatabaseClient | None:
        """``db`` when ``model`` lives on its connection - the query then stays inside the delete's
        transaction - else None: a model on another connection uses its own.
        """
        if db is None:
            return None
        if db.connection_name == model.get_connection(for_write=True).connection_name:
            return db
        return None

    @staticmethod
    async def get_rows_pointing_at(
        backward_field: BackwardFKRelation[Any] | BackwardOneToOneRelation[Any],
        target_values: list[Any],
        db: DatabaseClient | None,
        *,
        primary_keys_only: bool = False,
    ) -> list[Any]:
        """Every row on the far side of ``backward_field`` (soft-deleted ones included) whose
        shadow FK column(s) hold any of ``target_values`` - split into several lookups when the
        list would otherwise exceed the backend's bind-parameter ceiling.

        Args:
            backward_field: The backward FK/O2O relation of the rows' target model.
            target_values: Scalars for a single-column FK, per-row tuples for a composite one
                (see ``_target_values_for_pks``).
            db: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
            primary_keys_only: Return just each row's primary key (a tuple for a composite pk)
                instead of full instances.
        """
        pk_attr_names = backward_field.related_model._meta.pk_attr_names
        rows: list[Any] = []
        for query in RelatedRows.get_pointing_querysets(backward_field, target_values, db):
            if primary_keys_only:
                rows.extend(await query.values_list(*pk_attr_names, flat=len(pk_attr_names) == 1))
            else:
                rows.extend(await query)
        return rows

    @staticmethod
    def get_pointing_querysets(
        backward_field: BackwardFKRelation[Any] | BackwardOneToOneRelation[Any],
        target_values: list[Any],
        db: DatabaseClient | None,
    ) -> list[QuerySet[Any]]:
        """Querysets over the rows on the far side of ``backward_field`` (every tenant, soft-deleted
        ones included, bypassing ``Meta.manager``) whose shadow FK column(s) hold any of
        ``target_values`` - one per batch that fits the backend's bind-parameter ceiling.

        Args:
            backward_field: The backward FK/O2O relation of the rows' target model.
            target_values: Scalars for a single-column FK, per-row tuples for a composite one
                (see ``_target_values_for_pks``); a value with a ``None`` component matches nothing.
            db: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.

        Returns:
            The querysets - none when no value can match.
        """
        target_values = [value for value in target_values if not RelatedRows.has_null_component(value)]
        related_model = backward_field.related_model
        related_db = RelatedRows.get_connection_for(related_model, db)
        column_count = len(backward_field.relation_fields)
        querysets: list[QuerySet[Any]] = []
        lookup_db = related_db or related_model.get_connection(for_write=False)
        for batch in RelatedRows.split_into_batches(target_values, lookup_db, column_count):
            base_query = RelatedRows.get_base_queryset(related_model)
            if column_count == 1:
                query = base_query.filter(**{f"{backward_field.relation_field}__in": batch}).using(related_db)
            else:
                # A composite key has no single column for __in: each row's equalities are ANDed and
                # the rows ORed. A batch is never empty - an empty OR would be dropped.
                groups = [Q(**dict(zip(backward_field.relation_fields, row, strict=True))) for row in batch]
                query = base_query.filter(Q.with_connector(Connector.OR, *groups)).using(related_db)
            querysets.append(RelatedRows.include_soft_deleted(query))
        return querysets

    @staticmethod
    async def get_tree_rows(
        backward_field: BackwardFKRelation[Any] | BackwardOneToOneRelation[Any],
        root_pks: list[Any],
        db: DatabaseClient | None,
        value_names: list[str],
    ) -> list[tuple[Any, ...]] | None:
        """Every row below the root rows through ``backward_field``, a relation of a model onto itself
        by its single-column primary key, at any depth (every tenant, soft-deleted ones included) -
        one recursive query.

        Args:
            backward_field: The backward relation.
            root_pks: Primary keys of the root rows.
            db: The connection the delete runs on.
            value_names: The fields read of each row.

        Returns:
            The values of each row; None when the roots don't fit one query, or the model's default
            scope would hide some of the rows.
        """
        model = backward_field.related_model
        model_db = RelatedRows.get_connection_for(model, db)
        lookup_db = model_db or model.get_connection(for_write=False)
        root_pks = [pk for pk in root_pks if pk is not None]
        if not root_pks or len(RelatedRows.split_into_batches(root_pks, lookup_db, 1)) > 1:
            return None
        queryset = RelatedRows.include_soft_deleted(RelatedRows.get_base_queryset(model)).using(model_db)
        # Imported here: the module imports this one.
        from hare.query.scopes.row_scopes import RowScopes

        if RowScopes.of(model).get_condition(queryset._visibility) is not None:
            return None
        meta = model._meta
        table = meta.basetable
        tree = Table(DELETION_TREE_CTE_NAME)
        pk_column = meta.db_pk_column
        parent_column = meta.fields_db_projection[backward_field.relation_field]
        query_class = lookup_db.query_class
        roots = query_class.from_(table).select(table[pk_column]).where(table[parent_column].isin(root_pks))
        below = (
            query_class.from_(table).join(tree).on(table[parent_column] == tree[pk_column]).select(table[pk_column])
        )
        # UNION, not UNION ALL: a cycle ends once it brings no row it hasn't listed.
        union = roots + below
        union.base_query.wrap_set_operation_queries = False
        # The dialect's own builder writes the subquery's text.
        tree_pks = RawSQL(query_class.from_(tree).select(tree[pk_column]).get_sql())
        return list(
            await queryset.with_cte(DELETION_TREE_CTE_NAME, union).filter(pk__in=tree_pks).values_list(*value_names)
        )

    @staticmethod
    async def get_target_values(
        model: type[Model], fk_field: ForeignKeyFieldInstance[Any], pks: list[Any], db: DatabaseClient | None
    ) -> list[Any]:
        """The values the FK's key column(s) store for the given primary keys - the keys themselves
        unless ``to_field=`` names other columns, which takes a lookup. A composite target gives a
        tuple per row.
        """
        to_field_names = tuple(f.model_field_name for f in fk_field.to_field_instances)
        if to_field_names == model._meta.pk_attr_names:
            return pks
        query = RelatedRows.include_soft_deleted(
            RelatedRows.get_base_queryset(model).filter(pk__in=pks).using(RelatedRows.get_connection_for(model, db))
        )
        return [
            value
            for value in await query.values_list(*to_field_names, flat=len(to_field_names) == 1)
            if not RelatedRows.has_null_component(value)
        ]

    @staticmethod
    def split_into_batches(values: list[Any], db: DatabaseClient, column_count: int) -> list[list[Any]]:
        """Splits values bound ``column_count`` parameters each into batches within ``db``'s
        bind-parameter ceiling - a batch of composite values is capped further, its OR-ed row
        conditions grow the statement.

        Args:
            values: The values - scalars, or tuples of ``column_count`` components.
            db: The connection the lookup runs on.
            column_count: How many parameters one value binds.

        Returns:
            The batches.
        """
        batch_size = max(1, (db.features.max_bind_parameters - CASCADE_LOOKUP_BIND_PARAMS_HEADROOM) // column_count)
        if column_count > 1:
            batch_size = min(batch_size, CASCADE_LOOKUP_MAX_COMPOSITE_ROWS)
        return [values[start : start + batch_size] for start in range(0, len(values), batch_size)]

    @staticmethod
    def has_null_component(target_value: Any) -> bool:
        """Whether a stored FK target value (a scalar, or a tuple for a composite FK) is or
        contains ``None`` - such a parent is referenced by no row, since NULL never equals NULL."""
        if isinstance(target_value, tuple):
            return None in target_value
        return target_value is None

    @staticmethod
    def get_base_queryset(related_model: type[Model]) -> QuerySet[Any]:
        """The queryset every relation check starts from: every tenant's rows, through the base manager
        - a row referencing the deleted one exists whatever tenant is active and whatever a custom
        manager hides.
        """
        # hare.query.manager imports the queryset package, which imports this module back -
        # importing it at module level here would be circular.
        from hare.query.scopes.row_scopes import RowScopes

        return cast(
            "QuerySet[Any]",
            RowScopes.get_base_queryset(related_model, RowVisibility(all_tenants=True)),
        )

    @staticmethod
    def include_soft_deleted(queryset: QuerySet[Any]) -> QuerySet[Any]:
        """The queryset with soft-deleted rows included - a soft-deleted row still references the
        deleted one.
        """
        if queryset.model._meta.soft_delete_field:
            return queryset.include_deleted()
        return queryset

    @staticmethod
    async def get_soft_deleted_pks(model: type[Model], pks: list[Any], db: DatabaseClient | None) -> set[Any]:
        """The subset of ``pks`` whose ``model`` rows are already soft-deleted.

        Args:
            model: A model with ``Meta.soft_delete_field``.
            pks: Primary keys to check.
            db: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
        """
        soft_delete_field = cast("str", model._meta.soft_delete_field)
        pk_attr_names = model._meta.pk_attr_names
        model_db = RelatedRows.get_connection_for(model, db)
        lookup_db = model_db or model.get_connection(for_write=False)
        soft_deleted_pks: set[Any] = set()
        for batch in RelatedRows.split_into_batches(pks, lookup_db, len(pk_attr_names)):
            query = (
                RelatedRows.get_base_queryset(model)
                .include_deleted()
                .filter(pk__in=batch, **{f"{soft_delete_field}__isnull": False})
                .using(model_db)
            )
            soft_deleted_pks.update(await query.values_list(*pk_attr_names, flat=len(pk_attr_names) == 1))
        return soft_deleted_pks

    @staticmethod
    async def lock_soft_delete_values(
        model: type[Model], pks: list[Any], db: DatabaseClient | None
    ) -> dict[Any, datetime.datetime | None]:
        """Locks the ``model`` rows ``pks`` until the running transaction ends and reads their
        ``Meta.soft_delete_field`` values - a concurrent soft delete of the same rows waits here
        and then sees them already deleted. SQLite has no row locks; its writes are serialized.

        Args:
            model: A model with ``Meta.soft_delete_field``.
            pks: Primary keys to lock.
            db: Connection of the running transaction.

        Returns:
            ``{pk: soft-delete value}`` for every row that exists.
        """
        soft_delete_field = cast("str", model._meta.soft_delete_field)
        pk_attr_names = model._meta.pk_attr_names
        pk_column_count = len(pk_attr_names)
        lookup_db = RelatedRows.get_connection_for(model, db) or model.get_connection(for_write=True)
        soft_delete_values: dict[Any, datetime.datetime | None] = {}
        for batch in RelatedRows.split_into_batches(pks, lookup_db, pk_column_count):
            query = RelatedRows.get_base_queryset(model).include_deleted().filter(pk__in=batch).using(lookup_db)
            if lookup_db.features.supports_select_for_update:
                query = query.select_for_update(no_key=True)
            for values in await query.values_list(*pk_attr_names, soft_delete_field):
                row_pk = values[0] if pk_column_count == 1 else tuple(values[:pk_column_count])
                soft_delete_values[row_pk] = values[pk_column_count]
        return soft_delete_values

    @staticmethod
    def get_foreign_key_value(row: Model, fk_field: ForeignKeyFieldInstance[Any]) -> Any:
        """The value ``row`` stores in ``fk_field``'s shadow column(s) - a tuple for a composite FK."""
        if len(fk_field.source_fields) == 1:
            return getattr(row, cast("str", fk_field.source_field))
        return tuple(getattr(row, source_field) for source_field in fk_field.source_fields)

    @staticmethod
    async def get_pks_by_target_values(
        model: type[Model], fk_field: ForeignKeyFieldInstance[Any], pks: list[Any], db: DatabaseClient | None
    ) -> dict[Any, Any]:
        """Maps the values ``fk_field`` stores for the ``model`` rows ``pks`` back to their pks.

        Args:
            model: The model ``fk_field`` points at.
            fk_field: The FK/O2O field.
            pks: Primary keys of the ``model`` rows.
            db: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.

        Returns:
            ``{stored value: pk}`` - a tuple key for a composite FK.
        """
        to_field_names = tuple(to_field.model_field_name for to_field in fk_field.to_field_instances)
        pk_attr_names = model._meta.pk_attr_names
        if to_field_names == pk_attr_names or not pks:
            return {pk: pk for pk in pks}
        query = RelatedRows.include_soft_deleted(
            RelatedRows.get_base_queryset(model).filter(pk__in=pks).using(RelatedRows.get_connection_for(model, db))
        )
        pk_by_target_value = {}
        for values in await query.values_list(*pk_attr_names, *to_field_names):
            pk_values, target_values = values[: len(pk_attr_names)], values[len(pk_attr_names) :]
            if None in target_values:
                continue
            pk = pk_values[0] if len(pk_values) == 1 else tuple(pk_values)
            pk_by_target_value[target_values[0] if len(target_values) == 1 else tuple(target_values)] = pk
        return pk_by_target_value

    @staticmethod
    def get_through_row_criteria(
        model: type[Model], m2m_field: ManyToManyFieldInstance[Any], pks: list[Any], db: DatabaseClient | None
    ) -> tuple[DatabaseClient, Table, list[Criterion]]:
        """Selects the auto-generated through-table rows linking ``m2m_field`` to any ``model`` row
        in ``pks``, one criterion per batch that fits the backend's bind-parameter ceiling.

        Args:
            model: The model ``m2m_field`` is reached from.
            m2m_field: The M2M relation.
            pks: Primary keys of the ``model`` rows.
            db: The connection the delete runs on - used when the through table's owning model
                lives on it; otherwise, or when None, that model's own write connection.

        Returns:
            ``(the through table's connection, the through table, criteria)``.
        """
        owning_model = m2m_field.related_model if m2m_field._generated else model
        chosen_db = RelatedRows.get_connection_for(owning_model, db) or owning_model.get_connection(for_write=True)
        through_table = Table(m2m_field.through, schema=m2m_field.through_schema)
        backward_columns = [through_table[column_name] for column_name in m2m_field.backward_keys]
        pk_fields = model._meta.pk_fields if model._meta.has_composite_primary_key else (model._meta.pk,)
        pk_value_rows = [
            tuple(
                chosen_db.dialect.types.get_db_value(pk_field, pk_component, model)
                for pk_field, pk_component in zip(pk_fields, pk if len(pk_fields) > 1 else (pk,), strict=True)
            )
            for pk in pks
        ]
        criteria = [
            KeyColumns.row_is_in(backward_columns, batch)
            for batch in RelatedRows.split_into_batches(pk_value_rows, chosen_db, len(backward_columns))
        ]
        return chosen_db, through_table, criteria
