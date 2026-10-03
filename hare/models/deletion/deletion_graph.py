from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.core.model_cache import ModelCache
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class DeletionGraph:
    """What the model graph says about deleting a model's rows - the relations pointing at it,
    which of them protect it or cascade onto other models, which the database doesn't enforce.
    Each is a fact of the model graph, computed once."""

    @staticmethod
    @ModelCache.fact()
    def get_backward_relations(
        model: type[Model],
    ) -> list[tuple[BackwardFKRelation[Any] | BackwardOneToOneRelation[Any], ForeignKeyFieldInstance[Any]]]:
        """Every backward FK/O2O relation of ``model``, each with the forward field on the related
        model pointing back at it.
        """
        relations = []
        backward_fields = [
            cast("BackwardFKRelation[Any] | BackwardOneToOneRelation[Any]", model._meta.fields_map[field_name])
            for field_name in sorted(model._meta.backward_fk_fields | model._meta.backward_o2o_fields)
        ]
        backward_fields.extend(model._meta.hidden_backward_relations.values())
        for backward_field in backward_fields:
            shadow_column = backward_field.related_model._meta.fields_map[backward_field.relation_field]
            fk_field = cast("ForeignKeyFieldInstance[Any]", shadow_column.reference)
            relations.append((backward_field, fk_field))
        return relations

    @staticmethod
    @ModelCache.fact()
    def is_unreferenced(model: type[Model]) -> bool:
        """Whether no relation of any model points at ``model`` - deleting its row cascades to
        nothing and nothing protects it.
        """
        return not DeletionGraph.get_backward_relations(model) and not DeletionGraph.get_m2m_fields(model)

    @staticmethod
    def has_protecting_relations(model: type[Model]) -> bool:
        """True if ``model`` itself is guarded by an ``on_delete=PROTECT`` backward FK/O2O or
        M2M relation. A ``through=Model`` M2M is covered by its through model's own FK field,
        already a backward FK relation of ``model``."""
        return any(
            fk_field.on_delete == OnDelete.PROTECT for __, fk_field in DeletionGraph.get_backward_relations(model)
        ) or any(
            m2m_field.through_model is None and m2m_field.on_delete == OnDelete.PROTECT
            for m2m_field in DeletionGraph.get_m2m_fields(model)
        )

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def has_transitive_protect(model: type[Model]) -> bool:
        """Whether a model reachable from ``model`` through ``on_delete=CASCADE`` relations is guarded
        by a PROTECT relation - a hard delete then needs ``check_protected_transitively``. ``model``
        itself counts only when a CASCADE cycle leads back to it.
        """
        seen: set[type[Model]] = set()
        pending = [model]
        result = False
        while pending and not result:
            current = pending.pop()
            for backward_field, fk_field in DeletionGraph.get_backward_relations(current):
                related_model = backward_field.related_model
                if fk_field.on_delete != OnDelete.CASCADE or related_model in seen:
                    continue
                seen.add(related_model)
                if DeletionGraph.has_protecting_relations(related_model):
                    result = True
                    break
                pending.append(related_model)
        return result

    @staticmethod
    def get_cascade_models(model: type[Model]) -> list[type[Model]]:
        """``model`` plus every model its ``on_delete=CASCADE`` backward FK/O2O relations reach,
        ``db_constraint`` either way - the models a hard delete of ``model`` rows can remove rows of.

        Args:
            model: The model rows are deleted from.

        Returns:
            The models, each listed once, ``model`` first.
        """
        cascade_models = [model]
        pending = [model]
        while pending:
            current = pending.pop()
            for backward_field, fk_field in DeletionGraph.get_backward_relations(current):
                related_model = backward_field.related_model
                if fk_field.on_delete == OnDelete.CASCADE and related_model not in cascade_models:
                    cascade_models.append(related_model)
                    pending.append(related_model)
        return cascade_models

    @staticmethod
    def get_cascade_protect_foreign_keys(model: type[Model]) -> list[ForeignKeyFieldInstance[Any]]:
        """Every constrained ``on_delete=PROTECT`` FK/O2O field pointing at a model of
        ``get_cascade_models(model)`` - the database backstops a hard delete of ``model`` rows can
        run into.

        Args:
            model: The model rows are deleted from.

        Returns:
            The fields, each listed once.
        """
        protect_fields: list[ForeignKeyFieldInstance[Any]] = []
        for cascade_model in DeletionGraph.get_cascade_models(model):
            for __, fk_field in DeletionGraph.get_backward_relations(cascade_model):
                if (
                    fk_field.on_delete == OnDelete.PROTECT
                    and fk_field.has_database_constraint
                    and fk_field not in protect_fields
                ):
                    protect_fields.append(fk_field)
        return protect_fields

    @staticmethod
    @ModelCache.fact()
    def get_m2m_fields(model: type[Model]) -> list[ManyToManyFieldInstance[Any]]:
        """Every M2M field touching ``model`` - declared on it, or the backward field of a relation
        declared on the other side.
        """
        return [
            cast("ManyToManyFieldInstance[Any]", model._meta.fields_map[name])
            for name in sorted(model._meta.m2m_fields)
        ]

    @staticmethod
    def has_unconstrained_relations(model: type[Model]) -> bool:
        """Whether ``model``, or a model reachable from it through ``on_delete=CASCADE`` relations, has
        a relation the database doesn't enforce - a hard delete then runs ``CascadeDeletion`` for
        it.
        """
        return DeletionGraph._has_unconstrained_relations_transitively(model, visiting=set())

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def _has_unconstrained_relations_transitively(model: type[Model], visiting: set[type[Model]]) -> bool:
        """The walk behind ``has_unconstrained_relations``, memoized per model. A model met again on
        the walk (``visiting``) adds nothing new.
        """
        if model in visiting:
            return False
        visiting.add(model)
        relations = DeletionGraph.get_backward_relations(model)
        # A `through=Model` relation's real FK field is already covered by the backward-relations
        # check above - `m2m_field.db_constraint` itself isn't read by schema generation for a
        # through model's own table, so it carries no independent signal here.
        result = (
            any(not fk_field.has_database_constraint for __, fk_field in relations)
            or any(
                not m2m_field.has_database_constraint
                for m2m_field in DeletionGraph.get_m2m_fields(model)
                if m2m_field.through_model is None
            )
            or any(
                fk_field.on_delete == OnDelete.CASCADE
                and DeletionGraph._has_unconstrained_relations_transitively(backward_field.related_model, visiting)
                for backward_field, fk_field in relations
            )
        )
        return result

    @staticmethod
    def has_self_cascading_constrained_relations(model: type[Model]) -> bool:
        """Whether ``model`` is part of a cycle of database-enforced ``CASCADE`` relations - only then
        can the depth of the database's own cascade depend on the data, and reach a dialect's
        cascade depth limit.
        """
        return DeletionGraph._has_self_cascading_constrained_relations(model, path=[])

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def _has_self_cascading_constrained_relations(model: type[Model], path: list[type[Model]]) -> bool:
        """The walk behind ``has_self_cascading_constrained_relations``, memoized per model. ``path``
        is the current depth-first path - a model met again on it closes a cycle.
        """
        if model in path:
            return True
        path = [*path, model]
        result = any(
            fk_field.on_delete == OnDelete.CASCADE
            and fk_field.has_database_constraint
            and DeletionGraph._has_self_cascading_constrained_relations(backward_field.related_model, path)
            for backward_field, fk_field in DeletionGraph.get_backward_relations(model)
        )
        return result
