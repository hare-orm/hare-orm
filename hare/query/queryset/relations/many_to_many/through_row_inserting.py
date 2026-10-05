from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.core.connections.connections import Connections
from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import UnSupportedError
from hare.fields.constants import MANY_TO_MANY_WRITE_BIND_PARAMETERS_HEADROOM
from hare.fields.field import Field
from hare.instrumentation.capture.captured_write import CapturedWrite
from hare.instrumentation.enums import RowOperation
from hare.models.uniqueness.declared_uniqueness import DeclaredUniqueness
from hare.models.write.generated_keys import GeneratedKeys
from hare.models.write.rules.relation_key_values import RelationKeyValues
from hare.query.key_columns import KeyColumns
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.relations.many_to_many.many_to_many_members import ManyToManyMembers
from hare.query.queryset.relations.many_to_many.many_to_many_scope_checks import ManyToManyScopeChecks
from hare.query.queryset.relations.many_to_many.through_defaults import ThroughDefaults
from hare.query.queryset.relations.many_to_many.through_rows import ThroughRows
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql.builder.tables.table import Table
from hare.sql.terms.parameters.query_parameters import QueryParameters

TModel = TypeVar("TModel", bound="Model")

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.fields.relations.fields.relational_field import RelationalField
    from hare.models import Model
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
    from hare.sql.terms.field import Field as SqlField


