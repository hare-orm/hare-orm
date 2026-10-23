from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, TypeVar

from hare.core.connections.connections import Connections
from hare.core.routing.written_connections import WrittenConnections
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.write.rules.constants import REMOVE_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.write_steps import WriteSteps
from hare.query.key_columns import KeyColumns
from hare.query.queryset.relations.many_to_many.linked_keys import LinkedKeys
from hare.query.queryset.relations.many_to_many.many_to_many_members import ManyToManyMembers
from hare.query.queryset.relations.many_to_many.many_to_many_scope_checks import ManyToManyScopeChecks
from hare.query.queryset.relations.many_to_many.through_row_inserting import ThroughRowInserting
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.builder.tables.table import Table
from hare.sql.terms.tuple import Tuple
from hare.sql.terms.values.row_value_list import RowValueList
from hare.time import Timezone

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    import datetime

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.sql.terms.criteria.criterion import Criterion


class ThroughRowUnlinking:
    """The rows a many-to-many relation's ``remove()`` and ``clear()`` delete from its through table, and
    their report."""

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
        """Removes the through rows linking ``relation.instance`` to ``instances`` (all of them for
        ``None``) and reports the change (``ChangeEvents``) - shared by ``remove()``/``clear()``/
        ``set()`` and the ``on_delete=CASCADE`` cascade of a deleted owner.

        Args:
            relation: The many-to-many relation of an instance.
            instances: The related instances to unlink, or ``None`` to unlink every one.
            using: Connection to use instead of the through table's own.
            check_tenant_scope: See ``ThroughRowUnlinking.unlink()``.
            forward_pk_values: See ``ThroughRowUnlinking.unlink()``.
            visibility: See ``ThroughRowUnlinking.unlink()``.
        """
        through_model = relation.field.through_model_class
        if through_model is not None and through_model._meta.change_capture_needs is not None:
            # The links' captured changes are written with them.
            connection = Connections.get_client(using) or ThroughRows.through_table_connection(
                relation, for_write=True
            )
            if ChangeCapturing.needs_transaction(connection):
                async with connection._in_transaction() as transaction_connection:
                    await ThroughRowUnlinking.remove_or_clear(
                        relation,
                        instances,
                        transaction_connection,
                        check_tenant_scope=check_tenant_scope,
                        forward_pk_values=forward_pk_values,
                        visibility=visibility,
                    )
                return
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(relation, for_write=True)
        # On a database without transactions the rows, their capture and their report are one step a cancel can't
        # split.
        await WriteSteps.get_whole_run(
            connection,
            ThroughRowUnlinking.unlink_and_report(
                relation,
                instances,
                connection,
                check_tenant_scope=check_tenant_scope,
                forward_pk_values=forward_pk_values,
                visibility=visibility,
            ),
        )

    @staticmethod
    async def unlink_and_report(
        relation: ManyToManyRelation[Any],
        instances: tuple[TModel, ...] | None,
        connection: DatabaseClient,
        *,
        check_tenant_scope: bool,
        forward_pk_values: list[tuple[Any, ...]] | None,
        visibility: RowVisibility,
    ) -> None:
        """Removes the through rows linking ``relation.instance`` to ``instances`` (all of them for ``None``) and
        reports the change (``ChangeEvents``).

        Args:
            relation: The many-to-many relation of an instance.
            instances: The related instances to unlink, or ``None`` to unlink every one.
            connection: The connection of the through table.
            check_tenant_scope: See ``ThroughRowUnlinking.unlink()``.
            forward_pk_values: See ``ThroughRowUnlinking.unlink()``.
            visibility: See ``ThroughRowUnlinking.unlink()``.
        """
        related_instances = list(instances) if instances else None
        if related_instances and ChangeEvents.is_observed():
            # Only the instances linked before are reported - removing an unlinked one changes nothing.
            linked_pk_values = await LinkedKeys.get_linked_pk_values(relation, connection)
            related_meta = type(related_instances[0])._meta
            related_instances = [
                instance
                for instance in related_instances
                if KeyColumns.get_db_values(related_meta, instance, connection.dialect.types) in linked_pk_values
            ]
        unlinked_count = await ThroughRowUnlinking.unlink(
            relation,
            instances,
            connection,
            check_tenant_scope=check_tenant_scope,
            forward_pk_values=forward_pk_values,
            visibility=visibility,
        )
        # A count the database doesn't report may be of removed links.
        if (unlinked_count is None or unlinked_count) and ChangeEvents.is_observed():
            await ThroughRows.report_links(relation, connection, related_instances, RowOperation.DELETE)

    @staticmethod
    async def unlink(
        relation: ManyToManyRelation[Any],
        instances: tuple[TModel, ...] | None = None,
        using: str | DatabaseClient | None = None,
        *,
        check_tenant_scope: bool = True,
        forward_pk_values: list[tuple[Any, ...]] | None = None,
        visibility: RowVisibility = RowVisibility.DEFAULT,
    ) -> int | None:
        """Removes the through rows linking ``relation.instance`` to ``instances`` (all of them for
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

        Returns:
            How many through rows were removed or soft-deleted - None where the database reports no
            count of written rows.
        """
        ManyToManyMembers.validate_owner_saved(relation)
        if instances:
            ManyToManyMembers.validate_related_instances(relation, instances)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(relation, for_write=True)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        relation._reset_cache_on_rollback(connection)
        # Holding instances of another tenant doesn't let remove() unlink them - checked as add() checks.
        if check_tenant_scope:
            await ManyToManyScopeChecks.check_tenant_scope(relation, instances, connection)
        # Rows fetched onto the instance before would still show the removed members.
        relation._invalidate_local_cache()
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        backward_columns = [through_table[column] for column in relation.field.backward_keys]
        backward_key_values = KeyColumns.get_db_values(
            type(relation.instance)._meta, relation.instance, connection.dialect.types
        )

        condition = KeyColumns.row_equality(backward_columns, backward_key_values)
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
            visible_target_criterion = await ManyToManyScopeChecks.get_visible_links_criterion(
                relation, through_table, visibility=visibility, connection=connection
            )
            if visible_target_criterion is not None:
                condition &= visible_target_criterion
        through_model = relation.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        capture_needs = through_model._meta.change_capture_needs if through_model is not None else None
        # A through model with Meta.soft_delete_field is soft-deleted, like any other model.
        operation = RowOperation.UPDATE if soft_delete_field is not None else RowOperation.DELETE
        if through_model is not None and soft_delete_field is not None:
            # Only live rows - a row soft-deleted before keeps its deletion time.
            condition &= through_table[through_model._meta.fields_db_projection[soft_delete_field]].isnull()
        now = Timezone.now()
        if through_model is None or capture_needs is None or not capture_needs.captures(operation):
            query = ThroughRowUnlinking.get_unlink_statement(connection, relation, through_table, condition, now)
            return (await connection.execute(*ThroughRows.get_statement(relation, query)))[0]
        captured = CapturedWrite(
            through_model,
            capture_needs,
            connection,
            operation,
            changed=None if soft_delete_field is None else [soft_delete_field],
        )
        if not connection.features.supports_returning:
            return await ThroughRowUnlinking.unlink_captured_by_reading(
                relation, captured, through_table, condition, now
            )
        before_by_pk = None
        if operation is RowOperation.UPDATE and capture_needs.reads_before:
            before_query = connection.query_class.from_(through_table).where(condition).select(*capture_needs.columns)
            _, before_rows = await connection.execute(*ThroughRows.get_statement(relation, before_query))
            types = connection.dialect.types
            before_by_pk = {
                ChangeCapturing.get_row_key(through_model, capture_needs, types, dict(row))[0]: (
                    ChangeCapturing.get_row_values(through_model, capture_needs, types, dict(row))
                )
                for row in before_rows
            }
        query = ThroughRowUnlinking.get_unlink_statement(
            connection, relation, through_table, condition, now
        ).returning(*capture_needs.columns)
        count, returned_rows = await connection.execute(*ThroughRows.get_statement(relation, query), returns_rows=True)
        await captured.capture_returned(returned_rows, before_by_pk=before_by_pk)
        return count

    @staticmethod
    def get_unlink_statement(
        connection: DatabaseClient,
        relation: ManyToManyRelation[Any],
        through_table: Table,
        condition: Criterion,
        now: datetime.datetime,
    ) -> Any:
        """The statement unlinking the through rows of a condition - a DELETE, or the soft delete of a through
        model with ``Meta.soft_delete_field``, which bumps the version and stamps the ``auto_now`` fields as every
        other write does.

        Args:
            connection: The connection of the write.
            relation: The many-to-many relation of an instance.
            through_table: The through table.
            condition: The rows unlinked - the live ones of a soft-deleted through model.
            now: The moment of the write.

        Returns:
            The statement.
        """
        through_model = relation.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is None or soft_delete_field is None:
            return connection.query_class.from_(through_table).where(condition).delete()
        meta = through_model._meta
        soft_delete_model_field = meta.fields_map[soft_delete_field]
        soft_delete_column = meta.fields_db_projection[soft_delete_field]
        types = connection.dialect.types
        query = (
            connection.query_class.update(through_table)
            .where(condition)
            .set(soft_delete_column, types.get_db_value(soft_delete_model_field, now, None))
        )
        rules = RowWriteRules.get(through_model, WriteType.SOFT_DELETE, [soft_delete_field], REMOVE_METHOD_NAME)
        if (version_field := rules.version_field_name) is not None:
            version_column = meta.fields_db_projection[version_field]
            query = query.set(version_column, through_table[version_column] + 1)
        for field_name, field_object, value in rules.get_stamp_values(now):
            query = query.set(meta.fields_db_projection[field_name], types.get_db_value(field_object, value, None))
        return query

    @staticmethod
    async def unlink_captured_by_reading(
        relation: ManyToManyRelation[Any],
        captured: CapturedWrite,
        through_table: Table,
        condition: Criterion,
        now: datetime.datetime,
    ) -> int | None:
        """Unlinks the through rows of a captured through model on a database whose writes return no rows: the
        rows are read first, the write narrowed to them, and a soft delete's rows read again after it.

        Args:
            relation: The many-to-many relation of an instance.
            captured: The capture of the unlink.
            through_table: The through table.
            condition: The rows unlinked.
            now: The moment of the write.

        Returns:
            How many through rows were removed or soft-deleted - None where the database reports no
            count of written rows.
        """
        connection = captured.connection
        meta = captured.model._meta
        key_columns = [through_table[meta.fields_db_projection[name]] for name in meta.primary_key_attribute_names]
        key_query = connection.query_class.from_(through_table).where(condition).select(*key_columns)
        _, key_rows = await connection.execute(*ThroughRows.get_statement(relation, key_query))
        # A row by column name on some drivers - its values are in the order selected.
        rows = [tuple(row.values()) if isinstance(row, Mapping) else tuple(row) for row in key_rows]
        if not rows:
            return 0
        pks = ThroughRowInserting.get_row_keys(captured.model, key_columns, rows, connection.dialect.types)
        read_before = await captured.read(pks, lock=True)
        key_condition = Tuple(*key_columns).isin(RowValueList(*[Tuple(*row) for row in rows]))
        query = ThroughRowUnlinking.get_unlink_statement(
            connection, relation, through_table, condition & key_condition, now
        )
        count = (await connection.execute(*ThroughRows.get_statement(relation, query)))[0]
        await captured.capture_read(read_before, pks)
        return count
