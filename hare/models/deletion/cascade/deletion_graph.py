from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.core.caching.model_cache import ModelCache
from hare.core.connections.model_connection_choice import ModelConnectionChoice
from hare.core.connections.relation_constraints import RelationConstraints
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.deletion.cascade.deletion_scope import DeletionScope

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model
    from hare.models.model_binding import ModelBinding


class DeletionGraph:
    """What the model graph says about deleting a model's rows - the relations pointing at it,
    which of them protect it or cascade onto other models, which the database doesn't enforce.
    Each is a fact of the model graph, computed once."""

    @staticmethod
    @ModelCache.fact()
    def get_backward_relations(
        binding: ModelBinding,
    ) -> list[tuple[BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any], ForeignKeyFieldInstance[Any]]]:
        """Every backward FK/O2O relation of ``model``, each with the forward field on the related
        model pointing back at it.
        """
        relations = []
        backward_fields = [
            binding.fields_map[field_name]
            for field_name in sorted(binding.backward_foreign_key_fields | binding.backward_one_to_one_fields)
        ]
        backward_fields.extend(binding.hidden_backward_relations.values())
        for backward_field in backward_fields:
            shadow_column = backward_field.get_related_binding().fields_map[backward_field.relation_field]  # type: ignore[attr-defined]
            foreign_key_field: ForeignKeyFieldInstance[Any] = shadow_column.reference
            relations.append((backward_field, foreign_key_field))
        return relations  # type: ignore[return-value]

    @staticmethod
    @ModelCache.fact()
    def is_unreferenced(binding: ModelBinding) -> bool:
        """Whether no relation of any model points at ``model`` - deleting its row cascades to
        nothing and nothing protects it.
        """
        return not DeletionGraph.get_backward_relations(binding) and not DeletionGraph.get_many_to_many_fields(binding)

    @staticmethod
    def has_protecting_relations(binding: ModelBinding) -> bool:
        """True if ``model`` itself is guarded by an ``on_delete=PROTECT`` backward FK/O2O or
        M2M relation. A ``through=Model`` M2M is covered by its through model's own FK field,
        already a backward FK relation of ``model``."""
        return any(
            foreign_key_field.on_delete == OnDelete.PROTECT
            for __, foreign_key_field in DeletionGraph.get_backward_relations(binding)
        ) or any(
            many_to_many_field.through_model is None and many_to_many_field.on_delete == OnDelete.PROTECT
            for many_to_many_field in DeletionGraph.get_many_to_many_fields(binding)
        )

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def has_transitive_protect(binding: ModelBinding) -> bool:
        """Whether a model reachable from ``model`` through ``on_delete=CASCADE`` relations is guarded
        by a PROTECT relation - a hard delete then needs ``check_protected_transitively``. ``model``
        itself counts only when a CASCADE cycle leads back to it.
        """
        model = binding._model
        seen: set[type[Model]] = set()
        pending = [model]
        result = False
        while pending and not result:
            current = pending.pop()
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(
                binding.graph.get_binding(current)
            ):
                related_model = backward_field.related_model
                if foreign_key_field.on_delete != OnDelete.CASCADE or related_model in seen:
                    continue
                seen.add(related_model)
                if DeletionGraph.has_protecting_relations(backward_field.get_related_binding()):
                    result = True
                    break
                pending.append(related_model)
        return result

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def has_protect_within_cascade(binding: ModelBinding) -> bool:
        """Whether a hard delete of ``model`` rows can remove a row a database-enforced PROTECT guards
        together with a row guarding it - a guard below it, or one its own cascade reaches; the
        database, checking the constraint after every cascade step, then needs it deferred.
        """
        cascade_models = DeletionGraph.get_cascade_models(binding)
        return DeletionGraph.has_transitive_protect(binding) or any(
            foreign_key_field.get_referencing_model() in cascade_models
            for foreign_key_field in DeletionGraph.get_cascade_protect_foreign_keys(binding)
        )

    @staticmethod
    def get_cascade_models(binding: ModelBinding) -> list[type[Model]]:
        """``model`` plus every model its ``on_delete=CASCADE`` backward FK/O2O relations reach,
        ``db_constraint`` either way - the models a hard delete of ``model`` rows can remove rows of.

        Args:
            binding: The model rows are deleted from.

        Returns:
            The models, each listed once, ``model`` first.
        """
        model = binding._model
        cascade_models = [model]
        pending = [model]
        while pending:
            current = pending.pop()
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(
                binding.graph.get_binding(current)
            ):
                related_model = backward_field.related_model
                if foreign_key_field.on_delete == OnDelete.CASCADE and related_model not in cascade_models:
                    cascade_models.append(related_model)
                    pending.append(related_model)
        return cascade_models

    @staticmethod
    def get_soft_delete_cascade_models(binding: ModelBinding) -> list[tuple[type[Model], bool]]:
        """``model`` plus every model a soft delete of its rows reaches by ``on_delete=CASCADE``, each with
        whether its rows are soft-deleted or deleted for real - as the delete itself walks them: from a
        soft-deleted row a model with ``Meta.soft_delete_field`` is soft-deleted, one without it is kept
        unless the parent's model has ``Meta.soft_delete_hard_cascade``; below a row deleted for real every
        row is deleted for real.

        Args:
            binding: The model rows are soft-deleted from.

        Returns:
            ``(model, soft)`` pairs, each listed once, ``(model, True)`` first.
        """
        model = binding._model
        reached = [(model, True)]
        pending = [(model, True)]
        while pending:
            current_model, is_soft = pending.pop()
            current_binding = binding.graph.get_binding(current_model)
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(current_binding):
                if foreign_key_field.on_delete != OnDelete.CASCADE:
                    continue
                related_model = backward_field.related_model
                if not is_soft:
                    is_related_soft = False
                elif backward_field.get_related_binding().soft_delete_field:
                    is_related_soft = True
                elif current_binding.soft_delete_hard_cascade:
                    is_related_soft = False
                else:
                    continue
                if (related_model, is_related_soft) not in reached:
                    reached.append((related_model, is_related_soft))
                    pending.append((related_model, is_related_soft))
        return reached

    @staticmethod
    def get_cascade_protect_foreign_keys(binding: ModelBinding) -> list[ForeignKeyFieldInstance[Any]]:
        """Every constrained ``on_delete=PROTECT`` FK/O2O field pointing at a model of
        ``get_cascade_models(model)`` - the database backstops a hard delete of ``model`` rows can
        run into.

        Args:
            binding: The model rows are deleted from.

        Returns:
            The fields, each listed once.
        """
        from hare.core.hare_context import HareContext

        protect_fields: list[ForeignKeyFieldInstance[Any]] = []
        for cascade_model in DeletionGraph.get_cascade_models(binding):
            for __, foreign_key_field in DeletionGraph.get_backward_relations(
                binding.graph.get_binding(cascade_model)
            ):
                if (
                    foreign_key_field.on_delete == OnDelete.PROTECT
                    and RelationConstraints.is_enforced(
                        foreign_key_field, HareContext.require_current(), connection=None, source_binding=binding
                    )
                    and foreign_key_field not in protect_fields
                ):
                    protect_fields.append(foreign_key_field)
        return protect_fields

    @staticmethod
    @ModelCache.fact()
    def get_many_to_many_fields(binding: ModelBinding) -> list[ManyToManyFieldInstance[Any]]:
        """Every M2M field touching ``model`` - declared on it, or the backward field of a relation
        declared on the other side.
        """
        return [binding.fields_map[name] for name in sorted(binding.many_to_many_fields)]  # type: ignore[misc]

    @staticmethod
    def has_unconstrained_relations(binding: ModelBinding) -> bool:
        """Whether ``model``, or a model reachable from it through ``on_delete=CASCADE`` relations, has
        a relation the database doesn't enforce - a hard delete then runs ``CascadeDeletion`` for
        it. Whether a database enforces a relation is read from the connection the model writes to
        now, never kept with the model: the same model may be bound to another database later.
        """
        declares_unconstrained_relations, constraint_models = DeletionGraph.get_cascade_constraint_models(binding)
        return declares_unconstrained_relations or not DeletionGraph.enforce_foreign_keys(constraint_models)

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def get_cascade_constraint_models(binding: ModelBinding) -> tuple[bool, tuple[ModelBinding, ...]]:
        """What the relations of ``model`` and of the models its ``on_delete=CASCADE`` relations reach
        declare about their constraints: whether one of them is declared ``db_constraint=False``,
        and the models holding the keys of the others - their databases enforce those only when they
        support foreign keys.
        """
        declares_unconstrained_relations = False
        constraint_models: list[ModelBinding] = []
        for cascade_model in DeletionGraph.get_cascade_models(binding):
            cascade_binding = binding.graph.get_binding(cascade_model)
            relation_fields: list[ForeignKeyFieldInstance[Any] | ManyToManyFieldInstance[Any]] = [
                foreign_key_field for __, foreign_key_field in DeletionGraph.get_backward_relations(cascade_binding)
            ]
            # A `through=Model` relation's real FK field is already among the backward relations -
            # `db_constraint` of the many-to-many field itself isn't read for a through model's table.
            relation_fields.extend(
                many_to_many_field
                for many_to_many_field in DeletionGraph.get_many_to_many_fields(cascade_binding)
                if many_to_many_field.through_model is None
            )
            for relation_field in relation_fields:
                if not relation_field.db_constraint:
                    declares_unconstrained_relations = True
                    continue
                referencing_model = binding.graph.get_binding(relation_field.get_referencing_model())
                if referencing_model not in constraint_models:
                    constraint_models.append(referencing_model)
        return declares_unconstrained_relations, tuple(constraint_models)

    @staticmethod
    def enforce_foreign_keys(bindings: tuple[ModelBinding, ...]) -> bool:
        """Whether the database every model of ``models`` writes to now enforces foreign keys.

        Args:
            bindings: The model bindings to check.

        Returns:
            True when each of them does, or there are none.
        """
        from hare.core.hare_context import HareContext

        context = HareContext.require_current()
        return all(
            ModelConnectionChoice.get_metadata_connection(context, binding, for_write=True).enforces_foreign_keys()
            for binding in bindings
        )

    @staticmethod
    def needs_python_cascade(binding: ModelBinding) -> bool:
        """Whether a hard delete of ``model``'s rows runs ``CascadeDeletion`` - for a relation the
        database doesn't enforce, for the rows of a model with ``Meta.change_capture`` the delete
        removes or changes, which the database's own cascade would change unseen, or for the rows of
        a model overriding ``delete()``, deleted through that override.
        """
        return (
            DeletionGraph.has_unconstrained_relations(binding)
            or DeletionGraph.reaches_captured_models(binding)
            or DeletionGraph.reaches_overridden_deletes(binding)
            or DeletionGraph.has_tenant_schema_work(binding)
        )

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def reaches_tenant_schema_models(binding: ModelBinding) -> bool:
        """Whether deleting rows of ``model``, kept once for every tenant, reaches rows of a
        ``Meta.tenant_schema`` model - pointing at them, or at a row their ``on_delete=CASCADE``
        removes - kept in each tenant's schema: the delete then visits every tenant schema.
        """
        model = binding._model
        if binding.tenant_schema:
            return False
        seen = {model}
        pending = [model]
        while pending:
            current = pending.pop()
            current_binding = binding.graph.get_binding(current)
            if any(
                DeletionGraph.is_tenant_schema_relation(many_to_many_field)
                for many_to_many_field in DeletionGraph.get_many_to_many_fields(current_binding)
            ):
                return True
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(current_binding):
                related_model = backward_field.related_model
                if backward_field.get_related_binding().tenant_schema:
                    return True
                if foreign_key_field.on_delete == OnDelete.CASCADE and related_model not in seen:
                    seen.add(related_model)
                    pending.append(related_model)
        return False

    @staticmethod
    def has_tenant_schema_work(binding: ModelBinding) -> bool:
        """Whether a hard delete of rows of ``model``, kept once for every tenant, has work in the
        tenant schemas beyond the database's own cascade - a ``Meta.tenant_schema`` model it reaches
        with a PROTECT relation, a relation the database doesn't enforce, captured changes or an
        overridden ``delete()``.
        """
        from hare.core.hare_context import HareContext

        model = binding._model
        if not DeletionGraph.reaches_tenant_schema_models(binding):
            return False
        seen = {model}
        pending = [model]
        while pending:
            current = pending.pop()
            current_binding = binding.graph.get_binding(current)
            for many_to_many_field in DeletionGraph.get_many_to_many_fields(current_binding):
                if DeletionGraph.is_tenant_schema_relation(many_to_many_field) and (
                    not many_to_many_field.db_constraint or many_to_many_field.on_delete == OnDelete.PROTECT
                ):
                    return True
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(current_binding):
                related_model = backward_field.related_model
                related_binding = backward_field.get_related_binding()
                if related_binding.tenant_schema and (
                    foreign_key_field.on_delete == OnDelete.PROTECT
                    or not RelationConstraints.is_enforced(
                        foreign_key_field, HareContext.require_current(), connection=None, source_binding=binding
                    )
                    or DeletionGraph.needs_python_cascade(related_binding)
                    or DeletionGraph.overrides_delete(related_binding)
                    or DeletionGraph.has_protecting_relations(related_binding)
                    or DeletionGraph.has_transitive_protect(related_binding)
                ):
                    return True
                if (
                    not related_binding.tenant_schema
                    and foreign_key_field.on_delete == OnDelete.CASCADE
                    and related_model not in seen
                ):
                    seen.add(related_model)
                    pending.append(related_model)
        return False

    @staticmethod
    def is_tenant_schema_relation(many_to_many_field: ManyToManyFieldInstance[Any]) -> bool:
        """Whether a many-to-many relation's table is kept in each tenant's schema - it joins a
        ``Meta.tenant_schema`` model.

        Args:
            many_to_many_field: The relation.

        Returns:
            True when either side has ``Meta.tenant_schema``.
        """
        return bool(
            many_to_many_field.get_binding().model_binding.tenant_schema  # type: ignore[union-attr]
            or many_to_many_field.get_related_binding().tenant_schema
        )

    @staticmethod
    def reaches_table(binding: ModelBinding, connection: DatabaseClient | None, *, source_model: type[Model]) -> bool:
        """Whether a delete on ``connection`` reaches the table of ``model`` - a ``Meta.tenant_schema``
        model's table only from a tenant's schema; the other tenants' schemas are visited apart.

        Args:
            binding: The model.
            connection: The connection the delete runs on; None for the model's own.
            source_model: The root model, used to choose whether this operation shares the cascade's connection.

        Returns:
            False for a ``Meta.tenant_schema`` model while no tenant schema is active.
        """
        from hare.core.hare_context import HareContext

        if not binding.tenant_schema:
            return True
        # Local import: related-row lookups use this graph through the deletion package.
        from hare.models.deletion.cascade.related_rows import RelatedRows

        scope = DeletionScope.current.get()
        client = RelatedRows.get_connection_for(
            binding, connection, source_model=source_model
        ) or ModelConnectionChoice.get_unchecked(HareContext.require_current(), binding, True)
        visited_tables = scope.tables if scope is not None and scope.includes(client) else None
        if visited_tables is not None:
            return binding.db_table in visited_tables
        return client.tenant_schema_template is None or client.tenant_schema is not None

    @staticmethod
    def reaches_relation_table(
        many_to_many_field: ManyToManyFieldInstance[Any],
        connection: DatabaseClient | None,
        *,
        source_model: type[Model],
    ) -> bool:
        """Whether a delete on ``connection`` reaches the table of a many-to-many relation.

        Args:
            many_to_many_field: The relation.
            connection: The connection the delete runs on; None for the models' own.
            source_model: The root model, used to choose whether this operation shares the cascade's connection.

        Returns:
            False for a relation kept in each tenant's schema while no tenant schema is active.
        """
        from hare.core.hare_context import HareContext
        from hare.models.deletion.cascade.related_rows import RelatedRows

        scope = DeletionScope.current.get()
        source_binding = many_to_many_field.get_binding().model_binding
        visited_tables = None
        if scope is not None:
            through_model = (
                many_to_many_field.related_model if many_to_many_field._generated else many_to_many_field.model
            )
            declaring_binding = source_binding.graph.get_binding(through_model)  # type: ignore[union-attr]
            client = RelatedRows.get_connection_for(
                declaring_binding,
                connection,
                source_model=source_model,
            ) or ModelConnectionChoice.get_unchecked(
                HareContext.require_current(),
                declaring_binding,
                True,
            )
            if scope.includes(client):
                visited_tables = scope.tables
        if visited_tables is not None and DeletionGraph.is_tenant_schema_relation(many_to_many_field):
            return many_to_many_field.through in visited_tables
        return DeletionGraph.reaches_table(
            source_binding,  # type: ignore[arg-type]
            connection,
            source_model=source_model,
        ) and DeletionGraph.reaches_table(
            many_to_many_field.get_related_binding(), connection, source_model=source_model
        )

    @staticmethod
    def overrides_delete(binding: ModelBinding) -> bool:
        """Whether ``model`` overrides ``Model.delete()`` or ``Model.hard_delete()``."""
        model = binding._model
        from hare.models import Model

        return model.delete is not Model.delete or model.hard_delete is not Model.hard_delete

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def reaches_overridden_deletes(binding: ModelBinding) -> bool:
        """Whether an ``on_delete=CASCADE`` relation leads from ``model``, directly or further down,
        to a model overriding ``delete()``.
        """
        return any(
            DeletionGraph.overrides_delete(binding.graph.get_binding(cascade_model))
            for cascade_model in DeletionGraph.get_cascade_models(binding)[1:]
        )

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def reaches_captured_models(binding: ModelBinding) -> bool:
        """Whether deleting ``model``'s rows changes rows of a model with ``Meta.change_capture`` -
        its own, those ``on_delete=CASCADE`` reaches, or those ``SET_NULL``/``SET_DEFAULT`` updates.
        """
        for cascade_model in DeletionGraph.get_cascade_models(binding):
            cascade_binding = binding.graph.get_binding(cascade_model)
            if cascade_binding.change_capture_needs is not None:
                return True
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(cascade_binding):
                if (
                    foreign_key_field.on_delete in {OnDelete.SET_NULL, OnDelete.SET_DEFAULT}
                    and backward_field.get_related_binding().change_capture_needs is not None
                ):
                    return True
        return False

    @staticmethod
    def has_self_cascading_constrained_relations(binding: ModelBinding) -> bool:
        """Whether ``model`` is on or reaches a cycle of database-enforced ``CASCADE`` relations - only then
        can the depth of the database's own cascade depend on the data, and reach a dialect's
        cascade depth limit. A relation declared ``db_constraint=True`` joins models of one
        connection, so the cycle is enforced when the database ``model`` writes to now enforces
        foreign keys.
        """
        return DeletionGraph._has_self_cascading_declared_constraints(
            binding, path=[]
        ) and DeletionGraph.enforce_foreign_keys((binding,))

    @staticmethod
    @ModelCache.fact(depends_on_other_models=True)
    def _has_self_cascading_declared_constraints(binding: ModelBinding, path: list[type[Model]]) -> bool:
        """The walk behind ``has_self_cascading_constrained_relations`` over the relations declared
        ``db_constraint=True``, memoized per model. ``path`` is the current depth-first path - a model
        met again on it closes a cycle.
        """
        model = binding._model
        if model in path:
            return True
        path = [*path, model]
        return any(
            foreign_key_field.on_delete == OnDelete.CASCADE
            and foreign_key_field.db_constraint
            and DeletionGraph._has_self_cascading_declared_constraints(backward_field.get_related_binding(), path)
            for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(binding)
        )
