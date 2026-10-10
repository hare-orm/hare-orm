from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.core.connections.connections import Connections
from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import QueryError
from hare.fields.constants import MANY_TO_MANY_WRITE_BIND_PARAMETERS_HEADROOM
from hare.fields.data.temporal.datetime_field import DatetimeField
from hare.fields.data.temporal.time_field import TimeField
from hare.fields.enums import RelationType
from hare.fields.field import Field
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.instances.instance_connections import InstanceConnections
from hare.models.write.write_steps import WriteSteps
from hare.query.key_columns import KeyColumns
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.relations.many_to_many.many_to_many_members import ManyToManyMembers
from hare.query.queryset.relations.many_to_many.many_to_many_scope_checks import ManyToManyScopeChecks
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.builder.tables.table import Table
from hare.time import Timezone

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.sql.terms.field import Field as SqlField


class ThroughRows:
    """The rows of a many-to-many relation's through table: inserted for new links with the through
    model's defaults and the active tenant, deleted when links are removed or cleared, on the
    connection of the model declaring the field - and the report of the links changed."""

    @staticmethod
    def through_table_connection(relation: ManyToManyRelation[Any], for_write: bool) -> DatabaseClient:
        """The connection the through table lives on: that of the model declaring the forward side of
        the field - the instance's model from the forward side, the related model from the backward
        one.

        Args:
            relation: The many-to-many relation of an instance.
            for_write: Whether the connection is one to write through.
        """
        owning_model = relation.model if relation.field._generated else type(relation.instance)
        return InstanceConnections.get_connection_for_instance(relation.instance, for_write, model=owning_model)

    @staticmethod
    async def add_links(
        relation: ManyToManyRelation[Any],
        instances: tuple[Any, ...],
        through_defaults: dict[str, Any] | None,
        using: str | DatabaseClient | None,
    ) -> list[Any]:
        """Everything ``add()`` does but reporting the change.

        Args:
            relation: The many-to-many relation of an instance.
            instances: Related instances or their primary key values.
            through_defaults: See ``add()``.
            using: See ``add()``.

        Returns:
            The related instances.
        """
        ManyToManyMembers.validate_owner_saved(relation)
        instances = await ManyToManyMembers.get_related_instances(relation, instances, Connections.get_client(using))
        ManyToManyMembers.validate_related_instances(relation, instances)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(relation, for_write=True)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        relation._reset_cache_on_rollback(connection)
        pk_b_values = KeyColumns.get_db_values(
            type(relation.instance)._meta, relation.instance, connection.dialect.types
        )
        related_meta = type(instances[0])._meta
        await ManyToManyScopeChecks.check_tenant_scope(relation, instances, connection)
        await ManyToManyScopeChecks.check_visible_scope(relation, instances, connection)
        # The fetched rows are dropped up front - every exit below then leaves no stale snapshot.
        relation._invalidate_local_cache()
        pks_f: list[tuple[Any, ...]] = [
            KeyColumns.get_db_values(related_meta, instance_to_add, connection.dialect.types)
            for instance_to_add in instances
        ]
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        backward_columns = [through_table[column] for column in relation.field.backward_keys]
        forward_columns = [through_table[column] for column in relation.field.forward_keys]
        through_defaults = ThroughRows.through_defaults_with_active_tenant(relation, through_defaults)
        extra_columns, extra_values = ThroughRows.through_defaults_columns_and_values(
            relation, through_defaults, connection.dialect.types
        )

        if (
            relation.field.unique
            and relation.field.through_model is None
            and connection.features.supports_unique_constraints
        ):
            # The through table's unique index lets one INSERT ... ON CONFLICT DO NOTHING skip the
            # pairs already linked. Not for a through=Model table, which has no such index of
            # hare's, nor on a database without unique constraints.
            unique_pks_f = set(pks_f)
            if len(unique_pks_f) == 1:
                # The single-row statement of .add(one_instance) is the same on every call -
                # rendered once per field and dialect.
                statement_key = (relation.field.model, relation.field.model_field_name, connection.query_class)
                cached_sql = StatementPlans.many_to_many_add_statements.get(statement_key)
                if cached_sql is None:
                    query = connection.query_class.into(through_table).columns(*forward_columns, *backward_columns)
                    placeholder_row = (0,) * (len(forward_columns) + len(backward_columns))
                    query = query.insert(*placeholder_row)  # placeholder values - only the SQL text is kept
                    query = query.on_conflict(*backward_columns, *forward_columns).do_nothing()
                    cached_sql, _ = query.get_parameterized_sql()
                    StatementPlans.many_to_many_add_statements[statement_key] = cached_sql
                (single_pk_f,) = unique_pks_f
                await connection.execute(cached_sql, [*single_pk_f, *pk_b_values])
                return list(instances)
            rows = [(*pk_f, *pk_b_values) for pk_f in unique_pks_f]
            await ThroughRows.insert_through_rows(
                connection,
                through_table,
                [*forward_columns, *backward_columns],
                rows,
                conflict_target_columns=[*backward_columns, *forward_columns],
            )
            return list(instances)

        select_query = connection.query_class.from_(through_table).where(
            KeyColumns.row_equality(backward_columns, pk_b_values)
        )
        select_query = select_query.select(*relation.field.forward_keys)
        criterion = (
            KeyColumns.row_equality(forward_columns, pks_f[0])
            if len(pks_f) == 1
            else connection.dialect.filter_operators.get_row_membership_criterion(
                forward_columns, pks_f, ManyToManyMembers.get_forward_key_fields(relation)
            )
        )
        select_query = select_query.where(criterion)
        through_model = relation.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            # A soft-deleted through row is no active link - the pair is added again.
            soft_delete_column = through_model._meta.fields_map[soft_delete_field].source_field or soft_delete_field
            select_query = select_query.where(through_table[soft_delete_column].isnull())

        _, already_existing_relations_raw = await connection.execute(*select_query.get_parameterized_sql())
        # The related model's key fields convert a related instance's values - instances[0], not
        # self.instance.
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        already_existing_forward_pks = {
            tuple(
                connection.dialect.types.get_db_value(
                    field,
                    connection.dialect.types.get_python_value(field, existing_relation[forward_column]),
                    instances[0],
                )
                for field, forward_column in zip(related_pk_fields, relation.field.forward_keys, strict=True)
            )
            for existing_relation in already_existing_relations_raw
        }

        if pks_f_to_insert := set(pks_f) - already_existing_forward_pks:
            default_fields = ThroughRows.through_model_default_fields(relation, through_defaults)
            default_columns = [through_table[column_name] for column_name, _ in default_fields]
            rows = [
                (
                    *pk_f,
                    *pk_b_values,
                    *extra_values,
                    *await ThroughRows.through_model_default_values(
                        relation, default_fields, connection.dialect.types
                    ),
                )
                for pk_f in pks_f_to_insert
            ]
            await ThroughRows.insert_through_rows(
                connection,
                through_table,
                [*forward_columns, *backward_columns, *extra_columns, *default_columns],
                rows,
                captured_model=through_model,
            )
        return list(instances)

    @staticmethod
    async def insert_through_rows(
        connection: DatabaseClient,
        through_table: Table,
        columns: list[SqlField],
        rows: list[tuple[Any, ...]],
        conflict_target_columns: list[SqlField] | None = None,
        captured_model: type[Model] | None = None,
    ) -> None:
        """Inserts through-table rows in as few multi-row INSERTs as the backend's bind-parameter
        ceiling allows - in one transaction when it takes more than one.

        Args:
            connection: Connection to write on.
            through_table: The through table.
            columns: The inserted columns, in row order.
            rows: One value tuple per inserted row.
            conflict_target_columns: The ``ON CONFLICT DO NOTHING`` target, or None to insert
                without a conflict clause.
            captured_model: The ``through=Model`` model - its rows are captured when it declares
                ``Meta.change_capture``.
        """
        capture_needs = captured_model._meta.change_capture_needs if captured_model is not None else None
        if capture_needs is not None and not capture_needs.captures(RowOperation.INSERT):
            capture_needs = None
        rows_per_statement = max(
            1, (connection.features.max_bind_parameters - MANY_TO_MANY_WRITE_BIND_PARAMETERS_HEADROOM) // len(columns)
        )
        statements = []
        for start in range(0, len(rows), rows_per_statement):
            query = connection.query_class.into(through_table).columns(*columns)
            for row in rows[start : start + rows_per_statement]:
                query = query.insert(*row)
            if conflict_target_columns is not None:
                query = query.on_conflict(*conflict_target_columns).do_nothing()
            if capture_needs is not None:
                query = query.returning(*capture_needs.columns)
            statements.append(query.get_parameterized_sql())
        if capture_needs is not None:
            captured = cast("type[Model]", captured_model)
            for sql, values in statements:
                _, returned_rows = await connection.execute(sql, values, returns_rows=True)
                changes = ChangeCapturing.get_returned_changes(
                    captured, capture_needs, connection.dialect.types, returned_rows, RowOperation.INSERT
                )
                await ChangeCapturing.capture(connection, captured, changes)
            return
        if len(statements) == 1:
            await connection.execute(*statements[0])
            return
        async with connection._in_transaction() as transaction_connection:
            for sql, values in statements:
                await transaction_connection.execute(sql, values)

    @staticmethod
    def through_defaults_with_active_tenant(
        relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """Adds the through model's one tenant to ``through_defaults`` when it has
        ``Meta.tenant_field`` set and the caller didn't give it.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.

        Raises:
            QueryError: ``through_defaults`` gives a tenant outside the through model's scope, or
                gives none under a scope of several values.
        """
        # Local import: same package-init cycle as ManyToManyScopeChecks.check_instance_tenant().
        from hare.models.tenancy.tenancy import Tenancy

        through_model = relation.field.through_model_class
        tenant_field = through_model._meta.tenant_field if through_model is not None else None
        if tenant_field is None:
            return through_defaults
        through_model = cast("type[Model]", through_model)
        active_tenant = Tenancy.get_scope(through_model)
        if active_tenant is None:
            return through_defaults
        if through_defaults and tenant_field in through_defaults:
            if not Tenancy.allows(through_model, active_tenant, through_defaults[tenant_field]):
                raise QueryError(
                    f"through_defaults sets '{tenant_field}'={through_defaults[tenant_field]!r}, which "
                    f"differs from the active tenant ({active_tenant!r})."
                )
            return through_defaults
        if not Tenancy.has_one_value(active_tenant):
            raise QueryError(
                f"{through_model.__name__} rows need '{tenant_field}' and the active tenant scope "
                f"({active_tenant!r}) has no single value to give them - name it in through_defaults"
            )
        return {**(through_defaults or {}), tenant_field: active_tenant}

    @staticmethod
    def through_model_default_fields(
        relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None
    ) -> list[tuple[str, Field[Any]]]:
        """The through model's fields filled from a Python-side default - ``default=``,
        ``auto_now``/``auto_now_add`` - as ``(column name, field)`` pairs: ``add()`` inserts through
        rows with a raw INSERT. A ``db_default`` is left to the database.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.
        """
        through_model = relation.field.through_model_class
        if through_model is None:
            return []
        relation_columns = set(relation.field.forward_keys) | set(relation.field.backward_keys)
        provided = set(through_defaults or ())
        default_fields: list[tuple[str, Field[Any]]] = []
        for field_name, field_object in through_model._meta.fields_map.items():
            column_name = field_object.source_field or field_name
            if (
                not field_object.has_db_field
                or field_object.generated
                or column_name in relation_columns
                or field_name in provided
            ):
                continue
            if (
                field_object.default is not None
                or field_object._default_is_coroutine
                or getattr(field_object, "auto_now", False)
                or getattr(field_object, "auto_now_add", False)
            ):
                default_fields.append((column_name, field_object))
        return default_fields

    @staticmethod
    async def through_model_default_values(
        relation: ManyToManyRelation[Any], default_fields: list[tuple[str, Field[Any]]], types: TypeRegistry
    ) -> list[Any]:
        """One through row's DB-ready values for ``ThroughRows.through_model_default_fields()``'s fields,
        computed per row (a callable default such as ``uuid4`` must produce a fresh value for each
        inserted row) by letting a real, never-saved through-model instance apply its own defaults.

        Args:
            relation: The many-to-many relation of an instance.
            default_fields: The fields ``through_model_default_fields()`` gave.
            types: The type registry of the through table's connection.
        """
        if not default_fields:
            return []
        through_model = relation.field.through_model_class
        assert through_model is not None  # nosec B101
        row_instance = through_model()
        for field_name, awaitable_default in row_instance._await_when_save.items():
            setattr(row_instance, field_name, await awaitable_default())
        return [
            types.get_db_value(field_object, getattr(row_instance, field_object.model_field_name), row_instance)
            for _, field_object in default_fields
        ]

    @staticmethod
    def through_defaults_columns_and_values(
        relation: ManyToManyRelation[Any], through_defaults: dict[str, Any] | None, types: TypeRegistry
    ) -> tuple[list[SqlField], list[Any]]:
        """Resolves ``through_defaults`` into the through table's own extra column fields and
        their already-DB-converted values, ready to append onto ``.add()``'s own INSERT column
        list/values.

        Args:
            relation: The many-to-many relation of an instance.
            through_defaults: The values the caller gives the through rows' own fields, None for none.
            types: The type registry of the through table's connection.

        Raises:
            QueryError: ``through_defaults`` names a field that doesn't exist on the
                through model, one of the through model's own FK fields making up the relation
                itself, or a ``GeneratedField`` (computed by the database, not written to).
        """
        if not through_defaults:
            return [], []
        through_model = relation.field.through_model_class
        if through_model is None:
            raise QueryError(
                f"through_defaults isn't supported on '{relation.field.model_field_name}' - it has no "
                "real through model (through=Model) to hold extra fields."
            )
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        relation_columns = set(relation.field.forward_keys) | set(relation.field.backward_keys)
        columns = []
        values = []
        for field_name, value in through_defaults.items():
            field_object = through_model._meta.fields_map.get(field_name)
            if field_object is None:
                raise QueryError(
                    f"through_defaults names '{field_name}', which isn't a field on through model "
                    f"'{through_model.__name__}'."
                )
            column_name = field_object.source_field or field_object.model_field_name
            field_column_names = (
                set(cast("ForeignKeyFieldInstance[Any]", field_object).db_column_names)
                if field_object.relation_type in {RelationType.FOREIGN_KEY, RelationType.ONE_TO_ONE}
                else {column_name}
            )
            if field_column_names & relation_columns:
                raise QueryError(
                    f"through_defaults cannot set '{field_name}' - it's one of the through model's "
                    "own FK fields making up the relation itself."
                )
            if field_object.generated:
                raise QueryError(
                    f"through_defaults cannot set '{field_name}' - it's a generated field, "
                    "computed by the database, not written to."
                )
            columns.append(through_table[column_name])
            values.append(types.get_db_value(field_object, value, through_model))
        return columns, values

    @staticmethod
    async def remove_or_clear(
        relation: ManyToManyRelation[Any],
        instances: tuple[TModel, ...] | None = None,
        using: str | DatabaseClient | None = None,
        *,
        check_tenant_scope: bool = True,
        forward_pk_values: list[tuple[Any, ...]] | None = None,
        visibility: RowVisibility = RowVisibility.DEFAULT,
    ) -> None:
        """Removes the through rows linking ``self.instance`` to ``instances`` (all of them for
        ``None``) and reports the change (``ChangeEvents``) - shared by ``remove()``/``clear()``/
        ``set()`` and the ``on_delete=CASCADE`` cascade of a deleted owner.

        Args:
            relation: The many-to-many relation of an instance.
            instances: The related instances to unlink, or ``None`` to unlink every one.
            using: Connection to use instead of the through table's own.
            check_tenant_scope: See ``ThroughRows.unlink()``.
            forward_pk_values: See ``ThroughRows.unlink()``.
            visibility: See ``ThroughRows.unlink()``.
        """
        through_model = relation.field.through_model_class
        if through_model is not None and through_model._meta.change_capture_needs is not None:
            # The links' captured changes are written with them.
            connection = Connections.get_client(using) or ThroughRows.through_table_connection(
                relation, for_write=True
            )
            if ChangeCapturing.needs_transaction(connection):
                async with connection._in_transaction() as transaction_connection:
                    await ThroughRows.remove_or_clear(
                        relation,
                        instances,
                        transaction_connection,
                        check_tenant_scope=check_tenant_scope,
                        forward_pk_values=forward_pk_values,
                        visibility=visibility,
                    )
                return
        await ThroughRows.unlink(
            relation,
            instances,
            using,
            check_tenant_scope=check_tenant_scope,
            forward_pk_values=forward_pk_values,
            visibility=visibility,
        )
        if ChangeEvents.is_observed():
            related_instances = list(instances) if instances else None
            await ThroughRows.report_links(
                relation,
                Connections.get_client(using) or ThroughRows.through_table_connection(relation, for_write=True),
                related_instances,
                RowOperation.DELETE,
            )

    @staticmethod
    async def unlink(
        relation: ManyToManyRelation[Any],
        instances: tuple[TModel, ...] | None = None,
        using: str | DatabaseClient | None = None,
        *,
        check_tenant_scope: bool = True,
        forward_pk_values: list[tuple[Any, ...]] | None = None,
        visibility: RowVisibility = RowVisibility.DEFAULT,
    ) -> None:
        """Removes the through rows linking ``self.instance`` to ``instances`` (all of them for
        ``None``).

        Args:
            relation: The many-to-many relation of an instance.
            instances: The related instances to unlink, or ``None`` to unlink every one.
            using: Connection to use instead of the through table's own.
            check_tenant_scope: ``False`` for a cascade, which follows the through rows of a row
                it is deleting regardless of the active tenant scope, and for ``clear(all_tenants=True)``.
            forward_pk_values: DB-ready primary key values of the related rows to unlink, in
                place of ``instances``.
            visibility: Which related rows the related model's default scope shows when unlinking
                every row - every tenant's for ``clear(all_tenants=True)``.
        """
        ManyToManyMembers.validate_owner_saved(relation)
        if instances:
            ManyToManyMembers.validate_related_instances(relation, instances)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(relation, for_write=True)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        relation._reset_cache_on_rollback(connection)
        # remove()/clear() had no tenant check at all (unlike add()'s own _check_tenant_scope) -
        # nothing stopped removing/soft-deleting a through-table row linking a DIFFERENT tenant's
        # rows just because the caller happened to hold Python references to them.
        if check_tenant_scope:
            await ManyToManyScopeChecks.check_tenant_scope(relation, instances, connection)
        # Same staleness as add() (see its own comment) - a relation already fetched onto this
        # instance kept showing removed members after remove()/clear().
        relation._invalidate_local_cache()
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        backward_columns = [through_table[column] for column in relation.field.backward_keys]
        pk_b_values = KeyColumns.get_db_values(
            type(relation.instance)._meta, relation.instance, connection.dialect.types
        )

        condition = KeyColumns.row_equality(backward_columns, pk_b_values)
        if instances:
            related_meta = type(instances[0])._meta
            forward_pk_values = [
                KeyColumns.get_db_values(related_meta, instance, connection.dialect.types) for instance in instances
            ]
        if forward_pk_values:
            forward_columns = [through_table[column] for column in relation.field.forward_keys]
            condition &= (
                KeyColumns.row_equality(forward_columns, forward_pk_values[0])
                if len(forward_pk_values) == 1
                else connection.dialect.filter_operators.get_row_membership_criterion(
                    forward_columns, forward_pk_values, ManyToManyMembers.get_forward_key_fields(relation)
                )
            )
        if (check_tenant_scope or visibility.all_tenants) and not instances:
            # clear()/set() name no related rows: only the links to rows the related model's default
            # scope shows are theirs to remove. remove() names its rows, and can drop a link to a
            # hidden one.
            visible_target_criterion = ManyToManyScopeChecks.get_visible_target_criterion(
                relation, through_table, visibility=visibility, dialect=connection.dialect, connection=connection
            )
            if visible_target_criterion is not None:
                condition &= visible_target_criterion
        through_model = relation.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        capture_needs = through_model._meta.change_capture_needs if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            # A through model with Meta.soft_delete_field is soft-deleted, like any other model.
            soft_delete_field_obj = through_model._meta.fields_map[soft_delete_field]
            soft_delete_column = soft_delete_field_obj.source_field or soft_delete_field
            now = Timezone.now()
            # Only live rows - a row soft-deleted before keeps its deletion time.
            condition &= through_table[soft_delete_column].isnull()
            query = connection.query_class.update(through_table).where(condition)
            query = query.set(
                soft_delete_column, connection.dialect.types.get_db_value(soft_delete_field_obj, now, through_model)
            )
            # The optimistic lock field and the auto_now fields are bumped, as every other write
            # does.
            if optimistic_lock_field := through_model._meta.optimistic_lock_field:
                version_column = through_model._meta.fields_db_projection[optimistic_lock_field]
                query = query.set(version_column, through_table[version_column] + 1)
            for field_name, field_object in through_model._meta.fields_map.items():
                if (
                    isinstance(field_object, DatetimeField | TimeField)
                    and field_object.auto_now
                    and field_name != soft_delete_field
                ):
                    query = query.set(
                        through_model._meta.fields_db_projection[field_name],
                        connection.dialect.types.get_db_value(
                            field_object, field_object.get_auto_now_value(now), through_model
                        ),
                    )
            if capture_needs is not None and capture_needs.captures(RowOperation.UPDATE):
                before_by_pk = None
                if capture_needs.reads_before:
                    before_query = (
                        connection.query_class.from_(through_table).where(condition).select(*capture_needs.columns)
                    )
                    _, before_rows = await connection.execute(*before_query.get_parameterized_sql())
                    types = connection.dialect.types
                    before_by_pk = {
                        ChangeCapturing.get_row_key(through_model, capture_needs, types, dict(row))[0]: (
                            ChangeCapturing.get_row_values(through_model, capture_needs, types, dict(row))
                        )
                        for row in before_rows
                    }
                query = query.returning(*capture_needs.columns)
                _, returned_rows = await connection.execute(*query.get_parameterized_sql(), returns_rows=True)
                changes = ChangeCapturing.get_returned_changes(
                    through_model,
                    capture_needs,
                    connection.dialect.types,
                    returned_rows,
                    RowOperation.UPDATE,
                    changed=[soft_delete_field],
                    before_by_pk=before_by_pk,
                )
                await ChangeCapturing.capture(connection, through_model, changes)
                return
            await connection.execute(*query.get_parameterized_sql())
            return
        query = connection.query_class.from_(through_table).where(condition).delete()
        if through_model is not None and capture_needs is not None and capture_needs.captures(RowOperation.DELETE):
            query = query.returning(*capture_needs.columns)
            _, returned_rows = await connection.execute(*query.get_parameterized_sql(), returns_rows=True)
            changes = ChangeCapturing.get_returned_changes(
                through_model, capture_needs, connection.dialect.types, returned_rows, RowOperation.DELETE
            )
            await ChangeCapturing.capture(connection, through_model, changes)
            return
        await connection.execute(*query.get_parameterized_sql())

    @staticmethod
    async def get_linked_pk_values(
        relation: ManyToManyRelation[Any], connection: DatabaseClient
    ) -> frozenset[tuple[Any, ...]]:
        """The DB-ready primary key values of every related row actively linked to ``self.instance``.

        Args:
            relation: The many-to-many relation of an instance.
            connection: Connection to read the through table on.
        """
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        backward_columns = [through_table[column_name] for column_name in relation.field.backward_keys]
        pk_b_values = KeyColumns.get_db_values(
            type(relation.instance)._meta, relation.instance, connection.dialect.types
        )
        query = (
            connection.query_class.from_(through_table)
            .where(KeyColumns.row_equality(backward_columns, pk_b_values))
            .select(*relation.field.forward_keys)
        )
        through_model = relation.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            soft_delete_column = through_model._meta.fields_map[soft_delete_field].source_field or soft_delete_field
            query = query.where(through_table[soft_delete_column].isnull())
        if (
            visible_target_criterion := ManyToManyScopeChecks.get_visible_target_criterion(
                relation, through_table, dialect=connection.dialect, connection=connection
            )
        ) is not None:
            query = query.where(visible_target_criterion)
        _, rows = await connection.execute(*query.get_parameterized_sql())
        related_meta = relation.model._meta
        related_pk_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        linked_pk_values: list[tuple[Any, ...]] = []
        for row in rows:
            if any(row[column_name] is None for column_name in relation.field.forward_keys):
                continue
            linked_pk_values.append(
                tuple(
                    connection.dialect.types.get_db_value(pk_field, row[column_name], relation.model)
                    for pk_field, column_name in zip(related_pk_fields, relation.field.forward_keys, strict=True)
                )
            )
        return frozenset(linked_pk_values)

    @staticmethod
    async def report_links(
        relation: ManyToManyRelation[Any],
        connection: DatabaseClient,
        related_instances: Sequence[Any] | None,
        operation: RowOperation,
    ) -> None:
        """Reports links added or removed (``ChangeEvents``): the owner's and the related rows'
        relation changed, and the rows of a through model were inserted or deleted.

        Args:
            relation: The many-to-many relation of an instance.
            connection: The connection the links were written on.
            related_instances: The related rows, None for every one.
            operation: ``INSERT`` for added links, ``DELETE`` for removed ones.
        """
        await WriteSteps.report(
            connection,
            type(relation.instance),
            RowOperation.UPDATE,
            instances=[relation.instance],
            fields=[relation.field.model_field_name],
        )
        related_fields = [relation.field.related_name] if relation.field.related_name else None
        if related_instances is None:
            await WriteSteps.report(connection, relation.model, RowOperation.UPDATE, fields=related_fields)
        else:
            await WriteSteps.report(
                connection,
                relation.model,
                RowOperation.UPDATE,
                instances=list(related_instances),
                fields=related_fields,
            )
        through_model = relation.field.through_model_class
        if through_model is not None:
            if operation is RowOperation.DELETE and through_model._meta.soft_delete_field:
                await WriteSteps.report(
                    connection, through_model, RowOperation.UPDATE, fields=[through_model._meta.soft_delete_field]
                )
            else:
                await WriteSteps.report(connection, through_model, operation)
