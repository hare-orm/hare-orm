from __future__ import annotations

from contextlib import nullcontext
from itertools import chain
from typing import TYPE_CHECKING, Any

from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import UnSupportedError
from hare.fields.constants import MANY_TO_MANY_WRITE_BIND_PARAMETERS_HEADROOM
from hare.fields.field import Field
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.enums import RowOperation
from hare.models.uniqueness.declared_uniqueness import DeclaredUniqueness
from hare.models.write.generated_keys import GeneratedKeys
from hare.query.execution.bound_write import BoundWrite
from hare.query.execution.write_statements import WriteCopy, WriteStatement
from hare.query.key_columns import KeyColumns
from hare.query.key_comparison import KeyComparison
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.relations.many_to_many.linked_keys import LinkedKeys
from hare.query.queryset.relations.many_to_many.many_to_many_members import ManyToManyMembers
from hare.query.queryset.relations.many_to_many.many_to_many_scope_checks import ManyToManyScopeChecks
from hare.query.queryset.relations.many_to_many.through_defaults import ThroughDefaults
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.builder.tables.table import Table
from hare.sql.terms.parameters.query_parameters import QueryParameters

if TYPE_CHECKING:
    from collections.abc import Iterable

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.models.model_binding import ModelBinding
    from hare.query.queryset.relations.many_to_many.through_values import ThroughValues
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.sql.terms.field import Field as SqlField


