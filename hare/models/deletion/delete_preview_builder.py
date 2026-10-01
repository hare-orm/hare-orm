from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.fields.enums import OnDelete
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.constants import (
    PROTECTING_ON_DELETE_ACTIONS,
    RESTRICTING_ON_DELETE_ACTIONS,
)
from hare.models.deletion.delete_preview import DeletePreview
from hare.models.deletion.deletion_collector import DeletionCollector
from hare.models.deletion.deletion_graph import DeletionGraph
from hare.models.deletion.related_rows import RelatedRows
from hare.models.enums import DeletionAction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class DeletePreviewBuilder:
    """Builds ``Model.delete_preview()``: what ``delete()`` would remove, soft-delete, null out
    or reset, and which rows block it - from the same deletion plan the real delete follows,
    reading only."""

    @staticmethod
    async def build(instance: Model, db: DatabaseClient | None = None) -> DeletePreview:
        """Computes what ``instance.delete()`` would do, reading only.

        Walks the same CASCADE tree, with the same tenant/soft-delete visibility, as the real
        cascade, then counts what every reached row's ``on_delete`` actions touch.

        Args:
            instance: A persisted instance ``delete()`` would act on.
            db: Specific DB connection every lookup is scoped to.

        Returns:
            The preview; empty for an instance that is already soft-deleted.
        """
        model = type(instance)
        preview = DeletePreview()
        soft_delete_field = model._meta.soft_delete_field
        if await instance._is_already_soft_deleted(db):
            return preview
        plan = await DeletionCollector(
            db, root_action=DeletionAction.SOFT_DELETE if soft_delete_field else DeletionAction.DELETE
        ).collect(model, [instance.pk])
        pks_by_model = plan.get_pks_by_model()
        soft_deleted_keys = plan.get_keys(DeletionAction.SOFT_DELETE, DeletionAction.UNCHANGED)
        unchanged_keys = plan.get_keys(DeletionAction.UNCHANGED)
        physically_deleted_keys = plan.get_keys(DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE)
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

        root_key = (model, instance.pk)
        statement_restricting_groups: list[tuple[type[Model], ForeignKeyFieldInstance[Any], list[Model]]] = []
        for reached_model, reached_pks in pks_by_model.items():
            # The instance's own guards are checked as-is, a hard-deleted descendant's ignore rows
            # the same delete physically removes too, a soft-deleted descendant's are checked as-is.
            protect_check_groups: list[tuple[list[Any], frozenset[tuple[type[Model], Any]] | None]] = [
                ([pk for pk in reached_pks if (reached_model, pk) == root_key], None),
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
                    reached_model, group_pks, db, guarding_actions=PROTECTING_ON_DELETE_ACTIONS, exclude=exclude
                ):
                    preview.add_blocking_rows(preview.protected_by, protecting_group[1])
            async for __, restricting_rows, guarding_field in DeletionCollector.iterate_guarding_rows(
                reached_model, reached_pks, db, guarding_actions=RESTRICTING_ON_DELETE_ACTIONS
            ):
                if isinstance(guarding_field, ForeignKeyFieldInstance) and (
                    DeletePreviewBuilder.database_checks_at_statement_end(reached_model, guarding_field, db)
                ):
                    statement_restricting_groups.append((reached_model, guarding_field, restricting_rows))
                else:
                    preview.add_blocking_rows(preview.restricted_by, restricting_rows)
        await DeletePreviewBuilder.add_restricting_rows_outside_statement(
            preview, statement_restricting_groups, pks_by_model, physically_deleted_keys, db
        )

        await DeletePreviewBuilder.add_fk_updates(preview, pks_by_model, physically_deleted_keys, db)
        # A soft delete keeps the through rows of the rows it soft-deletes, unless their model
        # sets Meta.soft_delete_hard_cascade.
        through_clearing_pks_by_model = {
            reached_model: [
                pk
                for pk in reached_pks
                if (reached_model, pk) in physically_deleted_keys
                or (reached_model._meta.soft_delete_hard_cascade and (reached_model, pk) not in unchanged_keys)
            ]
            for reached_model, reached_pks in pks_by_model.items()
        }
        await DeletePreviewBuilder.add_m2m_through_rows(preview, through_clearing_pks_by_model, db)
        return preview

    @staticmethod
    def database_checks_at_statement_end(
        model: type[Model], fk_field: ForeignKeyFieldInstance[Any], db: DatabaseClient | None
    ) -> bool:
        """Whether the database enforces ``fk_field``'s ``RESTRICT``/``NO_ACTION`` only once the whole
        ``DELETE`` has run - a guarding row the same statement removes then never blocks it.

        Args:
            model: The model ``fk_field`` points at.
            fk_field: A ``RESTRICT``/``NO_ACTION`` FK/O2O field.
            db: The connection the delete runs on; ``model``'s own write connection when None or
                when ``model`` lives elsewhere.
        """
        if not fk_field.has_database_constraint:
            return False
        if fk_field.on_delete == OnDelete.NO_ACTION:
            return True
        model_db = RelatedRows.get_connection_for(model, db) or model.get_connection(for_write=True)
        return model_db.dialect.checks_restrict_at_statement_end

    @staticmethod
    async def add_restricting_rows_outside_statement(
        preview: DeletePreview,
        restricting_groups: list[tuple[type[Model], ForeignKeyFieldInstance[Any], list[Model]]],
        pks_by_model: dict[type[Model], list[Any]],
        physically_deleted_keys: frozenset[tuple[type[Model], Any]],
        db: DatabaseClient | None,
    ) -> None:
        """Fills ``preview.restricted_by`` with the rows of ``restricting_groups`` that still
        exist once the ``DELETE`` statement removing the row they point at has run.

        Args:
            preview: The preview being filled.
            restricting_groups: ``(guarded model, guarding field, guarding rows)`` for relations
                the database checks at the end of the statement.
            pks_by_model: The rows the delete reaches, by model.
            physically_deleted_keys: ``(type, pk)`` of rows the delete physically removes.
            db: The connection the delete runs on - every lookup against a model on that
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
        statement_by_key = await DeletePreviewBuilder.group_rows_by_deleting_statement(
            pks_by_model, physically_deleted_keys, db
        )
        for guarded_model, fk_field, rows in restricting_groups:
            deleted_guarded_pks = [
                pk for pk in pks_by_model.get(guarded_model, []) if (guarded_model, pk) in physically_deleted_keys
            ]
            pk_by_target_value = await RelatedRows.get_pks_by_target_values(
                guarded_model, fk_field, deleted_guarded_pks, db
            )
            blocking_rows = []
            for row in rows:
                row_key = (type(row), row.pk)
                guarded_pk = pk_by_target_value.get(RelatedRows.get_foreign_key_value(row, fk_field))
                if (
                    row_key in statement_by_key
                    and guarded_pk is not None
                    and DeletePreviewBuilder.find_statement_root(statement_by_key, row_key)
                    == DeletePreviewBuilder.find_statement_root(statement_by_key, (guarded_model, guarded_pk))
                ):
                    continue
                blocking_rows.append(row)
            preview.add_blocking_rows(preview.restricted_by, blocking_rows)

    @staticmethod
    async def group_rows_by_deleting_statement(
        pks_by_model: dict[type[Model], list[Any]],
        physically_deleted_keys: frozenset[tuple[type[Model], Any]],
        db: DatabaseClient | None,
    ) -> dict[tuple[type[Model], Any], tuple[type[Model], Any]]:
        """Groups the physically deleted rows by the ``DELETE`` statement removing them: a row the
        database's own cascade removes belongs to its parent's statement.

        Returns:
            A union-find parent map over ``physically_deleted_keys``; read it through
            ``_find_statement_root``.
        """
        statement_by_key = {key: key for key in physically_deleted_keys}
        for parent_model, parent_pks in pks_by_model.items():
            deleted_parent_pks = [pk for pk in parent_pks if (parent_model, pk) in physically_deleted_keys]
            if not deleted_parent_pks:
                continue
            for backward_field, fk_field in DeletionGraph.get_backward_relations(parent_model):
                child_model = backward_field.related_model
                if (
                    fk_field.on_delete != OnDelete.CASCADE
                    or not fk_field.has_database_constraint
                    or child_model not in pks_by_model
                ):
                    continue
                parent_pk_by_target_value = await RelatedRows.get_pks_by_target_values(
                    parent_model, fk_field, deleted_parent_pks, db
                )
                for child in await RelatedRows.get_rows_pointing_at(
                    backward_field, list(parent_pk_by_target_value), db
                ):
                    child_key = (child_model, child.pk)
                    parent_pk = parent_pk_by_target_value.get(RelatedRows.get_foreign_key_value(child, fk_field))
                    if child_key not in statement_by_key or parent_pk is None:
                        continue
                    child_root = DeletePreviewBuilder.find_statement_root(statement_by_key, child_key)
                    parent_root = DeletePreviewBuilder.find_statement_root(statement_by_key, (parent_model, parent_pk))
                    statement_by_key[child_root] = parent_root
        return statement_by_key

    @staticmethod
    def find_statement_root(
        statement_by_key: dict[tuple[type[Model], Any], tuple[type[Model], Any]], key: tuple[type[Model], Any]
    ) -> tuple[type[Model], Any]:
        """The representative row of ``key``'s statement group.

        Args:
            statement_by_key: Built by ``_group_rows_by_deleting_statement``; compressed in place.
            key: A physically deleted row.
        """
        root = key
        while statement_by_key[root] != root:
            root = statement_by_key[root]
        while statement_by_key[key] != root:
            statement_by_key[key], key = root, statement_by_key[key]
        return root

    @staticmethod
    async def add_fk_updates(
        preview: DeletePreview,
        pks_by_model: dict[type[Model], list[Any]],
        physically_deleted_keys: frozenset[tuple[type[Model], Any]],
        db: DatabaseClient | None,
    ) -> None:
        """Fills ``preview.nulled``/``preview.set_default`` with the surviving rows whose
        ``SET_NULL``/``SET_DEFAULT`` FK points at a reached row.

        Args:
            preview: The preview being filled.
            pks_by_model: Every row the delete reaches, grouped by model.
            physically_deleted_keys: ``(type, pk)`` of rows the same delete removes anyway.
            db: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
        """
        updated_keys_by_action: dict[OnDelete, set[tuple[type[Model], Any]]] = {
            OnDelete.SET_NULL: set(),
            OnDelete.SET_DEFAULT: set(),
        }
        for reached_model, reached_pks in pks_by_model.items():
            for backward_field, fk_field in DeletionGraph.get_backward_relations(reached_model):
                updated_keys = updated_keys_by_action.get(fk_field.on_delete)
                if updated_keys is None:
                    continue
                target_values = await RelatedRows.get_target_values(reached_model, fk_field, reached_pks, db)
                if not target_values:
                    continue
                related_model = backward_field.related_model
                for related_pk in await RelatedRows.get_rows_pointing_at(
                    backward_field, target_values, db, primary_keys_only=True
                ):
                    if (related_model, related_pk) not in physically_deleted_keys:
                        updated_keys.add((related_model, related_pk))
        for action, counts in ((OnDelete.SET_NULL, preview.nulled), (OnDelete.SET_DEFAULT, preview.set_default)):
            for related_model, __ in updated_keys_by_action[action]:
                counts[related_model] = counts.get(related_model, 0) + 1

    @staticmethod
    async def add_m2m_through_rows(
        preview: DeletePreview, pks_by_model: dict[type[Model], list[Any]], db: DatabaseClient | None
    ) -> None:
        """Fills the preview with the auto-generated through-table rows an M2M ``CASCADE``/``SET_NULL``
        relation links to a reached row - each counted once, as deleted when any side deletes it.

        Args:
            preview: The preview being filled.
            pks_by_model: Every row the delete reaches, grouped by model.
            db: The connection the delete runs on; a model on another connection uses its own.
        """
        # Per through table and action, how many rows carry each distinct key-column tuple - the
        # same tuple found from two sides is the same rows, so the sides are merged with max().
        row_counts_by_table: dict[OnDelete, dict[str, dict[tuple[Any, ...], int]]] = {
            OnDelete.CASCADE: {},
            OnDelete.SET_NULL: {},
        }
        for reached_model, reached_pks in pks_by_model.items():
            for m2m_field in DeletionGraph.get_m2m_fields(reached_model):
                row_counts_by_action = row_counts_by_table.get(m2m_field.on_delete)
                if m2m_field.through_model is not None or row_counts_by_action is None:
                    continue
                table_row_counts = row_counts_by_action.setdefault(m2m_field.through, {})
                side_row_counts = await DeletePreviewBuilder.count_m2m_through_rows(
                    reached_model, m2m_field, reached_pks, db
                )
                for key_values, row_count in side_row_counts.items():
                    table_row_counts[key_values] = max(table_row_counts.get(key_values, 0), row_count)
        for table_name, table_row_counts in row_counts_by_table[OnDelete.CASCADE].items():
            if deleted_count := sum(table_row_counts.values()):
                preview.m2m_through[table_name] = deleted_count
        for table_name, table_row_counts in row_counts_by_table[OnDelete.SET_NULL].items():
            deleted_row_counts = row_counts_by_table[OnDelete.CASCADE].get(table_name, {})
            if nulled_count := sum(
                row_count for key_values, row_count in table_row_counts.items() if key_values not in deleted_row_counts
            ):
                preview.m2m_through_nulled[table_name] = nulled_count

    @staticmethod
    async def count_m2m_through_rows(
        model: type[Model], m2m_field: ManyToManyFieldInstance[Any], pks: list[Any], db: DatabaseClient | None
    ) -> dict[tuple[Any, ...], int]:
        """Counts the auto-generated through-table rows linking ``m2m_field`` to any ``model``
        row in ``pks``, grouped by the row's key columns (sorted by column name).

        Args:
            model: The model ``m2m_field`` is reached from.
            m2m_field: The M2M relation.
            pks: Primary keys of the ``model`` rows.
            db: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
        """
        chosen_db, through_table, criteria = RelatedRows.get_through_row_criteria(model, m2m_field, pks, db)
        key_column_names = sorted(set(m2m_field.forward_keys) | set(m2m_field.backward_keys))
        row_counts: dict[tuple[Any, ...], int] = {}
        for criterion in criteria:
            query = (
                chosen_db.query_class.from_(through_table)
                .select(*(through_table[column_name] for column_name in key_column_names))
                .where(criterion)
            )
            __, through_rows = await chosen_db.execute(*query.get_parameterized_sql())
            for through_row in through_rows:
                key_values = tuple(through_row[column_name] for column_name in key_column_names)
                row_counts[key_values] = row_counts.get(key_values, 0) + 1
        return row_counts
