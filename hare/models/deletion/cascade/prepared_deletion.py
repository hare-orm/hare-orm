from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.enums import OnDelete
from hare.models.deletion.cascade.declarations import ForeignKeyChange, PreparedDeletionWave, ThroughChange
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.deletion.constants import HANDED_OVER_ACTIONS
from hare.models.enums import DeletionAction

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.deletion.cascade.cascade_policy import CascadePolicy
    from hare.models.deletion.cascade.deletion_plan import DeletionPlan


@dataclass(frozen=True, slots=True)
class PreparedDeletion:
    """A collected graph with FK and through selectors bound before writes begin.

    Preparation reads non-primary target values while they still identify the original
    rows. Execution and preview consume the same selected relationships; neither needs
    to infer actions from the model graph again. Model overrides remain deferred actions
    in the graph and are never called while preparing it.
    """

    graph: DeletionPlan
    waves: tuple[PreparedDeletionWave, ...]

    @classmethod
    async def prepare(
        cls,
        graph: DeletionPlan,
        policy: CascadePolicy,
        connection: DatabaseClient | None,
    ) -> PreparedDeletion:
        """Binds each wave's relationship selectors without persisting any row."""
        waves = []
        for rows_by_model in graph.waves:
            foreign_key_changes = []
            through_changes = []
            for model, rows in rows_by_model.items():
                walked_pks = [pk for pk, action in rows.items() if action not in HANDED_OVER_ACTIONS]
                if not walked_pks:
                    continue
                for backward_field, field in policy.get_foreign_key_updates(model, connection):
                    values = await RelatedRows.get_target_values(model, field, walked_pks, connection)
                    querysets = RelatedRows.get_pointing_querysets(backward_field, values, connection)
                    if querysets:
                        foreign_key_changes.append(ForeignKeyChange(backward_field, field, tuple(querysets)))
                clearing_pks = [
                    pk
                    for pk, action in rows.items()
                    if action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
                    or (action is DeletionAction.SOFT_DELETE and model._meta.soft_delete_hard_cascade)
                ]
                if not clearing_pks:
                    continue
                for many_to_many_field in policy.get_through_actions(model, connection):
                    client, table, criteria = RelatedRows.get_through_row_criteria(
                        model, many_to_many_field, clearing_pks, connection
                    )
                    through_changes.append(ThroughChange(model, many_to_many_field, client, table, tuple(criteria)))
            waves.append(PreparedDeletionWave(rows_by_model, tuple(foreign_key_changes), tuple(through_changes)))
        return cls(graph, tuple(waves))

    @staticmethod
    async def group_rows_by_deleting_statement(
        pks_by_model: dict[type[Model], list[Any]],
        physically_deleted_keys: frozenset[tuple[type[Model], Any]],
        connection: DatabaseClient | None,
    ) -> dict[tuple[type[Model], Any], tuple[type[Model], Any]]:
        """Groups the physically deleted rows by the ``DELETE`` statement removing them: a row the
        database's own cascade removes belongs to its parent's statement.

        Returns:
            A union-find parent map over ``physically_deleted_keys``; read it through
            ``find_statement_root()``.
        """
        statement_by_key = {key: key for key in physically_deleted_keys}
        for parent_model, parent_pks in pks_by_model.items():
            deleted_parent_pks = [pk for pk in parent_pks if (parent_model, pk) in physically_deleted_keys]
            if not deleted_parent_pks:
                continue
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(parent_model):
                child_model = backward_field.related_model
                if (
                    foreign_key_field.on_delete != OnDelete.CASCADE
                    or not foreign_key_field.has_database_constraint
                    or child_model not in pks_by_model
                ):
                    continue
                parent_pk_by_target_value = await RelatedRows.get_pks_by_target_values(
                    parent_model, foreign_key_field, deleted_parent_pks, connection
                )
                for child in await RelatedRows.get_rows_pointing_at(
                    backward_field, list(parent_pk_by_target_value), connection
                ):
                    child_key = (child_model, child.pk)
                    parent_pk = parent_pk_by_target_value.get(
                        RelatedRows.get_foreign_key_value(child, foreign_key_field)
                    )
                    if child_key not in statement_by_key or parent_pk is None:
                        continue
                    child_root = PreparedDeletion.find_statement_root(statement_by_key, child_key)
                    parent_root = PreparedDeletion.find_statement_root(statement_by_key, (parent_model, parent_pk))
                    statement_by_key[child_root] = parent_root
        return statement_by_key

    @staticmethod
    def find_statement_root(
        statement_by_key: dict[tuple[type[Model], Any], tuple[type[Model], Any]], key: tuple[type[Model], Any]
    ) -> tuple[type[Model], Any]:
        """The representative row of ``key``'s statement group.

        Args:
            statement_by_key: Built by ``group_rows_by_deleting_statement()``; compressed in place.
            key: A physically deleted row.
        """
        root = key
        while statement_by_key[root] != root:
            root = statement_by_key[root]
        while statement_by_key[key] != root:
            statement_by_key[key], key = root, statement_by_key[key]
        return root