class ThroughRowInserting:
    """The rows a many-to-many relation's ``add()`` inserts into its through table - the links not there yet,
    soft-deleted ones restored - in as few statements as the database takes."""

    @staticmethod
    async def add_links(
        relation: ManyToManyRelation[Any],
        instances: tuple[Any, ...],
        prepared: ThroughValues,
        connection: DatabaseClient,
    ) -> tuple[list[Any] | None, bool]:
        """Everything ``add()`` does but reporting the change.

        Args:
            relation: The many-to-many relation of an instance.
            instances: Related instances or their primary key values.
            prepared: Through assignments already converted by the enclosing operation.
            connection: The exact through-table client already selected by ``add()``.

        Returns:
            The related instances linked, each once - none when every pair was linked already, None when
            some were and the database doesn't tell which - and whether through rows
            were inserted: a soft-deleted link restored is an update of its row, reported by the restore.
        """
        ManyToManyMembers.validate_owner_saved(relation)
        instances = await ManyToManyMembers.get_related_instances(
            relation,
            instances,
            ManyToManyMembers.get_connection_of(relation, relation.model, connection),
        )
        ManyToManyMembers.validate_related_instances(relation, instances)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection)
        relation._reset_cache_on_rollback(connection)
        backward_key_values = KeyColumns.get_db_values(
            relation.instance._binding, relation.instance, connection.dialect.types
        )
        related_meta: ModelBinding = instances[0]._binding
        await ManyToManyScopeChecks.check_tenant_scope(relation, instances, connection)
        await ManyToManyScopeChecks.check_visible_scope(relation, instances, connection)
        # The fetched rows are dropped up front - every exit below then leaves no stale snapshot.
        relation._invalidate_local_cache()
        forward_key_values = KeyColumns.get_db_rows(related_meta, instances, connection.dialect.types)
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        backward_columns = [through_table[column] for column in relation.field.backward_keys]
        forward_columns = [through_table[column] for column in relation.field.forward_keys]
        through_defaults = prepared.given_values
        extra_columns, extra_values = prepared.columns, prepared.database_values

        if (
            relation.field.unique
            and relation.field.through_model is None
            and connection.features.supports_unique_constraints
        ):
            # The through table's unique index lets one INSERT ... ON CONFLICT DO NOTHING skip the
            # pairs already linked. Not for a through=Model table, which has no such index of
            # hare's, nor on a database without unique constraints.
            unique_forward_key_values = set(forward_key_values)
            if len(unique_forward_key_values) == 1:
                # The single-row statement of .add(one_instance) is the same on every call -
                # rendered once per field and dialect.
                statement_key = (relation.field.model, relation.field.model_field_name, connection.query_class)
                statements = StatementPlans.many_to_many_add_statements.get_bucket(
                    relation.field.get_binding().model_binding
                )
                cached_sql = statements.get(statement_key)
                if cached_sql is None:
                    query = connection.query_class.into(through_table).columns(*forward_columns, *backward_columns)
                    placeholder_row = (0,) * (len(forward_columns) + len(backward_columns))
                    query = query.insert(*placeholder_row)  # placeholder values - only the SQL text is kept
                    query = query.on_conflict(*backward_columns, *forward_columns).do_nothing()
                    cached_sql, _ = query.get_parameterized_sql()
                    statements[statement_key] = cached_sql
                (single_forward_key_value,) = unique_forward_key_values
                inserted_count, _ = await BoundWrite.execute_command(
                    connection,
                    WriteStatement(
                        cached_sql,
                        ThroughRows.hide_values(relation, [*single_forward_key_value, *backward_key_values]),
                    ),
                )
                if inserted_count is None:
                    return None, True
                # The pair linked already adds no link.
                return ([instances[0]], True) if inserted_count else ([], False)
            rows = [(*forward_key_value, *backward_key_values) for forward_key_value in unique_forward_key_values]
            inserted_count = await ThroughRowInserting.insert_through_rows(
                connection,
                through_table,
                [*forward_columns, *backward_columns],
                rows,
                conflict_target_columns=[*backward_columns, *forward_columns],
                hides_values=ThroughRows.holds_sensitive_keys(relation),
            )
            # Which of the pairs were linked already isn't returned - all of them or none added names them,
            # some added names none.
            if not inserted_count:
                return [], False
            if inserted_count == len(rows):
                return ThroughRowInserting.get_distinct_instances(instances), True
            return None, True

        criterion = (
            KeyColumns.row_equality(forward_columns, forward_key_values[0])
            if len(forward_key_values) == 1
            else connection.dialect.filter_operators.get_row_membership_criterion(
                forward_columns,
                forward_key_values,
                relation._binding.pk_fields,
                connection=connection,
            )
        )
        select_query = LinkedKeys.get_links_query(
            relation, connection, through_table, backward_key_values=backward_key_values
        ).where(criterion)
        through_binding = relation.field.get_through_binding()

        _, already_existing_relations_raw = await connection._execute_built(
            *ThroughRows.get_statement(relation, select_query, connection)
        )
        # The related model's key fields convert a related instance's values - instances[0], not
        # relation.instance.
        related_pk_fields = related_meta.pk_fields
        types = connection.dialect.types
        existing = KeyColumns.transfer_rows(
            related_pk_fields, relation.field.forward_keys, already_existing_relations_raw, types, types, instances[0]
        )
        read_key = await KeyComparison.get_key_reader(
            connection, related_pk_fields, chain(forward_key_values, existing)
        )
        existing_keys = set(map(read_key, existing))
        links: dict[tuple[Any, ...], tuple[tuple[Any, ...], Any]] = {}
        for instance, db_values in zip(instances, forward_key_values, strict=True):
            key = read_key(db_values)
            if key not in existing_keys:
                links.setdefault(key, (db_values, instance))
        forward_key_values_to_insert = [values for values, _ in links.values()]
        if forward_key_values_to_insert and ThroughRowInserting.restores_soft_deleted_links(relation):
            restored = await ThroughRowInserting.restore_soft_deleted_links(
                relation,
                connection,
                through_table,
                KeyColumns.row_equality(backward_columns, backward_key_values) & criterion,
                instances[0],
                prepared,
            )
            if restored:
                read_key = await KeyComparison.get_key_reader(
                    connection, related_pk_fields, chain(forward_key_values_to_insert, restored)
                )
                restored_keys = set(map(read_key, restored))
                forward_key_values_to_insert = [
                    values for values in forward_key_values_to_insert if read_key(values) not in restored_keys
                ]
        if forward_key_values_to_insert:
            default_fields = ThroughDefaults.get_default_fields(relation, through_defaults)
            default_writers = tuple(connection.dialect.types.get_db_writer(field) for _column, field in default_fields)
            default_columns = [through_table[column_name] for column_name, _ in default_fields]
            key_columns, keys = await ThroughRowInserting.take_through_keys(
                relation, connection, through_table, len(forward_key_values_to_insert), list(extra_columns)
            )
            rows = [
                (
                    *forward_key_value,
                    *backward_key_values,
                    *extra_values,
                    *await ThroughDefaults.through_model_default_values(
                        relation, default_fields, writers=default_writers
                    ),
                    *key,
                )
                for forward_key_value, key in zip(forward_key_values_to_insert, keys, strict=True)
            ]
            inserted_columns = [
                *forward_columns,
                *backward_columns,
                *extra_columns,
                *default_columns,
                *key_columns,
            ]
            column_fields = ThroughRows.get_column_fields(relation, inserted_columns)
            await ThroughRowInserting.insert_through_rows(
                connection,
                through_table,
                inserted_columns,
                rows,
                captured_binding=through_binding,
                hides_values=ThroughRows.holds_sensitive_keys(relation)
                or any(field.sensitive for field in column_fields or ()),
                column_fields=column_fields if connection.features.copies_bulk_inserts else None,
            )
        return [instance for _, instance in links.values()], bool(forward_key_values_to_insert)

    @staticmethod
    def get_distinct_instances(instances: Iterable[Any]) -> list[Any]:
        """The first of the instances of each related row - an instance given twice links one row.

        Args:
            instances: The related instances.

        Returns:
            The instances, each row once.
        """
        distinct_instances: dict[Any, Any] = {}
        for instance in instances:
            distinct_instances.setdefault(instance.pk, instance)
        return list(distinct_instances.values())

    @staticmethod
    async def insert_through_rows(
        connection: DatabaseClient,
        through_table: Table,
        columns: list[SqlField],
        rows: list[tuple[Any, ...]],
        conflict_target_columns: list[SqlField] | None = None,
        captured_binding: ModelBinding | None = None,
        hides_values: bool = False,
        column_fields: list[Field[Any]] | None = None,
    ) -> int | None:
        """Inserts through-table rows in as few multi-row INSERTs as the backend's bind-parameter
        ceiling allows - in one transaction when it takes more than one.

        Args:
            connection: Connection to write on.
            through_table: The through table.
            columns: The inserted columns, in row order.
            rows: One value tuple per inserted row.
            conflict_target_columns: The ``ON CONFLICT DO NOTHING`` target, or None to insert
                without a conflict clause.
            captured_binding: The binding of the ``through=Model`` model - its rows are captured when it declares
                ``Meta.change_capture``.
            hides_values: Whether no parameter is shown - a sensitive value is linked.
            column_fields: The field of each column - given, a database loading bulk inserts through
                ``copy()`` takes the rows in one binary insert when nothing is returned of them.

        Returns:
            How many rows were written - a row ``ON CONFLICT DO NOTHING`` skipped isn't.
        """
        capture_needs = captured_binding.change_capture_needs if captured_binding is not None else None
        if capture_needs is not None and not capture_needs.captures(RowOperation.INSERT):
            capture_needs = None
        if column_fields is not None and conflict_target_columns is None and capture_needs is None:
            parameters = connection.dialect.parameters
            command = WriteCopy(
                through_table.get_table_name(),
                [column.name for column in columns],
                rows,
                [parameters.get_copy_column_type(field) for field in column_fields],
                through_table.get_schema_name(),
            )
            await BoundWrite.execute_command(connection, command)
            return len(rows)
        rows_per_statement = max(
            1, (connection.features.max_bind_parameters - MANY_TO_MANY_WRITE_BIND_PARAMETERS_HEADROOM) // len(columns)
        )
        statements = []
        for start in range(0, len(rows), rows_per_statement):
            # Every row in one call - a call per row copies the rows taken before it.
            query = (
                connection.query_class.into(through_table)
                .columns(*columns)
                .insert(*rows[start : start + rows_per_statement])
            )
            if conflict_target_columns is not None:
                query = query.on_conflict(*conflict_target_columns).do_nothing()
            if capture_needs is not None and connection.features.supports_returning:
                query = query.returning(*capture_needs.columns)
            sql, values = query.get_parameterized_sql()
            statements.append(
                WriteStatement(
                    sql,
                    QueryParameters(values, range(len(values))) if hides_values else values,
                    returns_rows=capture_needs is not None and connection.features.supports_returning,
                )
            )
        bound = BoundWrite(statements)
        written_count: int | None = 0
        if capture_needs is not None:
            captured = CapturedWrite(captured_binding, capture_needs, connection, RowOperation.INSERT)  # type: ignore[arg-type]
            async for result in bound.execute(connection):
                written_count = ThroughRowInserting.get_count_sum(written_count, result.row_count)
                if connection.features.supports_returning:
                    await captured.capture_returned(result.rows)
            if not connection.features.supports_returning:
                await captured.capture_read(
                    {},
                    ThroughRowInserting.get_row_keys(captured_binding, columns, rows, connection.dialect.types),  # type: ignore[arg-type]
                )
            return written_count
        async with (
            connection._in_transaction() if bound.requires_transaction else nullcontext(connection)
        ) as execution_connection:
            async for result in bound.execute(execution_connection):
                written_count = ThroughRowInserting.get_count_sum(written_count, result.row_count)
        return written_count

    @staticmethod
    def get_count_sum(written_count: int | None, count: int | None) -> int | None:
        """The rows statements wrote, with those of one more.

        Args:
            written_count: The rows of the statements before - None where the database reported none.
            count: The rows of the statement.

        Returns:
            The sum - None where the database reported no count of either.
        """
        return None if written_count is None or count is None else written_count + count

    @staticmethod
    def get_row_keys(
        through_binding: ModelBinding, columns: list[SqlField], rows: list[tuple[Any, ...]], types: TypeRegistry
    ) -> list[Any]:
        """The primary keys of through rows written - read back by them on a database returning no rows.

        Args:
            through_binding: The through model binding.
            columns: The rows' columns, in order.
            rows: The rows' values, as the database takes them.
            types: The connection's type registry.

        Returns:
            One key per row.

        Raises:
            UnSupportedError: The rows are written without their keys - the database generates them as it
                writes the rows, and returns none.
        """
        through_model = through_binding._model
        meta = through_binding
        column_names = [column.name for column in columns]
        primary_key_names = meta.primary_key_attribute_names
        key_fields = [meta.fields_map[name] for name in primary_key_names]
        key_columns = [meta.fields_db_projection[name] for name in primary_key_names]
        if not all(column in column_names for column in key_columns):
            raise UnSupportedError(
                f"The links of {through_model.__name__}, which captures its rows (Meta.change_capture), are written "
                "without their keys, which the database returns none of"
            )
        positions = [column_names.index(column) for column in key_columns]
        return [
            values[0] if len(values) == 1 else values
            for values in KeyColumns.read_rows(key_fields, positions, rows, types)
        ]

    @staticmethod
    async def take_through_keys(
        relation: ManyToManyRelation[Any],
        connection: DatabaseClient,
        through_table: Table,
        count: int,
        given_columns: list[SqlField],
    ) -> tuple[list[Any], list[tuple[Any, ...]]]:
        """The keys of new through rows of a through model whose key the database hands out before
        the rows are written (``Features.takes_keys_before_insert``) - as ``create()`` takes them.

        Args:
            relation: The many-to-many relation of an instance.
            connection: The connection the rows are written on.
            through_table: The through table.
            count: How many rows are written.
            given_columns: The columns ``through_defaults`` gives - a key given there is the rows' own, as an
                explicit key of ``create()`` is.

        Returns:
            The key column - none when the rows take no key from here - and each row's key.
        """
        through_binding = relation.field.get_through_binding()
        key_field_name = None if through_binding is None else through_binding.generated_pk_field_name
        if through_binding is None or key_field_name is None or not connection.features.takes_keys_before_insert:
            return [], [()] * count
        key_field = through_binding.fields_map[key_field_name]
        key_column = key_field.source_field or key_field_name
        if any(column.name == key_column for column in given_columns):
            return [], [()] * count
        keys = await GeneratedKeys.take(through_binding, connection, count)
        types = connection.dialect.types
        return [through_table[key_column]], KeyColumns.write_rows((key_field,), ((key,) for key in keys), types, None)

    @staticmethod
    def restores_soft_deleted_links(relation: ManyToManyRelation[Any]) -> bool:
        """Whether ``add()`` restores the soft-deleted through row of a pair instead of inserting
        another - a soft-delete through model declaring the pair unique (with its tenant), which
        keeps a second row of the pair out.

        Args:
            relation: The many-to-many relation of an instance.

        Returns:
            Whether it does.
        """
        through_binding = relation.field.get_through_binding()
        if through_binding is None or through_binding.soft_delete_field is None:
            return False
        reverse_projection = through_binding.fields_db_projection_reverse
        pair_attribute_names = {
            reverse_projection.get(column, column)
            for column in (*relation.field.backward_keys, *relation.field.forward_keys)
        }
        if through_binding.tenant_field:
            pair_attribute_names.update(
                DeclaredUniqueness.get_attribute_names(through_binding, [through_binding.tenant_field])
            )
        return any(
            uniqueness.condition is None and set(uniqueness.attribute_names) <= pair_attribute_names
            for uniqueness in DeclaredUniqueness.get_declared(through_binding)
        )

    @staticmethod
    async def restore_soft_deleted_links(
        relation: ManyToManyRelation[Any],
        connection: DatabaseClient,
        through_table: Table,
        pair_condition: Any,
        related_instance: Model,
        prepared: ThroughValues,
    ) -> set[tuple[Any, ...]]:
        """Restores the soft-deleted through rows of the pairs added, ``through_defaults`` written
        into them as into a new row.

        Args:
            relation: The many-to-many relation of an instance.
            connection: The connection written on.
            through_table: The through table.
            pair_condition: The condition of the through rows of the pairs added.
            related_instance: A related instance - its model's key fields convert the keys.
            prepared: The explicit through assignments already converted for this operation.

        Returns:
            The related keys of the pairs restored, as the database takes them.
        """
        # Local import: the row scopes import the query package.
        from hare.query.scopes.row_scopes import RowScopes
        from hare.query.statements.write.update_query import UpdateQuery

        through_binding: ModelBinding = relation.field.get_through_binding()  # type: ignore[assignment]
        soft_delete_field: str = through_binding.soft_delete_field  # type: ignore[assignment]
        soft_delete_column = through_binding.fields_map[soft_delete_field].source_field or soft_delete_field
        primary_key_names = through_binding.primary_key_attribute_names
        key_fields = [through_binding.fields_map[name] for name in primary_key_names]
        key_columns = [through_binding.fields_db_projection[name] for name in primary_key_names]
        deleted_links_condition = pair_condition & through_table[soft_delete_column].notnull()
        written_defaults = prepared.given_values
        tenant_names: set[str] = set()
        if through_binding.tenant_field:
            # The link of the tenant added is restored - a row of another tenant is another link - and its
            # tenant is the one the row holds already, not written again.
            tenant_names = {
                through_binding.tenant_field,
                *(
                    name
                    for name in through_binding.forward_key_relation_names
                    if list(through_binding.fields_map[name].source_fields) == [through_binding.tenant_field]  # type: ignore[attr-defined]
                ),
            }
            for bound in prepared.assignments.values():
                if bound.field.model_field_name == through_binding.tenant_field:
                    deleted_links_condition &= through_table[
                        through_binding.fields_db_projection[through_binding.tenant_field]
                    ] == (bound.database_value)
            written_defaults = {name: value for name, value in written_defaults.items() if name not in tenant_names}
        deleted_links_query = (
            connection.query_class.from_(through_table)
            .where(deleted_links_condition)
            .select(*dict.fromkeys((*relation.field.forward_keys, *key_columns)))
        )
        _, rows = await connection._execute_built(
            *ThroughRows.get_statement(relation, deleted_links_query, connection)
        )
        if not rows:
            return set()
        types = connection.dialect.types
        related_meta: ModelBinding = related_instance._binding
        related_key_fields = related_meta.pk_fields
        key_readers = tuple(types.get_python_reader(field) for field in key_fields)
        transfer_key = KeyColumns.get_transfer_encoder(related_key_fields, relation.field.forward_keys, types, types)
        keys = []
        restored: set[tuple[Any, ...]] = set()
        for row in rows:
            key = tuple(read(row[column]) for read, column in zip(key_readers, key_columns, strict=True))
            keys.append(key[0] if len(key) == 1 else key)
            restored.add(transfer_key(row, related_instance))
        restored_rows = (
            RowScopes.get_base_queryset(
                through_binding,
                RowVisibility(all_tenants=True, include_deleted=True),
            )
            .using(connection)
            .filter(pk__in=keys)
        )
        update = None
        if written_defaults:
            update = UpdateQuery(
                restored_rows,
                written_defaults,
                bound_assignments={
                    key: value for key, value in prepared.assignments.items() if key[0] not in tenant_names
                },
            )
        await restored_rows.restore()
        if update is not None:
            await update
        return restored
