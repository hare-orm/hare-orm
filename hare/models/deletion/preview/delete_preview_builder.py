from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.enums import OnDelete
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.models.deletion.cascade.cascade_policy import CascadePolicy
from hare.models.deletion.cascade.declarations import ThroughChange
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.prepared_deletion import PreparedDeletion
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.deletion.cascade.tenant_schema_visits import TenantSchemaVisits
from hare.models.deletion.constants import PROTECTING_ON_DELETE_ACTIONS, RESTRICTING_ON_DELETE_ACTIONS
from hare.models.deletion.preview.delete_preview import DeletePreview
from hare.models.deletion.soft_deletion import SoftDeletion
from hare.models.enums import DeletionAction

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class DeletePreviewBuilder:
    """Builds ``Model.delete_preview()``: what ``delete()`` would remove, soft-delete, null out
    or reset, and which rows block it - from the same deletion plan the real delete follows,
    reading only."""

    @staticmethod
    async def build(obj: Model, connection: DatabaseClient | None = None) -> DeletePreview:
        """Computes what ``obj.delete()`` would do, reading only.

        Walks the same CASCADE tree, with the same tenant/soft-delete visibility, as the real
        cascade, then counts what every reached row's ``on_delete`` actions touch.

        Args:
            obj: A persisted obj ``delete()`` would act on.
            connection: Specific DB connection every lookup is scoped to.

        Returns:
            The preview; empty for an obj that is already soft-deleted.
        """
        from hare.models import Model

        if type(obj).delete is not Model.delete:
            return DeletePreview(unknown_actions={type(obj): [obj.pk]})
        if await SoftDeletion.is_already_soft_deleted(obj, connection):
            return DeletePreview()
        preview = await DeletePreviewBuilder.build_in_schema(obj, connection)
        if (
            connection is None
            or connection.tenant_schema_template is None
            or not DeletionGraph.reaches_tenant_schema_models(type(obj))
        ):
            return preview
        # The rows of the other tenants' schemas - read inside a transaction, which visits them.
        async with connection._in_transaction() as transaction_connection:
            for schema_name, tenant in await TenantSchemaVisits.get_other_schemas(transaction_connection):
                async with TenantSchemaVisits.visit(transaction_connection, schema_name, tenant) as scope:
                    schema_preview = await DeletePreviewBuilder.build_in_schema(
                        obj, scope.connection, tenant_rows_only=True
                    )
                preview.add(schema_preview)
        return preview

    @staticmethod
    async def build_in_schema(
        obj: Model, connection: DatabaseClient | None, *, tenant_rows_only: bool = False
    ) -> DeletePreview:
        """What ``obj.delete()`` would do in the schema the connection works in.

        Args:
            obj: A persisted obj, not soft-deleted.
            connection: Specific DB connection every lookup is scoped to.
            tenant_rows_only: Count only the rows of ``Meta.tenant_schema`` models - another tenant's
                schema, its shared rows counted already.

        Returns:
            The preview.
        """
        model = type(obj)
        preview = DeletePreview()
        soft_delete_field = model._meta.soft_delete_field
        plan = await DeletionCollector(
            connection,
            root_action=DeletionAction.SOFT_DELETE if soft_delete_field else DeletionAction.DELETE,
            hands_over_rows=True,
            defers_querysets=False,
        ).collect(model, [obj.pk])
        prepared = await PreparedDeletion.prepare(plan, CascadePolicy(), connection)
        pks_by_model = plan.get_pks_by_model()
        soft_deleted_keys = plan.get_keys(DeletionAction.SOFT_DELETE, DeletionAction.UNCHANGED)
        unchanged_keys = plan.get_keys(DeletionAction.UNCHANGED)
        physically_deleted_keys = plan.get_keys(DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE)
        preview.unknown_actions = plan.get_pks_by_model(
            DeletionAction.OVERRIDDEN_DELETE, DeletionAction.OVERRIDDEN_HARD_DELETE
        )
        for reached_model, reached_pks in pks_by_model.items():
            deleted_count = sum(1 for pk in reached_pks if (reached_model, pk) in physically_deleted_keys)
            soft_deleted_count = sum(
                1
                for pk in reached_pks
                if (reached_model, pk) in soft_deleted_keys and (reached_model, pk) not in unchanged_keys
            )
            if deleted_count:
                preview.deleted[reached_model] = deleted_count
            if soft_deleted_count:
                preview.soft_deleted[reached_model] = soft_deleted_count

        root_key = (model, obj.pk)
        # The obj's own guards ignore the rows its cascade reaches, as delete() checks them
        # (DeletionCollector.check_protected()).
        root_reachable_keys = await DeletionCollector.get_reachable_keys(model, [obj.pk], connection)
        statement_restricting_groups: list[tuple[type[Model], ForeignKeyFieldInstance[Any], list[Model]]] = []
        for reached_model, reached_pks in pks_by_model.items():
            # A hard-deleted descendant's guards ignore rows the same delete physically removes too, a
            # soft-deleted descendant's are checked as-is.
            protect_check_groups: list[tuple[list[Any], frozenset[tuple[type[Model], Any]] | None]] = [
                ([pk for pk in reached_pks if (reached_model, pk) == root_key], root_reachable_keys),
                (
                    [
                        pk
                        for pk in reached_pks
                        if (reached_model, pk) != root_key and (reached_model, pk) in physically_deleted_keys
                    ],
                    physically_deleted_keys,
                ),
                (
                    [
                        pk
                        for pk in reached_pks
                        if (reached_model, pk) != root_key and (reached_model, pk) in soft_deleted_keys
                    ],
                    None,
                ),
            ]
            for group_pks, exclude in protect_check_groups:
                async for protecting_group in DeletionCollector.iterate_guarding_rows(
                    reached_model,
                    group_pks,
                    connection,
                    guarding_actions=PROTECTING_ON_DELETE_ACTIONS,
                    exclude=exclude,
                ):
                    preview.add_blocking_rows(preview.protected_by, protecting_group[1])
            async for __, restricting_rows, guarding_field in DeletionCollector.iterate_guarding_rows(
                reached_model, reached_pks, connection, guarding_actions=RESTRICTING_ON_DELETE_ACTIONS
            ):
                if isinstance(guarding_field, ForeignKeyFieldInstance) and not guarding_field.has_database_constraint:
                    # hare enforces it - a row the same delete removes doesn't block, as the cascade
                    # checks it.
                    preview.add_blocking_rows(
                        preview.restricted_by,
                        [row for row in restricting_rows if (type(row), row.pk) not in physically_deleted_keys],
                    )
                elif isinstance(guarding_field, ForeignKeyFieldInstance) and (
                    CascadePolicy.database_checks_at_statement_end(reached_model, guarding_field, connection)
                ):
                    statement_restricting_groups.append((reached_model, guarding_field, restricting_rows))
                else:
                    preview.add_blocking_rows(preview.restricted_by, restricting_rows)
        await DeletePreviewBuilder.add_restricting_rows_outside_statement(
            preview, statement_restricting_groups, pks_by_model, physically_deleted_keys, connection
        )

        await DeletePreviewBuilder.add_foreign_key_updates(preview, prepared, physically_deleted_keys)
        await DeletePreviewBuilder.add_many_to_many_through_rows(preview, prepared)
        if tenant_rows_only:
            preview.keep_tenant_schema_rows(
                {
                    many_to_many_field.through
                    for reached_model in pks_by_model
                    for many_to_many_field in DeletionGraph.get_many_to_many_fields(reached_model)
                    if DeletionGraph.is_tenant_schema_relation(many_to_many_field)
                }
            )
        return preview

    @staticmethod
    async def add_restricting_rows_outside_statement(
        preview: DeletePreview,
        restricting_groups: list[tuple[type[Model], ForeignKeyFieldInstance[Any], list[Model]]],
        pks_by_model: dict[type[Model], list[Any]],
        physically_deleted_keys: frozenset[tuple[type[Model], Any]],
        connection: DatabaseClient | None,
    ) -> None:
        """Fills ``preview.restricted_by`` with the rows of ``restricting_groups`` that still
        exist once the ``DELETE`` statement removing the row they point at has run.

        Args:
            preview: The preview being filled.
            restricting_groups: ``(guarded model, guarding field, guarding rows)`` for relations
                the database checks at the end of the statement.
            pks_by_model: The rows the delete reaches, by model.
            physically_deleted_keys: ``(type, pk)`` of rows the delete physically removes.
            connection: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
        """
        if not any(
            (type(row), row.pk) in physically_deleted_keys
            for restricting_group in restricting_groups
            for row in restricting_group[2]
        ):
            for restricting_group in restricting_groups:
                preview.add_blocking_rows(preview.restricted_by, restricting_group[2])
            return
        statement_by_key = await PreparedDeletion.group_rows_by_deleting_statement(
            pks_by_model, physically_deleted_keys, connection
        )
        for guarded_model, foreign_key_field, rows in restricting_groups:
            deleted_guarded_pks = [
                pk for pk in pks_by_model.get(guarded_model, []) if (guarded_model, pk) in physically_deleted_keys
            ]
            pk_by_target_value = await RelatedRows.get_pks_by_target_values(
                guarded_model, foreign_key_field, deleted_guarded_pks, connection
            )
            blocking_rows = []
            for row in rows:
                row_key = (type(row), row.pk)
                guarded_pk = pk_by_target_value.get(RelatedRows.get_foreign_key_value(row, foreign_key_field))
                if (
                    row_key in statement_by_key
                    and guarded_pk is not None
                    and PreparedDeletion.find_statement_root(statement_by_key, row_key)
                    == PreparedDeletion.find_statement_root(statement_by_key, (guarded_model, guarded_pk))
                ):
                    continue
                blocking_rows.append(row)
            preview.add_blocking_rows(preview.restricted_by, blocking_rows)

    @staticmethod
    async def add_foreign_key_updates(
        preview: DeletePreview,
        prepared: PreparedDeletion,
        physically_deleted_keys: frozenset[tuple[type[Model], Any]],
    ) -> None:
        """Counts surviving rows selected by the prepared SET_NULL/SET_DEFAULT actions."""
        updated_keys_by_action: dict[OnDelete, set[tuple[type[Model], Any]]] = {
            OnDelete.SET_NULL: set(),
            OnDelete.SET_DEFAULT: set(),
        }
        for wave in prepared.waves:
            for change in wave.foreign_key_changes:
                updated_keys = updated_keys_by_action[change.field.on_delete]
                related_model = change.backward_field.related_model
                key_names = related_model._meta.primary_key_attribute_names
                for queryset in change.querysets:
                    for related_pk in await queryset.values_list(*key_names, flat=len(key_names) == 1):
                        if (related_model, related_pk) not in physically_deleted_keys:
                            updated_keys.add((related_model, related_pk))
        for action, counts in ((OnDelete.SET_NULL, preview.nulled), (OnDelete.SET_DEFAULT, preview.set_default)):
            for related_model, __ in updated_keys_by_action[action]:
                counts[related_model] = counts.get(related_model, 0) + 1

    @staticmethod
    async def add_many_to_many_through_rows(preview: DeletePreview, prepared: PreparedDeletion) -> None:
        """Counts prepared through actions, merging rows reached from both sides."""
        # Per through table and action, how many rows carry each distinct key-column tuple - the
        # same tuple found from two sides is the same rows, so the sides are merged with max().
        row_counts_by_table: dict[OnDelete, dict[str, dict[tuple[Any, ...], int]]] = {
            OnDelete.CASCADE: {},
            OnDelete.SET_NULL: {},
        }
        for wave in prepared.waves:
            for change in wave.through_changes:
                many_to_many_field = change.field
                row_counts_by_action = row_counts_by_table[many_to_many_field.on_delete]
                table_row_counts = row_counts_by_action.setdefault(many_to_many_field.through, {})
                side_row_counts = await DeletePreviewBuilder.count_many_to_many_through_rows(change)
                for key_values, row_count in side_row_counts.items():
                    table_row_counts[key_values] = max(table_row_counts.get(key_values, 0), row_count)
        for table_name, table_row_counts in row_counts_by_table[OnDelete.CASCADE].items():
            if deleted_count := sum(table_row_counts.values()):
                preview.many_to_many_through[table_name] = deleted_count
        for table_name, table_row_counts in row_counts_by_table[OnDelete.SET_NULL].items():
            deleted_row_counts = row_counts_by_table[OnDelete.CASCADE].get(table_name, {})
            if nulled_count := sum(
                row_count for key_values, row_count in table_row_counts.items() if key_values not in deleted_row_counts
            ):
                preview.many_to_many_through_nulled[table_name] = nulled_count

    @staticmethod
    async def count_many_to_many_through_rows(change: ThroughChange) -> dict[tuple[Any, ...], int]:
        """Counts selected through rows by their key-column tuples."""
        many_to_many_field = change.field
        chosen_connection, through_table, criteria = change.connection, change.table, change.criteria
        key_column_names = sorted(set(many_to_many_field.forward_keys) | set(many_to_many_field.backward_keys))
        row_counts: dict[tuple[Any, ...], int] = {}
        for criterion in criteria:
            query = (
                chosen_connection.query_class.from_(through_table)
                .select(*(through_table[column_name] for column_name in key_column_names))
                .where(criterion)
            )
            __, through_rows = await chosen_connection.execute(*query.get_parameterized_sql())
            for through_row in through_rows:
                key_values = tuple(through_row[column_name] for column_name in key_column_names)
                row_counts[key_values] = row_counts.get(key_values, 0) + 1
        return row_counts
