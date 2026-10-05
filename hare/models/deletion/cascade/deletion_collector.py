from __future__ import annotations

from collections.abc import AsyncGenerator
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import ProtectedError
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.deletion_plan import DeletionPlan, RowKey
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.deletion.constants import PROTECTING_ON_DELETE_ACTIONS
from hare.models.enums import DeletionAction

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models import Model


class DeletionCollector:
    """The one walk over what a delete reaches: from its root rows through ``on_delete=CASCADE``, one
    batched lookup per relation per wave. A row reached twice is kept once. The ``DeletionPlan`` it
    builds gives every reached row the action ``Model.delete()`` would take for it.

    Args:
        connection: The connection the delete runs on; a model on another connection uses its own.
        root_action: What the delete does to the root rows - ``SOFT_DELETE`` or ``DELETE``; None for
            a walk that only finds the rows a cascade reaches.
        only_unconstrained: Go below a row the database's own cascade removes only where a relation
            the database doesn't enforce follows.
        hands_over_rows: Leave a row whose model overrides ``delete()`` or deletes the other way to
            that model's own delete, without walking below it.
        reads_versions: Read the optimistic lock value of every row to soft-delete.
        only_toward_protect: Follow only the ``CASCADE`` edges leading to a model a PROTECT relation
            guards - the plan is then not the whole tree.
        locks_rows: Lock every row as the walk reads it, the root rows first - no row pointing at a
            reached row can appear before the transaction ends, so the database's own cascade removes
            no row the walk didn't see.
    """

    #: While set, every cascade adds the ``(type, pk)`` of each row it reached (its root rows
    #: included) - lets a multi-row ``QuerySet.delete()`` skip rows an earlier row's cascade
    #: already removed.
    reached_keys_collector: ClassVar[ContextVar[set[tuple[type[Model], Any]] | None]] = ContextVar(
        "hare_cascade_reached_keys_collector", default=None
    )

    def __init__(
        self,
        connection: DatabaseClient | None,
        *,
        root_action: DeletionAction | None,
        only_unconstrained: bool = False,
        hands_over_rows: bool = False,
        reads_versions: bool = False,
        only_toward_protect: bool = False,
        locks_rows: bool = False,
    ) -> None:
        self.connection = connection
        self.locks_rows = locks_rows
        self.root_action = root_action
        self.only_unconstrained = only_unconstrained
        self.hands_over_rows = hands_over_rows
        self.reads_versions = reads_versions
        self.only_toward_protect = only_toward_protect

    async def collect(self, model: type[Model], pks: list[Any]) -> DeletionPlan:
        """Walks the rows deleting the ``model`` rows ``pks`` reaches.

        Args:
            model: The model of the root rows.
            pks: Primary keys of the root rows, each listed once.

        Returns:
            The plan.
        """
        plan = DeletionPlan(model, list(pks))
        plan.waves.append({model: dict.fromkeys(pks, self.root_action)})
        if self.locks_rows:
            await self._lock_root_rows(model, pks)
        elif await self._collect_tree_through_self(plan):
            # One recursive query - it can't lock the rows it reads.
            return plan
        queued: set[RowKey] = {(model, pk) for pk in pks}
        frontier = plan.waves[0]
        while frontier:
            next_wave: dict[type[Model], dict[Any, DeletionAction | None]] = {}
            for current_model, rows in frontier.items():
                pks_by_action: dict[DeletionAction | None, list[Any]] = {}
                for pk, action in rows.items():
                    if action in {DeletionAction.OWN_DELETE, DeletionAction.OWN_DISPATCH}:
                        continue
                    pks_by_action.setdefault(action, []).append(pk)
                for action, action_pks in pks_by_action.items():
                    await self._collect_children(plan, current_model, action, action_pks, queued, next_wave)
            if next_wave:
                plan.waves.append(next_wave)
            frontier = next_wave
        return plan

    async def _lock_root_rows(self, model: type[Model], pks: list[Any]) -> None:
        """Locks the root rows until the transaction ends - a row pointing at one can't be inserted
        meanwhile.

        Args:
            model: The model of the root rows.
            pks: Their primary keys.
        """
        # A walk locks rows on a transaction alone (RelatedRows.locks_rows_of()).
        connection = cast("DatabaseClient", self.connection)
        primary_key_attribute_names = model._meta.primary_key_attribute_names
        for batch in RelatedRows.split_into_batches(pks, connection, len(primary_key_attribute_names)):
            queryset = RelatedRows.include_soft_deleted(
                RelatedRows.get_base_queryset(model).filter(pk__in=batch).using(connection)
            )
            await queryset.select_for_update().values_list(*primary_key_attribute_names)

    async def _collect_tree_through_self(self, plan: DeletionPlan) -> bool:
        """Fills the waves of a plan whose model cascades only onto itself, by a single-column key,
        each level the same way - from one recursive query instead of one per wave. A row is in the
        wave of its depth, as the wave by wave walk puts it.

        Args:
            plan: The plan, its root wave filled.

        Returns:
            Whether the waves were filled - otherwise the plan is walked wave by wave.
        """
        model = plan.root_model
        meta = model._meta
        cascading_relations = [
            relation
            for relation in DeletionGraph.get_backward_relations(model)
            if relation[1].on_delete == OnDelete.CASCADE
        ]
        if self.only_toward_protect or len(cascading_relations) != 1 or len(meta.primary_key_attribute_names) != 1:
            return False
        backward_field, foreign_key_field = cascading_relations[0]
        to_field_names = tuple(to_field.model_field_name for to_field in foreign_key_field.to_field_instances)
        if backward_field.related_model is not model or to_field_names != meta.primary_key_attribute_names:
            return False
        is_walkable, child_action = self._get_self_tree_child_action(model, foreign_key_field, self.root_action)
        if not is_walkable or self._get_self_tree_child_action(model, foreign_key_field, child_action) != (
            True,
            child_action,
        ):
            return False
        reads_state = child_action is DeletionAction.SOFT_DELETE
        reads_version = reads_state and self.reads_versions and bool(meta.optimistic_lock_field)
        value_names = [meta.primary_key_attribute_names[0], backward_field.relation_field]
        if reads_state:
            value_names.append(cast(str, meta.soft_delete_field))
        if reads_version:
            value_names.append(cast(str, meta.optimistic_lock_field))
        rows = await RelatedRows.get_tree_rows(backward_field, plan.root_pks, self.connection, value_names)
        if rows is None:
            return False
        rows_by_parent: dict[Any, list[tuple[Any, ...]]] = {}
        for row in rows:
            rows_by_parent.setdefault(row[1], []).append(row)
        root_pks = set(plan.root_pks)
        queued = set(plan.root_pks)
        frontier = list(plan.root_pks)
        while frontier:
            wave: dict[Any, DeletionAction | None] = {}
            for parent_pk in frontier:
                for row in rows_by_parent.get(parent_pk, ()):
                    pk = row[0]
                    if reads_version:
                        plan.row_versions[(model, pk)] = row[3]
                    if pk in queued:
                        if pk in root_pks:
                            plan.roots_reached_again.add(pk)
                        continue
                    queued.add(pk)
                    wave[pk] = DeletionAction.UNCHANGED if reads_state and row[2] is not None else child_action
            if wave:
                plan.waves.append({model: wave})
            frontier = list(wave)
        return True

    def _get_self_tree_child_action(
        self, model: type[Model], foreign_key_field: ForeignKeyFieldInstance[Any], parent_action: DeletionAction | None
    ) -> tuple[bool, DeletionAction | None]:
        """What the delete does to the rows ``foreign_key_field``, a relation of ``model`` onto itself, leads
        to from rows it does ``parent_action`` to - as ``_collect_children()`` decides it.

        Returns:
            Whether the rows are walked through as rows of the same type, and their action.
        """
        from hare.models import Model

        parent_soft = parent_action in {DeletionAction.SOFT_DELETE, DeletionAction.UNCHANGED}
        parent_hard = parent_action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
        meta = model._meta
        if parent_soft and not meta.soft_delete_field and not meta.soft_delete_hard_cascade:
            return False, None
        if parent_hard and self.only_unconstrained and foreign_key_field.has_database_constraint:
            return False, None
        if parent_action is None:
            return True, None
        child_soft = bool(meta.soft_delete_field) and (not parent_hard or not foreign_key_field.db_constraint)
        if self.hands_over_rows and (model.delete is not Model.delete or child_soft == parent_hard):
            return False, None
        return True, DeletionAction.SOFT_DELETE if child_soft else DeletionAction.DELETE

    async def _collect_children(
        self,
        plan: DeletionPlan,
        model: type[Model],
        parent_action: DeletionAction | None,
        pks: list[Any],
        queued: set[RowKey],
        next_wave: dict[type[Model], dict[Any, DeletionAction | None]],
    ) -> None:
        """Adds to ``next_wave`` the rows an ``on_delete=CASCADE`` relation leads to from the
        ``model`` rows ``pks``, which all get ``parent_action``.

        Args:
            plan: The plan being built.
            model: The model of the rows.
            parent_action: What the delete does to them.
            pks: Their primary keys.
            queued: ``(type, pk)`` of every row reached so far.
            next_wave: The next wave being built.
        """
        parent_soft = parent_action in {DeletionAction.SOFT_DELETE, DeletionAction.UNCHANGED}
        parent_hard = parent_action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
        for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(model):
            if foreign_key_field.on_delete != OnDelete.CASCADE:
                continue
            related_model = backward_field.related_model
            if parent_soft and not related_model._meta.soft_delete_field and not model._meta.soft_delete_hard_cascade:
                # restore() brings a soft-deleted row back: a row that can't be soft-deleted
                # itself is kept, still pointing at it - unless Meta.soft_delete_hard_cascade.
                continue
            if self.only_toward_protect and not (
                DeletionGraph.has_protecting_relations(related_model)
                or DeletionGraph.has_transitive_protect(related_model)
            ):
                continue
            by_database = parent_hard and self.only_unconstrained and foreign_key_field.has_database_constraint
            if by_database and not DeletionGraph.needs_python_cascade(related_model):
                # A purely database-enforced subtree from here on - the database deletes every
                # row down there on its own and backstops its PROTECT/RESTRICT itself too.
                continue
            target_values = await RelatedRows.get_target_values(model, foreign_key_field, pks, self.connection)
            if not target_values:
                continue
            if (
                parent_action is not None
                and not by_database
                and self.hands_over_rows
                and not related_model._meta.has_primary_key
            ):
                # No key to list its rows by - they're deleted by the relation's condition, as
                # the database's own ON DELETE CASCADE would.
                plan.rows_without_key.append((backward_field, target_values))
                continue
            child_action = self._get_child_action(
                related_model, foreign_key_field, parent_action, by_database=by_database
            )
            related_wave = next_wave.setdefault(related_model, {})
            child_rows = await self._get_child_rows(plan, backward_field, target_values, child_action)
            self._add_child_rows(
                plan, related_model, child_rows, child_action, queued, related_wave, parent_soft=parent_soft
            )
            if not related_wave:
                del next_wave[related_model]

    def _get_child_action(
        self,
        related_model: type[Model],
        foreign_key_field: ForeignKeyFieldInstance[Any],
        parent_action: DeletionAction | None,
        *,
        by_database: bool,
    ) -> DeletionAction | None:
        """What the delete does to the rows an ``on_delete=CASCADE`` relation leads to. A hard delete
        through a database-enforced relation removes the row physically, as the database's cascade
        would; any other step deletes the row the way its own model deletes.

        Args:
            related_model: The model of the rows.
            foreign_key_field: The relation.
            parent_action: What the delete does to the rows they point at.
            by_database: Whether the database's own cascade removes them.

        Returns:
            The action.
        """
        from hare.models import Model

        if parent_action is None:
            return None
        if by_database:
            return DeletionAction.DELETE_BY_DATABASE
        parent_hard = parent_action in {DeletionAction.DELETE, DeletionAction.DELETE_BY_DATABASE}
        child_soft = bool(related_model._meta.soft_delete_field) and (
            not parent_hard or not foreign_key_field.db_constraint
        )
        if self.hands_over_rows and related_model.delete is not Model.delete:
            return DeletionAction.OWN_DELETE
        if self.hands_over_rows and child_soft == parent_hard:
            return DeletionAction.OWN_DISPATCH
        return DeletionAction.SOFT_DELETE if child_soft else DeletionAction.DELETE

    @staticmethod
    def _add_child_rows(
        plan: DeletionPlan,
        related_model: type[Model],
        child_rows: list[tuple[Any, bool]],
        child_action: DeletionAction | None,
        queued: set[RowKey],
        related_wave: dict[Any, DeletionAction | None],
        *,
        parent_soft: bool,
    ) -> None:
        """Adds the rows a relation leads to to the next wave - each row once.

        Args:
            plan: The plan being built.
            related_model: The model of the rows.
            child_rows: ``(pk, already soft-deleted)`` per row.
            child_action: What the delete does to them.
            queued: ``(type, pk)`` of every row reached so far.
            related_wave: The next wave's rows of the model.
            parent_soft: Whether the rows they point at are soft-deleted.
        """
        root_pks: set[Any] = set(plan.root_pks) if related_model is plan.root_model else set()
        for related_pk, already_soft_deleted in child_rows:
            key = (related_model, related_pk)
            if key in queued:
                if related_model is plan.root_model and related_pk in root_pks:
                    plan.roots_reached_again.add(related_pk)
                elif (
                    child_action is DeletionAction.DELETE
                    and related_wave.get(related_pk) is DeletionAction.DELETE_BY_DATABASE
                ):
                    # Reached through a database-owned and a hare-owned edge at once - the
                    # database-owned edge alone would not remove it.
                    related_wave[related_pk] = DeletionAction.DELETE
                continue
            action = child_action
            if action is DeletionAction.SOFT_DELETE and already_soft_deleted:
                if not parent_soft:
                    # Its own delete() is a no-op.
                    continue
                action = DeletionAction.UNCHANGED
            queued.add(key)
            related_wave[related_pk] = action

    async def _get_child_rows(
        self,
        plan: DeletionPlan,
        backward_field: BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any],
        target_values: list[Any],
        action: DeletionAction | None,
    ) -> list[tuple[Any, bool]]:
        """The rows ``backward_field`` points from at ``target_values`` - with whether a row to
        soft-delete is soft-deleted already, and its version when the plan reads versions.

        Returns:
            ``(pk, already soft-deleted)`` per row.
        """
        related_model = backward_field.related_model
        if action is not DeletionAction.SOFT_DELETE:
            return [
                (related_pk, False)
                for related_pk in await RelatedRows.get_rows_pointing_at(
                    backward_field, target_values, self.connection, primary_keys_only=True, lock=self.locks_rows
                )
            ]
        meta = related_model._meta
        primary_key_attribute_names = meta.primary_key_attribute_names
        pk_column_count = len(primary_key_attribute_names)
        reads_version = self.reads_versions and bool(meta.optimistic_lock_field)
        value_names = [*primary_key_attribute_names, cast(str, meta.soft_delete_field)]
        if reads_version:
            value_names.append(cast(str, meta.optimistic_lock_field))
        rows: list[tuple[Any, bool]] = []
        for queryset in RelatedRows.get_pointing_querysets(
            backward_field, target_values, self.connection, lock=self.locks_rows
        ):
            for values in await queryset.values_list(*value_names):
                related_pk = values[0] if pk_column_count == 1 else tuple(values[:pk_column_count])
                if reads_version:
                    plan.row_versions[(related_model, related_pk)] = values[pk_column_count + 1]
                rows.append((related_pk, values[pk_column_count] is not None))
        return rows

    @staticmethod
    async def get_reachable_keys(
        model: type[Model], pks: list[Any], connection: DatabaseClient | None
    ) -> frozenset[RowKey]:
        """Every row deleting the ``model`` rows ``pks`` reaches through ``on_delete=CASCADE``, the
        roots included - a guarding row among them goes with the rows it guards.

        Args:
            model: The model of the root rows.
            pks: Their primary keys.
            connection: The connection the delete runs on.

        Returns:
            ``(type, pk)`` of each row.
        """
        return (await DeletionCollector(connection, root_action=None).collect(model, pks)).get_keys()

    @staticmethod
    async def check_protected(model: type[Model], pks: list[Any], connection: DatabaseClient | None = None) -> None:
        """Raises if a ``PROTECT`` relation has rows pointing at any of the ``model`` rows ``pks``
        - one query per ``PROTECT``-guarded relation, not one query per row. A protecting row the same
        delete removes as well doesn't count.

        Args:
            model: The model of the rows about to be deleted.
            pks: Primary keys of the ``model`` rows.
            connection: Specific DB connection to check against, matching whatever connection the
                delete itself is scoped to - a stale/wrong connection could miss a row created
                earlier in the same still-open transaction, or see one that a rolled-back
                transaction on a different connection never actually committed.

        Raises:
            ProtectedError: If a PROTECT-guarded relation has matching rows.
        """
        blocking = await DeletionCollector.find_protecting_rows(model, pks, connection)
        if blocking is not None:
            # A protecting row the same delete removes through another branch of its cascade goes
            # with the rows it protects.
            blocking = await DeletionCollector.find_protecting_rows(
                model, pks, connection, exclude=await DeletionCollector.get_reachable_keys(model, pks, connection)
            )
        if blocking is not None:
            raise ProtectedError(*blocking)

    @staticmethod
    async def find_protecting_rows(
        model: type[Model],
        pks: list[Any],
        connection: DatabaseClient | None,
        *,
        exclude: frozenset[tuple[type[Model], Any]] | None = None,
    ) -> tuple[str, list[Model]] | None:
        """Looks for rows a PROTECT-guarded relation keeps pointing at any of ``pks`` - the shared
        lookup behind ``check_protected`` and ``check_protected_transitively``.

        Args:
            model: The model of the rows about to be deleted.
            pks: Primary keys of the ``model`` rows.
            connection: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
            exclude: ``(type, pk)`` pairs to ignore as protectors, because the same operation
                removes them too.

        Returns:
            ``(message, protecting rows)`` for the first relation that has any, ``None`` if none.
        """
        first_group = await DeletionCollector.find_first_guarding_rows(
            model, pks, connection, guarding_actions=PROTECTING_ON_DELETE_ACTIONS, exclude=exclude
        )
        if first_group is None:
            return None
        message, guarding_rows, __ = first_group
        return message, guarding_rows

    @staticmethod
    async def find_first_guarding_rows(
        model: type[Model],
        pks: list[Any],
        connection: DatabaseClient | None,
        *,
        guarding_actions: frozenset[OnDelete],
        exclude: frozenset[tuple[type[Model], Any]] | None = None,
        only_unconstrained: bool = False,
    ) -> tuple[str, list[Model], ForeignKeyFieldInstance[Any] | ManyToManyFieldInstance[Any]] | None:
        """The first group ``iterate_guarding_rows()`` yields, if any.

        Args:
            model: The model of the rows about to be deleted.
            pks: Primary keys of the ``model`` rows.
            connection: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
            guarding_actions: ``on_delete`` actions to look at.
            exclude: ``(type, pk)`` pairs to ignore, because the same operation removes them too.
            only_unconstrained: Look only at ``db_constraint=False`` relations.

        Returns:
            ``(message, guarding rows, guarding field)``, or ``None`` if no relation has any.
        """
        guarding_row_groups = DeletionCollector.iterate_guarding_rows(
            model,
            pks,
            connection,
            guarding_actions=guarding_actions,
            exclude=exclude,
            only_unconstrained=only_unconstrained,
        )
        try:
            return await anext(guarding_row_groups, None)
        finally:
            await guarding_row_groups.aclose()

    @staticmethod
    async def iterate_guarding_rows(
        model: type[Model],
        pks: list[Any],
        connection: DatabaseClient | None,
        *,
        guarding_actions: frozenset[OnDelete],
        exclude: frozenset[tuple[type[Model], Any]] | None = None,
        only_unconstrained: bool = False,
    ) -> AsyncGenerator[tuple[str, list[Model], ForeignKeyFieldInstance[Any] | ManyToManyFieldInstance[Any]]]:
        """Yields, relation by relation, the rows that keep pointing at any of ``pks`` through a
        backward FK/O2O or auto-through M2M relation whose ``on_delete`` is in
        ``guarding_actions``.

        Args:
            model: The model of the rows about to be deleted.
            pks: Primary keys of the ``model`` rows.
            connection: The connection the delete runs on - every lookup against a model on that
                connection runs on it, inside its transaction; a model on another connection,
                or every model when None, uses its own.
            guarding_actions: ``on_delete`` actions to look at.
            exclude: ``(type, pk)`` pairs to ignore, because the same operation removes them too.
            only_unconstrained: Look only at ``db_constraint=False`` relations.

        Yields:
            ``(message, guarding rows, guarding field)`` for every relation that has any.
        """
        if not pks:
            return
        for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(model):
            if foreign_key_field.on_delete not in guarding_actions or (
                only_unconstrained and foreign_key_field.has_database_constraint
            ):
                continue
            related_model = backward_field.related_model
            target_values = await RelatedRows.get_target_values(model, foreign_key_field, pks, connection)
            if not target_values:
                continue
            matches = await RelatedRows.get_rows_pointing_at(backward_field, target_values, connection)
            if exclude:
                matches = [match for match in matches if (type(match), match.pk) not in exclude]
            if matches:
                yield (
                    f"Cannot delete {model.__name__} rows because "
                    f"{related_model.__name__}.{foreign_key_field.model_field_name} "
                    f"{DeletionCollector.describe_guarding_action(foreign_key_field.on_delete)}",
                    matches,
                    foreign_key_field,
                )
        for many_to_many_field in DeletionGraph.get_many_to_many_fields(model):
            # A through=Model relation's PROTECT is the through model's own FK field - checked
            # above.
            if (
                many_to_many_field.through_model is not None
                or many_to_many_field.on_delete not in guarding_actions
                or (only_unconstrained and many_to_many_field.has_database_constraint)
            ):
                continue
            related_model = many_to_many_field.related_model
            base_query = RelatedRows.get_base_queryset(related_model)
            related_connection = RelatedRows.get_connection_for(related_model, connection)
            # `<related_name>__in=` takes plain pks and composite pk tuples alike, binding a long
            # list as few parameters as the backend allows - batched anyway, for a list whose
            # values have no compact form.
            lookup_connection = related_connection or related_model.get_connection(for_write=False)
            matches_by_pk: dict[Any, Any] = {}
            for batch in RelatedRows.split_into_batches(
                pks, lookup_connection, len(model._meta.primary_key_attribute_names)
            ):
                query = base_query.filter(**{f"{many_to_many_field.related_name}__in": batch}).using(
                    related_connection
                )
                for match in await RelatedRows.include_soft_deleted(query):
                    matches_by_pk.setdefault(match.pk, match)
            matches = list(matches_by_pk.values())
            if exclude:
                matches = [match for match in matches if (type(match), match.pk) not in exclude]
            if matches:
                yield (
                    f"Cannot delete {model.__name__} rows because M2M relation "
                    f"'{many_to_many_field.model_field_name}' "
                    f"{DeletionCollector.describe_guarding_action(many_to_many_field.on_delete)}",
                    matches,
                    many_to_many_field,
                )

    @staticmethod
    def describe_guarding_action(on_delete: OnDelete) -> str:
        """Message tail naming how a relation with ``on_delete`` blocks a delete."""
        if on_delete == OnDelete.PROTECT:
            return "protects them"
        return f"has on_delete={on_delete.name} and rows point at them"

    @staticmethod
    async def check_protected_transitively(
        model: type[Model], pks: list[Any], connection: DatabaseClient | None = None
    ) -> None:
        """Raises when hard-deleting the rows would, through ``CASCADE`` alone, remove a row a PROTECT
        relation still guards. A protecting row the same delete removes never blocks.

        Args:
            model: The model of the rows about to be deleted.
            pks: Their primary keys.
            connection: The connection to check on.

        Raises:
            ProtectedError: A cascade-reachable row is protected by a row outside the cascade.
        """
        if not pks or not DeletionGraph.has_transitive_protect(model):
            return
        plan = await DeletionCollector(connection, root_action=None, only_toward_protect=True).collect(model, pks)
        tree_keys: frozenset[RowKey] | None = None
        for reachable_model, descendant_pks in plan.get_descendant_pks_by_model().items():
            if await DeletionCollector.find_protecting_rows(reachable_model, descendant_pks, connection) is None:
                continue
            if tree_keys is None:
                tree_keys = await DeletionCollector.get_reachable_keys(model, pks, connection)
            blocking = await DeletionCollector.find_protecting_rows(
                reachable_model, descendant_pks, connection, exclude=tree_keys
            )
            if blocking is not None:
                raise ProtectedError(*blocking)
