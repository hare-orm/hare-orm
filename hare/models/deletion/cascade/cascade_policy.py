from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.fields.enums import OnDelete
from hare.models.deletion.cascade.declarations import CascadeTransition
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.enums import DeletionAction

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
    from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
    from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
    from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
    from hare.models import Model


@dataclass(frozen=True, slots=True)
class CascadePolicy:
    """Action selection shared by cascade collection, execution and read-only preview.

    The policy performs no row IO and invokes no model overrides. Native subtrees remain
    native unless an unconstrained relation, capture or override needs ORM participation.
    Connection features are read at preparation time, separately from cached model facts.
    """

    only_unconstrained: bool = False
    hands_over_rows: bool = False
    only_toward_protect: bool = False
    tenant_rows_only: bool = False
    defers_querysets: bool = True

    def get_transition(
        self,
        model: type[Model],
        related_model: type[Model],
        field: ForeignKeyFieldInstance[Any],
        parent_action: DeletionAction | None,
        connection: DatabaseClient | None,
    ) -> CascadeTransition | None:
        """Selects a CASCADE edge; None leaves that subtree outside this ORM walk."""
        if field.on_delete != OnDelete.CASCADE or not DeletionGraph.reaches_table(related_model, connection):
            return None
        parent_soft = parent_action in {DeletionAction.SOFT_DELETE, DeletionAction.UNCHANGED}
        parent_hard = parent_action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
        if parent_soft and not related_model._meta.soft_delete_field and not model._meta.soft_delete_hard_cascade:
            return None
        if self.only_toward_protect and not (
            DeletionGraph.has_protecting_relations(related_model)
            or DeletionGraph.has_transitive_protect(related_model)
        ):
            return None
        by_database = (
            parent_hard
            and self.only_unconstrained
            and field.has_database_constraint
            and not (self.hands_over_rows and DeletionGraph.overrides_delete(related_model))
        )
        if by_database and not DeletionGraph.needs_python_cascade(related_model):
            return None
        return CascadeTransition(self.get_action(related_model, parent_action, by_database=by_database), by_database)

    def get_action(
        self,
        model: type[Model],
        parent_action: DeletionAction | None,
        *,
        by_database: bool,
    ) -> DeletionAction | None:
        """Selects the write or deferred override without executing application code."""
        from hare.models import Model

        if parent_action is None:
            return None
        if by_database:
            return DeletionAction.DELETE_BY_DATABASE
        parent_hard = parent_action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
        child_soft = bool(model._meta.soft_delete_field) and not parent_hard
        if self.hands_over_rows and parent_hard and model._meta.soft_delete_field:
            if model.hard_delete is not Model.hard_delete:
                return DeletionAction.OVERRIDDEN_HARD_DELETE
        elif self.hands_over_rows and model.delete is not Model.delete:
            return DeletionAction.OVERRIDDEN_DELETE
        if self.hands_over_rows and self.defers_querysets and child_soft == parent_hard:
            return DeletionAction.QUERYSET_DELETE
        return DeletionAction.SOFT_DELETE if child_soft else DeletionAction.DELETE

    def get_foreign_key_updates(
        self,
        model: type[Model],
        connection: DatabaseClient | None,
    ) -> Iterator[
        tuple[BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any], ForeignKeyFieldInstance[Any]]
    ]:
        """Selects SET_NULL/SET_DEFAULT edges handled by this operation."""
        for backward_field, field in DeletionGraph.get_backward_relations(model):
            related_model = backward_field.related_model
            if (
                field.on_delete not in {OnDelete.SET_NULL, OnDelete.SET_DEFAULT}
                or (self.tenant_rows_only and not related_model._meta.tenant_schema)
                or not DeletionGraph.reaches_table(related_model, connection)
                or (
                    self.only_unconstrained
                    and field.has_database_constraint
                    and related_model._meta.change_capture_needs is None
                )
            ):
                continue
            yield backward_field, field

    def get_through_actions(
        self,
        model: type[Model],
        connection: DatabaseClient | None,
    ) -> Iterator[ManyToManyFieldInstance[Any]]:
        """Selects auto-generated through tables affected by CASCADE or SET_NULL."""
        for field in DeletionGraph.get_many_to_many_fields(model):
            if (
                field.through_model is not None
                or field.on_delete not in {OnDelete.CASCADE, OnDelete.SET_NULL}
                or (self.only_unconstrained and field.has_database_constraint)
                or (self.tenant_rows_only and not DeletionGraph.is_tenant_schema_relation(field))
                or not DeletionGraph.reaches_relation_table(field, connection)
            ):
                continue
            yield field

    @staticmethod
    def database_checks_at_statement_end(
        model: type[Model],
        field: ForeignKeyFieldInstance[Any],
        connection: DatabaseClient | None,
    ) -> bool:
        """Whether a guarding row deleted in the same statement still blocks its target."""
        if not field.has_database_constraint:
            return False
        client = RelatedRows.get_connection_for(model, connection) or model.get_connection(for_write=True)
        if field.on_delete == OnDelete.NO_ACTION:
            return not client.features.checks_foreign_keys_per_cascade_step
        return client.features.checks_restrict_at_statement_end
