from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from hare.core.caching.model_cache import ModelCache
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
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
    ) -> list[tuple[BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any], ForeignKeyFieldInstance[Any]]]:
        """Every backward FK/O2O relation of ``model``, each with the forward field on the related
        model pointing back at it.
        """
        relations = []
        backward_fields = [
            cast("BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any]", model._meta.fields_map[field_name])
            for field_name in sorted(model._meta.backward_foreign_key_fields | model._meta.backward_one_to_one_fields)
        ]
        backward_fields.extend(model._meta.hidden_backward_relations.values())
        for backward_field in backward_fields:
            shadow_column = backward_field.related_model._meta.fields_map[backward_field.relation_field]
            foreign_key_field = cast("ForeignKeyFieldInstance[Any]", shadow_column.reference)
            relations.append((backward_field, foreign_key_field))
        return relations

    @staticmethod
    @ModelCache.fact()
    def is_unreferenced(model: type[Model]) -> bool:
        """Whether no relation of any model points at ``model`` - deleting its row cascades to
        nothing and nothing protects it.
        """
        return not DeletionGraph.get_backward_relations(model) and not DeletionGraph.get_many_to_many_fields(model)

    @staticmethod
    def has_protecting_relations(model: type[Model]) -> bool:
        """True if ``model`` itself is guarded by an ``on_delete=PROTECT`` backward FK/O2O or
        M2M relation. A ``through=Model`` M2M is covered by its through model's own FK field,
        already a backward FK relation of ``model``."""
        return any(
            foreign_key_field.on_delete == OnDelete.PROTECT
            for __, foreign_key_field in DeletionGraph.get_backward_relations(model)
        ) or any(
            many_to_many_field.through_model is None and many_to_many_field.on_delete == OnDelete.PROTECT
            for many_to_many_field in DeletionGraph.get_many_to_many_fields(model)
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
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(current):
                related_model = backward_field.related_model
                if foreign_key_field.on_delete != OnDelete.CASCADE or related_model in seen:
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
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(current):
                related_model = backward_field.related_model
                if foreign_key_field.on_delete == OnDelete.CASCADE and related_model not in cascade_models:
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
            for __, foreign_key_field in DeletionGraph.get_backward_relations(cascade_model):
                if (
                    foreign_key_field.on_delete == OnDelete.PROTECT
                    and foreign_key_field.has_database_constraint
                    and foreign_key_field not in protect_fields
                ):
                    protect_fields.append(foreign_key_field)
        return protect_fields

    @staticmethod
    @ModelCache.fact()
    def get_many_to_many_fields(model: type[Model]) -> list[ManyToManyFieldInstance[Any]]:
        """Every M2M field touching ``model`` - declared on it, or the backward field of a relation
        declared on the other side.
        """
        return [
            cast("ManyToManyFieldInstance[Any]", model._meta.fields_map[name])
            for name in sorted(model._meta.many_to_many_fields)
        ]

    @staticmethod
    def has_unconstrained_relations(model: type[Model]) -> bool:
        """Whether ``model``, or a model reachable from it through ``on_delete=CASCADE`` relations, has
        a relation the database doesn't enforce - a hard delete then runs ``CascadeDeletion`` for
        it. Whether a database enforces a relation is read from the connection the model writes to
        now, never kept with the model: the same model may be bound to another database later.
        """
        declares_unconstrained_relations, constraint_owners = DeletionGraph.get_cascade_constraint_owners(model)
        return declares_unconstrained_relations or not DeletionGraph.enforce_foreign_keys(constraint_owners)

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def get_cascade_constraint_owners(model: type[Model]) -> tuple[bool, tuple[type[Model], ...]]:
        """What the relations of ``model`` and of the models its ``on_delete=CASCADE`` relations reach
        declare about their constraints: whether one of them is declared ``db_constraint=False``,
        and the models holding the keys of the others - their databases enforce those only when they
        support foreign keys.
        """
        declares_unconstrained_relations = False
        constraint_owners: list[type[Model]] = []
        for cascade_model in DeletionGraph.get_cascade_models(model):
            relation_fields: list[ForeignKeyFieldInstance[Any] | ManyToManyFieldInstance[Any]] = [
                foreign_key_field for __, foreign_key_field in DeletionGraph.get_backward_relations(cascade_model)
            ]
            # A `through=Model` relation's real FK field is already among the backward relations -
            # `db_constraint` of the many-to-many field itself isn't read for a through model's table.
            relation_fields.extend(
                many_to_many_field
                for many_to_many_field in DeletionGraph.get_many_to_many_fields(cascade_model)
                if many_to_many_field.through_model is None
            )
            for relation_field in relation_fields:
                if not relation_field.db_constraint:
                    declares_unconstrained_relations = True
                    continue
                referencing_model = relation_field.get_referencing_model()
                if referencing_model not in constraint_owners:
                    constraint_owners.append(referencing_model)
        return declares_unconstrained_relations, tuple(constraint_owners)

    @staticmethod
    def enforce_foreign_keys(models: tuple[type[Model], ...]) -> bool:
        """Whether the database every model of ``models`` writes to now enforces foreign keys.

        Args:
            models: The models.

        Returns:
            True when each of them does, or there are none.
        """
        return all(model.get_connection(for_write=True).dialect.features.supports_foreign_keys for model in models)

    @staticmethod
    def needs_python_cascade(model: type[Model]) -> bool:
        """Whether a hard delete of ``model``'s rows runs ``CascadeDeletion`` - for a relation the
        database doesn't enforce, or for the rows of a model with ``Meta.change_capture`` the delete
        removes or changes, which the database's own cascade would change unseen.
        """
        return DeletionGraph.has_unconstrained_relations(model) or DeletionGraph.reaches_captured_models(model)

    @staticmethod
    def reaches_captured_models(model: type[Model]) -> bool:
        """Whether deleting ``model``'s rows changes rows of a model with ``Meta.change_capture`` -
        its own, those ``on_delete=CASCADE`` reaches, or those ``SET_NULL``/``SET_DEFAULT`` updates.
        """
        return DeletionGraph._reaches_captured_models(model, visiting=set())

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def _reaches_captured_models(model: type[Model], visiting: set[type[Model]]) -> bool:
        """The walk behind ``reaches_captured_models``, memoized per model. A model met again on the
        walk (``visiting``) adds nothing new.
        """
        if model in visiting:
            return False
        visiting.add(model)
        if model._meta.change_capture_needs is not None:
            return True
        for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(model):
            related_model = backward_field.related_model
            if foreign_key_field.on_delete in {OnDelete.SET_NULL, OnDelete.SET_DEFAULT}:
                if related_model._meta.change_capture_needs is not None:
                    return True
            elif foreign_key_field.on_delete == OnDelete.CASCADE and DeletionGraph._reaches_captured_models(
                related_model, visiting
            ):
                return True
        return False

    @staticmethod
    def has_self_cascading_constrained_relations(model: type[Model]) -> bool:
        """Whether ``model`` is part of a cycle of database-enforced ``CASCADE`` relations - only then
        can the depth of the database's own cascade depend on the data, and reach a dialect's
        cascade depth limit. A relation declared ``db_constraint=True`` joins models of one
        connection, so the cycle is enforced when the database ``model`` writes to now enforces
        foreign keys.
        """
        return DeletionGraph._has_self_cascading_declared_constraints(
            model, path=[]
        ) and DeletionGraph.enforce_foreign_keys((model,))

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def _has_self_cascading_declared_constraints(model: type[Model], path: list[type[Model]]) -> bool:
        """The walk behind ``has_self_cascading_constrained_relations`` over the relations declared
        ``db_constraint=True``, memoized per model. ``path`` is the current depth-first path - a model
        met again on it closes a cycle.
        """
        if model in path:
            return True
        path = [*path, model]
        return any(
            foreign_key_field.on_delete == OnDelete.CASCADE
            and foreign_key_field.db_constraint
            and DeletionGraph._has_self_cascading_declared_constraints(backward_field.related_model, path)
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(model)
        )