class ThroughRowInserting:
    """The rows a many-to-many relation's ``add()`` inserts into its through table - the links not there yet,
    soft-deleted ones restored - in as few statements as the database takes."""

    @staticmethod
    async def add_links(
        relation: ManyToManyRelation[Any],
        instances: tuple[Any, ...],
        through_defaults: dict[str, Any] | None,
        using: str | DatabaseClient | None,
    ) -> tuple[list[Any], bool]:
        """Everything ``add()`` does but reporting the change.

        Args:
            relation: The many-to-many relation of an instance.
            instances: Related instances or their primary key values.
            through_defaults: See ``add()``.
            using: See ``add()``.

        Returns:
            The related instances linked - none when every pair was linked already - and whether through rows
            were inserted: a soft-deleted link restored is an update of its row, reported by the restore.
        """
        ManyToManyMembers.validate_owner_saved(relation)
        using_client = Connections.get_client(using)
        instances = await ManyToManyMembers.get_related_instances(
            relation,
            instances,
            None if using_client is None else ManyToManyMembers.get_connection_of(relation.model, using_client),
        )
        ManyToManyMembers.validate_related_instances(relation, instances)
        connection = Connections.get_client(using) or ThroughRows.through_table_connection(relation, for_write=True)
        if WrittenConnections.is_recording:
            WrittenConnections.record(connection.connection_alias)
        relation._reset_cache_on_rollback(connection)
        backward_key_values = KeyColumns.get_db_values(
            type(relation.instance)._meta, relation.instance, connection.dialect.types
        )
        related_meta = type(instances[0])._meta
        await ManyToManyScopeChecks.check_tenant_scope(relation, instances, connection)
        await ManyToManyScopeChecks.check_visible_scope(relation, instances, connection)
        # The fetched rows are dropped up front - every exit below then leaves no stale snapshot.
        relation._invalidate_local_cache()
        forward_key_values: list[tuple[Any, ...]] = [
            KeyColumns.get_db_values(related_meta, instance_to_add, connection.dialect.types)
            for instance_to_add in instances
        ]
        through_table = Table(relation.field.through, schema=relation.field.through_schema)
        backward_columns = [through_table[column] for column in relation.field.backward_keys]
        forward_columns = [through_table[column] for column in relation.field.forward_keys]
        given_through_defaults = through_defaults
        through_defaults = ThroughDefaults.through_defaults_with_active_tenant(relation, through_defaults)
        extra_columns, extra_values = ThroughDefaults.through_defaults_columns_and_values(
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
            unique_forward_key_values = set(forward_key_values)
            if len(unique_forward_key_values) == 1:
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
                (single_forward_key_value,) = unique_forward_key_values
                inserted_count, _ = await connection.execute(
                    cached_sql, ThroughRows.hide_values(relation, [*single_forward_key_value, *backward_key_values])
                )
                # The pair linked already adds no link.
                return (list(instances), True) if inserted_count else ([], False)
            rows = [(*forward_key_value, *backward_key_values) for forward_key_value in unique_forward_key_values]
            inserted_count = await ThroughRowInserting.insert_through_rows(
                connection,
                through_table,
                [*forward_columns, *backward_columns],
                rows,
                conflict_target_columns=[*backward_columns, *forward_columns],
                hides_values=ThroughRows.holds_sensitive_keys(relation),
            )
            # Which of the pairs were linked already isn't returned - none added, none linked.
            return (list(instances), True) if inserted_count else ([], False)

        select_query = connection.query_class.from_(through_table).where(
            KeyColumns.row_equality(backward_columns, backward_key_values)
        )
        select_query = select_query.select(*relation.field.forward_keys)
        criterion = (
            KeyColumns.row_equality(forward_columns, forward_key_values[0])
            if len(forward_key_values) == 1
            else connection.dialect.filter_operators.get_row_membership_criterion(
                forward_columns, forward_key_values, ManyToManyMembers.get_forward_key_fields(relation)
            )
        )
        select_query = select_query.where(criterion)
        through_model = relation.field.through_model_class
        soft_delete_field = through_model._meta.soft_delete_field if through_model is not None else None
        if through_model is not None and soft_delete_field is not None:
            # A soft-deleted through row is no active link - the pair is added again.
            soft_delete_column = through_model._meta.fields_map[soft_delete_field].source_field or soft_delete_field
            select_query = select_query.where(through_table[soft_delete_column].isnull())

        _, already_existing_relations_raw = await connection.execute(
            *ThroughRows.get_statement(relation, select_query)
        )
        # The related model's key fields convert a related instance's values - instances[0], not
        # relation.instance.
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

        forward_key_values_to_insert = set(forward_key_values) - already_existing_forward_pks
        # The pairs linked by the call - inserted, or restored from a soft delete.
        linked_forward_key_values = set(forward_key_values_to_insert)
        if forward_key_values_to_insert and ThroughRowInserting.restores_soft_deleted_links(relation):
            forward_key_values_to_insert -= await ThroughRowInserting.restore_soft_deleted_links(
                relation,
                connection,
                through_table,
                KeyColumns.row_equality(backward_columns, backward_key_values) & criterion,
                instances[0],
                given_through_defaults,
            )
        if forward_key_values_to_insert:
            ThroughDefaults.raise_if_required_fields_missing(relation, through_defaults)
            default_fields = ThroughDefaults.through_model_default_fields(relation, through_defaults)
            default_columns = [through_table[column_name] for column_name, _ in default_fields]
            key_columns, keys = await ThroughRowInserting.take_through_keys(
                relation, connection, through_table, len(forward_key_values_to_insert), extra_columns
            )
            rows = [
                (
                    *forward_key_value,
                    *backward_key_values,
                    *extra_values,
                    *await ThroughDefaults.through_model_default_values(
                        relation, default_fields, connection.dialect.types
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
            await ThroughRowInserting.insert_through_rows(
                connection,
                through_table,
                inserted_columns,
                rows,
                captured_model=through_model,
                hides_values=ThroughRows.holds_sensitive_values(relation, through_defaults),
                column_fields=(
                    ThroughRows.get_column_fields(relation, inserted_columns)
                    if connection.features.copies_bulk_inserts
                    else None
                ),
            )
        linked = [
            instance
            for instance, forward_key_value in zip(instances, forward_key_values, strict=True)
            if forward_key_value in linked_forward_key_values
        ]
        return linked, bool(forward_key_values_to_insert)

    @staticmethod
    async def insert_through_rows(
        connection: DatabaseClient,
        through_table: Table,
        columns: list[SqlField],
        rows: list[tuple[Any, ...]],
        conflict_target_columns: list[SqlField] | None = None,
        captured_model: type[Model] | None = None,
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
            captured_model: The ``through=Model`` model - its rows are captured when it declares
                ``Meta.change_capture``.
            hides_values: Whether no parameter is shown - a sensitive value is linked.
            column_fields: The field of each column - given, a database loading bulk inserts through
                ``copy()`` takes the rows in one binary insert when nothing is returned of them.

        Returns:
            How many rows were written - a row ``ON CONFLICT DO NOTHING`` skipped isn't.
        """
        capture_needs = captured_model._meta.change_capture_needs if captured_model is not None else None
        if capture_needs is not None and not capture_needs.captures(RowOperation.INSERT):
            capture_needs = None
        if column_fields is not None and conflict_target_columns is None and capture_needs is None:
            parameters = connection.dialect.parameters
            await connection.copy(
                through_table.get_table_name(),
                [column.name for column in columns],
                rows,
                [parameters.get_copy_column_type(field) for field in column_fields],
                through_table.get_schema_name(),
            )
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
            statements.append((sql, QueryParameters(values, range(len(values))) if hides_values else values))
        if capture_needs is not None:
            captured_model = cast("type[Model]", captured_model)
            captured = CapturedWrite(captured_model, capture_needs, connection, RowOperation.INSERT)
            if not connection.features.supports_returning:
                # The rows written are read by their keys - the database returns none of them.
                written_count: int | None = 0
                for sql, values in statements:
                    written_count = ThroughRowInserting.get_count_sum(
                        written_count, (await connection.execute(sql, values, returns_rows=False))[0]
                    )
                await captured.capture_read(
                    {}, ThroughRowInserting.get_row_keys(captured_model, columns, rows, connection.dialect.types)
                )
                return written_count
            written_count = 0
            for sql, values in statements:
                count, returned_rows = await connection.execute(sql, values, returns_rows=True)
                written_count = ThroughRowInserting.get_count_sum(written_count, count)
                await captured.capture_returned(returned_rows)
            return written_count
        if len(statements) == 1:
            return (await connection.execute(*statements[0]))[0]
        written_count = 0
        async with connection._in_transaction() as transaction_connection:
            for sql, values in statements:
                written_count = ThroughRowInserting.get_count_sum(
                    written_count, (await transaction_connection.execute(sql, values))[0]
                )
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
        through_model: type[Model], columns: list[SqlField], rows: list[tuple[Any, ...]], types: TypeRegistry
    ) -> list[Any]:
        """The primary keys of through rows written - read back by them on a database returning no rows.

        Args:
            through_model: The through model.
            columns: The rows' columns, in order.
            rows: The rows' values, as the database takes them.
            types: The connection's type registry.

        Returns:
            One key per row.

        Raises:
            UnSupportedError: The rows are written without their keys - the database generates them as it
                writes the rows, and returns none.
        """
        meta = through_model._meta
        column_names = [column.name for column in columns]
        key_fields = [meta.fields_map[name] for name in meta.primary_key_attribute_names]
        key_columns = [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names]
        if not all(column in column_names for column in key_columns):
            raise UnSupportedError(
                f"The links of {through_model.__name__}, which captures its rows (Meta.change_capture), are written "
                "without their keys, which the database returns none of"
            )
        positions = [column_names.index(column) for column in key_columns]
        keys = []
        for row in rows:
            values = [
                types.get_python_value(field, row[position])
                for field, position in zip(key_fields, positions, strict=True)
            ]
            keys.append(values[0] if len(values) == 1 else tuple(values))
        return keys

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
        through_model = relation.field.through_model_class
        key_field_name = None if through_model is None else through_model._meta.generated_pk_field_name
        if through_model is None or key_field_name is None or not connection.features.takes_keys_before_insert:
            return [], [()] * count
        key_field = through_model._meta.fields_map[key_field_name]
        key_column = key_field.source_field or key_field_name
        if any(column.name == key_column for column in given_columns):
            return [], [()] * count
        keys = await GeneratedKeys.take(through_model, connection, count)
        types = connection.dialect.types
        return [through_table[key_column]], [(types.get_db_value(key_field, key, None),) for key in keys]

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
        through_model = relation.field.through_model_class
        if through_model is None or through_model._meta.soft_delete_field is None:
            return False
        meta = through_model._meta
        reverse_projection = meta.fields_db_projection_reverse
        pair_attribute_names = {
            reverse_projection.get(column, column)
            for column in (*relation.field.backward_keys, *relation.field.forward_keys)
        }
        if meta.tenant_field:
            pair_attribute_names.update(DeclaredUniqueness.get_attribute_names(through_model, [meta.tenant_field]))
        return any(
            uniqueness.condition is None and set(uniqueness.attribute_names) <= pair_attribute_names
            for uniqueness in DeclaredUniqueness.get_declared(through_model)
        )

    @staticmethod
    async def restore_soft_deleted_links(
        relation: ManyToManyRelation[Any],
        connection: DatabaseClient,
        through_table: Table,
        pair_condition: Any,
        related_instance: Model,
        through_defaults: dict[str, Any] | None,
    ) -> set[tuple[Any, ...]]:
        """Restores the soft-deleted through rows of the pairs added, ``through_defaults`` written
        into them as into a new row.

        Args:
            relation: The many-to-many relation of an instance.
            connection: The connection written on.
            through_table: The through table.
            pair_condition: The condition of the through rows of the pairs added.
            related_instance: A related instance - its model's key fields convert the keys.
            through_defaults: The values the caller gave for the through model's fields.

        Returns:
            The related keys of the pairs restored, as the database takes them.
        """
        # Local import: the row scopes import the query package.
        from hare.query.scopes.row_scopes import RowScopes

        through_model = cast("type[Model]", relation.field.through_model_class)
        meta = through_model._meta
        soft_delete_field = cast("str", meta.soft_delete_field)
        soft_delete_column = meta.fields_map[soft_delete_field].source_field or soft_delete_field
        key_fields = [meta.fields_map[name] for name in meta.primary_key_attribute_names]
        key_columns = [meta.fields_db_projection[name] for name in meta.primary_key_attribute_names]
        deleted_links_condition = pair_condition & through_table[soft_delete_column].notnull()
        written_defaults = dict(through_defaults or {})
        if meta.tenant_field:
            # The link of the tenant added is restored - a row of another tenant is another link - and its
            # tenant is the one the row holds already, not written again.
            tenant_names = {
                meta.tenant_field,
                *(
                    name
                    for name in meta.foreign_key_fields | meta.one_to_one_fields
                    if list(cast("RelationalField[Any]", meta.fields_map[name]).source_fields) == [meta.tenant_field]
                ),
            }
            tenant_values = ThroughDefaults.through_defaults_with_active_tenant(relation, through_defaults) or {}
            for name in tenant_names & tenant_values.keys():
                tenant_value = tenant_values[name]
                if name != meta.tenant_field:
                    tenant_value = RelationKeyValues.get_key_values(through_model, name, tenant_value)[0]
                deleted_links_condition &= through_table[meta.fields_db_projection[meta.tenant_field]] == (
                    connection.dialect.types.get_db_value(meta.fields_map[meta.tenant_field], tenant_value, None)
                )
            written_defaults = {name: value for name, value in written_defaults.items() if name not in tenant_names}
        deleted_links_query = (
            connection.query_class.from_(through_table)
            .where(deleted_links_condition)
            .select(*dict.fromkeys((*relation.field.forward_keys, *key_columns)))
        )
        _, rows = await connection.execute(*ThroughRows.get_statement(relation, deleted_links_query))
        if not rows:
            return set()
        types = connection.dialect.types
        related_meta = type(related_instance)._meta
        related_key_fields = related_meta.pk_fields if related_meta.has_composite_primary_key else (related_meta.pk,)
        keys = []
        restored: set[tuple[Any, ...]] = set()
        for row in rows:
            key = tuple(
                types.get_python_value(field, row[column])
                for field, column in zip(key_fields, key_columns, strict=True)
            )
            keys.append(key[0] if len(key) == 1 else key)
            restored.add(
                tuple(
                    types.get_db_value(field, types.get_python_value(field, row[column]), related_instance)
                    for field, column in zip(related_key_fields, relation.field.forward_keys, strict=True)
                )
            )
        restored_rows = (
            RowScopes.get_base_queryset(through_model, RowVisibility(all_tenants=True, include_deleted=True))
            .using(connection)
            .filter(pk__in=keys)
        )
        await restored_rows.restore()
        if written_defaults:
            await restored_rows.update(**written_defaults)
        return restored
