from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.core.connections.relation_constraints import RelationConstraints
from hare.core.hare_context import HareContext
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
    from hare.models.model_binding import ModelBinding


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
        binding: ModelBinding,
        related_model: type[Model],
        field: ForeignKeyFieldInstance[Any],
        parent_action: DeletionAction | None,
        connection: DatabaseClient | None,
        *,
        source_model: type[Model],
    ) -> CascadeTransition | None:
        """Selects a CASCADE edge; None leaves that subtree outside this ORM walk."""
        if field.on_delete != OnDelete.CASCADE:
            return None
        related_binding = binding.graph.get_binding(related_model)
        if not DeletionGraph.reaches_table(related_binding, connection, source_model=source_model):
            return None
        parent_soft = parent_action in {DeletionAction.SOFT_DELETE, DeletionAction.UNCHANGED}
        parent_hard = parent_action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
        if parent_soft and not related_binding.soft_delete_field and not binding.soft_delete_hard_cascade:
            return None
        if self.only_toward_protect and not (
            DeletionGraph.has_protecting_relations(related_binding)
            or DeletionGraph.has_transitive_protect(related_binding)
        ):
            return None
        by_database = (
            parent_hard
            and self.only_unconstrained
            and RelationConstraints.is_enforced(
                field,
                HareContext.require_current(),
                connection=connection,
                source_binding=binding.graph.get_binding(source_model),
            )
            and not (self.hands_over_rows and DeletionGraph.overrides_delete(related_binding))
        )
        if by_database and not DeletionGraph.needs_python_cascade(related_binding):
            return None
        return CascadeTransition(
            self.get_action(related_binding, parent_action, by_database=by_database),
            by_database,
        )

    def get_action(
        self,
        binding: ModelBinding,
        parent_action: DeletionAction | None,
        *,
        by_database: bool,
    ) -> DeletionAction | None:
        """Selects the write or deferred override without executing application code."""
        model = binding._model
        from hare.models import Model

        if parent_action is None:
            return None
        if by_database:
            return DeletionAction.DELETE_BY_DATABASE
        parent_hard = parent_action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
        child_soft = bool(binding.soft_delete_field) and not parent_hard
        if self.hands_over_rows and parent_hard and binding.soft_delete_field:
            if model.hard_delete is not Model.hard_delete:
                return DeletionAction.OVERRIDDEN_HARD_DELETE
        elif self.hands_over_rows and model.delete is not Model.delete:
            return DeletionAction.OVERRIDDEN_DELETE
        if self.hands_over_rows and self.defers_querysets and child_soft == parent_hard:
            return DeletionAction.QUERYSET_DELETE
        return DeletionAction.SOFT_DELETE if child_soft else DeletionAction.DELETE

    def get_foreign_key_updates(
        self,
        binding: ModelBinding,
        connection: DatabaseClient | None,
        *,
        source_model: type[Model],
    ) -> Iterator[
        tuple[BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any], ForeignKeyFieldInstance[Any]]
    ]:
        """Selects SET_NULL/SET_DEFAULT edges handled by this operation."""
        for backward_field, field in DeletionGraph.get_backward_relations(binding):
            if field.on_delete not in {OnDelete.SET_NULL, OnDelete.SET_DEFAULT}:
                continue
            related_binding = backward_field.get_related_binding()
            if (
                (self.tenant_rows_only and not related_binding.tenant_schema)
                or not DeletionGraph.reaches_table(related_binding, connection, source_model=source_model)
                or (
                    self.only_unconstrained
                    and RelationConstraints.is_enforced(
                        field,
                        HareContext.require_current(),
                        connection=connection,
                        source_binding=binding.graph.get_binding(source_model),
                    )
                    and related_binding.change_capture_needs is None
                )
            ):
                continue
            yield backward_field, field

    def get_through_actions(
        self,
        binding: ModelBinding,
        connection: DatabaseClient | None,
        *,
        source_model: type[Model],
    ) -> Iterator[ManyToManyFieldInstance[Any]]:
        """Selects auto-generated through tables affected by CASCADE or SET_NULL."""
        for field in DeletionGraph.get_many_to_many_fields(binding):
            if (
                field.through_model is not None
                or field.on_delete not in {OnDelete.CASCADE, OnDelete.SET_NULL}
                or (
                    self.only_unconstrained
                    and RelationConstraints.is_enforced(
                        field,
                        HareContext.require_current(),
                        connection=connection,
                        source_binding=binding.graph.get_binding(source_model),
                    )
                )
                or (self.tenant_rows_only and not DeletionGraph.is_tenant_schema_relation(field))
                or not DeletionGraph.reaches_relation_table(field, connection, source_model=source_model)
            ):
                continue
            yield field

    @staticmethod
    def database_checks_at_statement_end(
        binding: ModelBinding,
        field: ForeignKeyFieldInstance[Any],
        connection: DatabaseClient | None,
        *,
        source_model: type[Model],
    ) -> bool:
        """Whether a guarding row deleted in the same statement still blocks its target."""
        if not RelationConstraints.is_enforced(
            field,
            HareContext.require_current(),
            connection=connection,
            source_binding=binding.graph.get_binding(source_model),
        ):
            return False
        client = RelatedRows.get_connection_for(
            binding, connection, source_model=source_model
        ) or ModelConnectionChoice.get(HareContext.require_current(), binding, True)
        if field.on_delete == OnDelete.NO_ACTION:
            return not client.features.checks_foreign_keys_per_cascade_step
        return client.features.checks_restrict_at_statement_end
