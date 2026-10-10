from __future__ import annotations

import datetime
from contextlib import nullcontext
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.core.routing.written_connections import WrittenConnections
from hare.exceptions import ConfigurationError, IntegrityError, ProtectedError, StaleObjectError, UnSupportedError
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.instrumentation.capture.change_capturing import ChangeCapturing
from hare.instrumentation.enums import RowOperation
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from hare.models.deletion.cascade.deletion_plan import DeletionPlan, RowKey
from hare.models.deletion.cascade.related_rows import RelatedRows
from hare.models.deletion.constants import HANDED_OVER_ACTIONS, RESTRICTING_ON_DELETE_ACTIONS
from hare.models.enums import DeletionAction
from hare.models.tenancy.tenancy import Tenancy
from hare.query.expressions import RawSQL
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
    from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation


class CascadeDeletion:
    """Carries out the ``on_delete`` cascade below root rows of one model: collects the plan, checks
    PROTECT/RESTRICT, then writes it - one statement per relation and batch. Every write shares one
    deletion time; the caller runs the cascade in one transaction.

    Args:
        model: The model of the root rows.
        connection: The connection of the cascade; a model on another connection uses its own.
        only_unconstrained: Leave the relations the database enforces to the database.
        persist_as_hard_delete: Really delete the reached rows instead of soft-deleting them.
        bottom_up_persist: Delete the reached rows deepest wave first.
        deleted_at: The deletion time written to every soft-deleted row.
        root_instance: The instance whose ``delete()`` runs this cascade - its many-to-many caches
            are reset.
    """

    #: The deletion time of the cascade running in this context - a nested delete it triggers
    #: (a dispatch switch, an overridden ``delete()``) soft-deletes with the same time.
    active_deleted_at: ClassVar[ContextVar[datetime.datetime | None]] = ContextVar(
        "hare_cascade_active_deleted_at", default=None
    )

    def __init__(
        self,
        model: type[Model],
        connection: DatabaseClient | None,
        *,
        only_unconstrained: bool,
        persist_as_hard_delete: bool,
        bottom_up_persist: bool = False,
        deleted_at: datetime.datetime | None = None,
        root_instance: Model | None = None,
    ) -> None:
        self.model = model
        self.connection = connection
        self.only_unconstrained = only_unconstrained
        self.persist_as_hard_delete = persist_as_hard_delete
        self.bottom_up_persist = bottom_up_persist
        self.deleted_at = deleted_at or CascadeDeletion.get_deleted_at()
        self.root_instance = root_instance
        self.verb = "delete" if persist_as_hard_delete else "soft-delete"
        #: The ``Meta.optimistic_lock_field`` value each soft-deleted row was read with.
        self.row_versions: dict[RowKey, Any] = {}
        #: Every row the cascade reaches from the roots - read once a guarding row turns up.
        self.reachable_keys: frozenset[RowKey] | None = None

    @staticmethod
    def get_deleted_at() -> datetime.datetime:
        """The deletion time of the cascade running in this context, or now."""
        return CascadeDeletion.active_deleted_at.get() or Timezone.now()

    @staticmethod
    async def run_below(
        obj: Model,
        connection: DatabaseClient | None,
        *,
        only_unconstrained: bool,
        persist_as_hard_delete: bool,
        bottom_up_persist: bool = False,
        deleted_at: datetime.datetime | None = None,
    ) -> bool:
        """Carries out the cascade below one obj being deleted - its own row is the caller's.

        Args:
            obj: The row being deleted.
            connection: The connection the delete runs on.
            only_unconstrained: See the class.
            persist_as_hard_delete: See the class.
            bottom_up_persist: See the class.
            deleted_at: See the class.

        Returns:
            Whether a cascade cycle led back to ``obj`` - the database may already have removed
            its row.

        Raises:
            ProtectedError: A PROTECT relation guards a row the cascade reaches.
            IntegrityError: ``RESTRICT``/``NO_ACTION`` rows point at a row the cascade reaches.
        """
        cascade_deletion = CascadeDeletion(
            type(obj),
            connection,
            only_unconstrained=only_unconstrained,
            persist_as_hard_delete=persist_as_hard_delete,
            bottom_up_persist=bottom_up_persist,
            deleted_at=deleted_at,
            root_instance=obj,
        )
        return bool(await cascade_deletion.run([obj.pk]))

    async def run(self, root_pks: list[Any], *, persist_roots: bool = False) -> set[Any]:
        """Applies every ``on_delete`` action the deletion of the root rows triggers. The roots' own
        PROTECT guards are the caller's to check.

        Args:
            root_pks: Primary keys of the root rows, each listed once.
            persist_roots: Also soft-delete or delete the root rows themselves, last.

        Returns:
            The root pks a cascade cycle reached again - the database may already have removed those
            rows.

        Raises:
            ProtectedError: A PROTECT relation guards a row the cascade reaches.
            IntegrityError: ``RESTRICT``/``NO_ACTION`` rows point at a reached row, or a row to
                soft-delete no longer exists.
            StaleObjectError: A row to soft-delete changed after the cascade read it.
        """
        token = CascadeDeletion.active_deleted_at.set(self.deleted_at)
        try:
            plan = await self._run(root_pks, persist_roots=persist_roots)
        finally:
            CascadeDeletion.active_deleted_at.reset(token)
        if (reached_keys := DeletionCollector.reached_keys_collector.get()) is not None:
            reached_keys.update(plan.get_keys())
        return plan.roots_reached_again

    async def _run(self, root_pks: list[Any], *, persist_roots: bool) -> DeletionPlan:
        """The collection, checks and writes behind ``run()``."""
        plan = await DeletionCollector(
            self.connection,
            root_action=DeletionAction.DELETE if self.persist_as_hard_delete else DeletionAction.SOFT_DELETE,
            only_unconstrained=self.only_unconstrained,
            hands_over_rows=True,
            reads_versions=not self.persist_as_hard_delete,
            # A captured row the database's own cascade removes is captured from the walk's rows.
            locks_rows=DeletionGraph.reaches_captured_models(self.model)
            and RelatedRows.locks_rows_of(self.connection),
        ).collect(self.model, root_pks)
        self._check_row_writes(plan, persist_roots=persist_roots)
        self.row_versions = plan.row_versions
        if persist_roots and not self.persist_as_hard_delete and self.model._meta.optimistic_lock_field:
            await self._read_root_versions(root_pks)
        await self._check_guarding_rows(plan)
        await self._capture_database_deletes(plan)
        # A tree of one model's rows that nothing else points at: every wave's rows only change
        # themselves, so each type of write is made for the whole tree at once.
        writes_tree_at_once = self._is_tree_of_one_model(plan)
        tree_soft_deleted_pks: list[Any] = []
        hard_deleted_waves: list[dict[type[Model], list[Any]]] = []
        for wave_index, wave in enumerate(plan.waves):
            for model, rows in wave.items():
                walked_pks = [pk for pk, action in rows.items() if action not in HANDED_OVER_ACTIONS]
                if walked_pks:
                    await self._update_pointing_foreign_keys(model, walked_pks)
                    await self._clear_many_to_many_through_rows(model, walked_pks)
            if wave_index > 0:
                hard_deleted_waves.append(
                    await self._write_wave(wave, tree_soft_deleted_pks if writes_tree_at_once else None)
                )
        await self._persist_soft_deletes(self.model, tree_soft_deleted_pks)
        if writes_tree_at_once:
            hard_deleted_waves = self._merge_tree_waves(hard_deleted_waves)
        for backward_field, target_values in plan.rows_without_key:
            # No key to list them by - deleted by the relation's own condition, as the database's
            # own ON DELETE CASCADE would.
            for queryset in RelatedRows.get_pointing_querysets(backward_field, target_values, self.connection):
                await queryset.delete()
        # A hard delete writes nothing before the whole tree is known: the database's cascade of an
        # earlier row would remove rows whose unconstrained relations the walk never found.
        for hard_deleted in reversed(hard_deleted_waves) if self.bottom_up_persist else hard_deleted_waves:
            for model, pks in hard_deleted.items():
                await self._persist_hard_deletes(model, pks)
        if persist_roots:
            if self.persist_as_hard_delete:
                await self._persist_hard_deletes(self.model, root_pks, model_connection=self.connection)
            else:
                await self._persist_soft_deletes(self.model, root_pks, model_connection=self.connection)
        return plan

    async def _write_wave(
        self, wave: dict[type[Model], dict[Any, DeletionAction | None]], tree_soft_deleted_pks: list[Any] | None
    ) -> dict[type[Model], list[Any]]:
        """Writes the rows of one wave of the cascade a model's own code or a soft delete removes -
        the hard deletes wait for the whole tree.

        Args:
            wave: The action on each row, by model.
            tree_soft_deleted_pks: Where the soft-deleted rows of a tree written at once are
                collected, None to soft-delete each model's rows now.

        Returns:
            The rows to hard-delete, by model.
        """
        hard_deleted: dict[type[Model], list[Any]] = {}
        for model, rows in wave.items():
            pks_by_action: dict[DeletionAction | None, list[Any]] = {}
            for pk, action in rows.items():
                pks_by_action.setdefault(action, []).append(pk)
            if own_delete_pks := pks_by_action.get(DeletionAction.OWN_DELETE):
                await self._delete_through_override(model, own_delete_pks)
            if own_dispatch_pks := pks_by_action.get(DeletionAction.OWN_DISPATCH):
                await self._delete_with_own_dispatch(model, own_dispatch_pks)
            if soft_deleted_pks := pks_by_action.get(DeletionAction.SOFT_DELETE):
                if tree_soft_deleted_pks is not None:
                    tree_soft_deleted_pks.extend(soft_deleted_pks)
                else:
                    await self._persist_soft_deletes(model, soft_deleted_pks)
            if deleted_pks := pks_by_action.get(DeletionAction.DELETE):
                hard_deleted[model] = deleted_pks
        return hard_deleted

    def _check_row_writes(self, plan: DeletionPlan, *, persist_roots: bool) -> None:
        """Refuses the cascade before its first write when a connection it writes on doesn't update
        or delete stored rows and the cascade needs to - a database without transactions would keep
        the writes made before the refused one.

        Args:
            plan: The collected cascade.
            persist_roots: Whether the root rows themselves are written too.

        Raises:
            UnSupportedError: A write of the cascade has no statement on its connection.
        """
        for wave_index, wave in enumerate(plan.waves):
            for model, rows in wave.items():
                actions = set(rows.values())
                if actions - HANDED_OVER_ACTIONS:
                    for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(model):
                        if foreign_key_field.on_delete in {OnDelete.SET_NULL, OnDelete.SET_DEFAULT}:
                            self._check_row_write(backward_field.related_model, updates=True)
                    if self.persist_as_hard_delete or model._meta.soft_delete_hard_cascade:
                        for many_to_many_field in DeletionGraph.get_many_to_many_fields(model):
                            if (
                                many_to_many_field.through_model is None
                                and many_to_many_field.on_delete == OnDelete.CASCADE
                            ):
                                self._check_row_write(model, updates=False)
                            elif (
                                many_to_many_field.through_model is None
                                and many_to_many_field.on_delete == OnDelete.SET_NULL
                            ):
                                self._check_row_write(model, updates=True)
                if wave_index == 0:
                    continue
                if DeletionAction.SOFT_DELETE in actions:
                    self._check_row_write(model, updates=True)
                if DeletionAction.DELETE in actions:
                    self._check_row_write(model, updates=False)
        for backward_field, _target_values in plan.rows_without_key:
            self._check_row_write(backward_field.related_model, updates=False)
        if persist_roots:
            self._check_row_write(
                self.model, updates=not self.persist_as_hard_delete, model_connection=self.connection
            )

    def _check_row_write(
        self, model: type[Model], *, updates: bool, model_connection: DatabaseClient | None = None
    ) -> None:
        """Refuses an ``UPDATE`` (``updates``) or a ``DELETE`` of ``model`` rows the connection they
        are written on doesn't run.

        Args:
            model: The model whose rows are written.
            updates: An ``UPDATE`` rather than a ``DELETE``.
            model_connection: Connection to write through instead of the one ``model`` resolves to.

        Raises:
            UnSupportedError: The connection doesn't run the write.
        """
        connection = model_connection or self._get_model_connection(model) or model.get_connection(for_write=True)
        if connection.features.supports_row_updates if updates else connection.features.supports_row_deletes:
            return
        statement = "UPDATE" if updates else "DELETE"
        raise UnSupportedError(
            f"Deleting {self.model.__name__} rows needs a {statement} of {model.__name__} rows, which the "
            f"{connection.dialect.name} database of {connection.connection_alias!r} doesn't run - nothing was written"
        )

    async def _capture_database_deletes(self, plan: DeletionPlan) -> None:
        """Captures the rows of models with ``Meta.change_capture`` the database's own cascade will
        delete - read before any write, in the delete's transaction.

        Args:
            plan: The collected plan.
        """
        for model, pks in plan.get_pks_by_model(DeletionAction.DELETE_BY_DATABASE).items():
            capture_needs = model._meta.change_capture_needs
            if capture_needs is None or not capture_needs.captures(RowOperation.DELETE):
                continue
            model_connection = self._get_model_connection(model) or model.get_connection(for_write=True)
            values_by_pk = await ChangeCapturing.read_values(model, model_connection, pks, capture_needs, lock=True)
            changes = [
                ChangeCapturing.build_change(
                    model, capture_needs, RowOperation.DELETE, pk, before=values, tenant=tenant
                )
                for pk, (values, tenant) in values_by_pk.items()
            ]
            await ChangeCapturing.capture(model_connection, model, changes)

    @staticmethod
    def _is_tree_of_one_model(plan: DeletionPlan) -> bool:
        """Whether every row of the plan is of the root model, which only its own ``on_delete=CASCADE``
        relation points at, and the delete writes each row itself."""
        model = plan.root_model
        relations = DeletionGraph.get_backward_relations(model)
        return (
            len(relations) == 1
            and relations[0][0].related_model is model
            and relations[0][1].on_delete == OnDelete.CASCADE
            and not DeletionGraph.get_many_to_many_fields(model)
            and not plan.rows_without_key
            and all(
                wave_model is model and not HANDED_OVER_ACTIONS.intersection(rows.values())
                for wave in plan.waves
                for wave_model, rows in wave.items()
            )
        )

    def _merge_tree_waves(
        self, hard_deleted_waves: list[dict[type[Model], list[Any]]]
    ) -> list[dict[type[Model], list[Any]]]:
        """The hard deletes of a tree of one model's rows as few waves as can be written in order:
        all of them at once - or, deleted deepest first where the database's own cascade stops at a
        depth, bands of fewer levels than that, each listing its deepest rows first.

        Args:
            hard_deleted_waves: The rows of each wave below the roots, by model.

        Returns:
            The merged waves, in the order they are written.
        """
        model = self.model
        if not self.bottom_up_persist:
            return [{model: [pk for wave in hard_deleted_waves for pk in wave.get(model, ())]}]
        model_connection = self._get_model_connection(model)
        depth_limit = (model_connection or model.get_connection(for_write=True)).features.cascade_depth_limit
        band_size = len(hard_deleted_waves) if depth_limit is None else max(1, depth_limit - 1)
        deepest_first = list(reversed(hard_deleted_waves))
        bands: list[dict[type[Model], list[Any]]] = [
            {model: [pk for wave in deepest_first[start : start + band_size] for pk in wave.get(model, ())]}
            for start in range(0, len(deepest_first), band_size)
        ]
        # Written in reverse below - the deepest band first.
        bands.reverse()
        return bands

    async def _get_reachable_keys(self, plan: DeletionPlan) -> frozenset[RowKey]:
        """Every row the cascade reaches from its roots through ``on_delete=CASCADE`` - a guarding
        row among them goes with the rows it guards, so it doesn't block. Read on first use."""
        if self.reachable_keys is None:
            self.reachable_keys = await DeletionCollector.get_reachable_keys(
                self.model, plan.root_pks, self.connection
            )
        return self.reachable_keys

    async def _check_guarding_rows(self, plan: DeletionPlan) -> None:
        """Raises if a ``RESTRICT``/``NO_ACTION`` relation the cascade enforces points at a row it
        removes, or a ``PROTECT`` relation guards a row below the roots - a guarding row the same
        delete removes as well doesn't count.

        Raises:
            IntegrityError: Restricting rows exist.
            ProtectedError: Protecting rows exist.
        """
        plan_keys = plan.get_keys()
        for wave_index, wave in enumerate(plan.waves):
            for model, rows in wave.items():
                pks = [pk for pk, action in rows.items() if action not in HANDED_OVER_ACTIONS]
                if not pks:
                    continue
                await self._check_restricting_rows(plan, model, pks, plan_keys)
                if wave_index == 0:
                    continue
                protecting_rows = await DeletionCollector.find_protecting_rows(
                    model, pks, self.connection, exclude=plan_keys
                )
                if protecting_rows is not None:
                    # A guard deeper in the tree, or beside the guarded row, goes with it.
                    protecting_rows = await DeletionCollector.find_protecting_rows(
                        model, pks, self.connection, exclude=await self._get_reachable_keys(plan)
                    )
                if protecting_rows is not None:
                    raise ProtectedError(*protecting_rows)

    async def _check_restricting_rows(
        self, plan: DeletionPlan, model: type[Model], pks: list[Any], plan_keys: frozenset[RowKey]
    ) -> None:
        """Raises if a ``RESTRICT``/``NO_ACTION`` relation this cascade enforces has rows pointing
        at any of the ``model`` rows ``pks``.

        Raises:
            IntegrityError: Such rows exist.
        """
        restricting_group = await DeletionCollector.find_first_guarding_rows(
            model,
            pks,
            self.connection,
            guarding_actions=RESTRICTING_ON_DELETE_ACTIONS,
            exclude=plan_keys,
            only_unconstrained=self.only_unconstrained,
        )
        if restricting_group is not None:
            # A restricting row deeper in the tree, or beside this one, goes with it.
            restricting_group = await DeletionCollector.find_first_guarding_rows(
                model,
                pks,
                self.connection,
                guarding_actions=RESTRICTING_ON_DELETE_ACTIONS,
                exclude=await self._get_reachable_keys(plan),
                only_unconstrained=self.only_unconstrained,
            )
        if restricting_group is None:
            return
        __, __, guarding_field = restricting_group
        if isinstance(guarding_field, ManyToManyFieldInstance):
            relation_name = f"M2M relation '{guarding_field.model_field_name}'"
        else:
            relation_name = f"{guarding_field.model.__name__}.{guarding_field.model_field_name}"
        raise IntegrityError(
            f"Cannot {self.verb} {model.__name__} because {relation_name} has "
            f"on_delete={guarding_field.on_delete.name} and live rows point at it"
        )

    async def _delete_through_override(self, model: type[Model], pks: list[Any]) -> None:
        """Deletes the ``model`` rows ``pks`` one by one through their own overridden ``delete()``
        - each as its own tenant, since a row found through a real FK match may belong to another
        tenant than the active scope."""
        model_connection = RelatedRows.get_connection_for(model, self.connection)
        tenant_field = model._meta.tenant_field
        for row in await self._get_written_rows_queryset(model, pks, model_connection):
            tenant = getattr(row, tenant_field) if tenant_field else None
            with Tenancy.scope(tenant) if tenant is not None else nullcontext():
                await row.delete(using=model_connection)

    async def _delete_with_own_dispatch(self, model: type[Model], pks: list[Any]) -> None:
        """Deletes the ``model`` rows ``pks`` the way their own model deletes (soft or hard, with
        its own cascade) - through ``QuerySet.delete()`` over every tenant, which skips rows
        already soft-deleted."""
        model_connection = RelatedRows.get_connection_for(model, self.connection)
        await RelatedRows.get_base_queryset(model).filter(pk__in=pks).using(model_connection).delete()

    async def _update_pointing_foreign_keys(self, model: type[Model], pks: list[Any]) -> None:
        """Applies ``on_delete=SET_NULL``/``SET_DEFAULT`` to the rows pointing at the ``model``
        rows ``pks``, soft-deleted ones and every tenant's included.

        Raises:
            ConfigurationError: A ``SET_DEFAULT`` composite FK has no fitting default.
        """
        for backward_field, foreign_key_field in DeletionGraph.get_backward_relations(model):
            if foreign_key_field.on_delete not in {OnDelete.SET_NULL, OnDelete.SET_DEFAULT} or (
                self.only_unconstrained
                and foreign_key_field.has_database_constraint
                # The rows of a captured model are updated here, captured - not by the database.
                and backward_field.related_model._meta.change_capture_needs is None
            ):
                continue
            if foreign_key_field.on_delete == OnDelete.SET_NULL:
                # The shadow *_id column(s), not the relation attribute - .update() on an FK field
                # expects a Model instance as the value.
                update_values: dict[str, Any] | None = dict.fromkeys(foreign_key_field.source_fields)
            else:
                update_values = self._get_set_default_values(backward_field, foreign_key_field)
            if update_values is None:
                continue
            target_values = await RelatedRows.get_target_values(model, foreign_key_field, pks, self.connection)
            for queryset in RelatedRows.get_pointing_querysets(backward_field, target_values, self.connection):
                await queryset.update(**update_values)

    def _get_set_default_values(
        self,
        backward_field: BackwardForeignKeyRelation[Any] | BackwardOneToOneRelation[Any],
        foreign_key_field: ForeignKeyFieldInstance[Any],
    ) -> dict[str, Any] | None:
        """The shadow column values ``on_delete=SET_DEFAULT`` writes for ``foreign_key_field``.

        Returns:
            ``{shadow column: value}``, or ``None`` when the field has no default at all.

        Raises:
            ConfigurationError: A composite FK has only a ``db_default``, or a default that
                doesn't fit its columns.
        """
        generic_relation = foreign_key_field.generic_relation
        if generic_relation is not None:
            # A branch of a generic foreign key: the default's branch is set, every other cleared.
            return generic_relation.get_set_default_values()
        if foreign_key_field.default is not None:
            default = foreign_key_field.default
            value = default() if callable(default) else default
        elif foreign_key_field.has_db_default():
            # QuerySet.update() has no notion of "the column's own DB-level default", so the
            # db_default is rendered as SQL (or bound as a plain literal).
            if len(foreign_key_field.source_fields) > 1:
                raise ConfigurationError(
                    f"on_delete=SET_DEFAULT with only a db_default (no Python-level "
                    f"default=) isn't supported for composite-target FK "
                    f"'{foreign_key_field.model_field_name}' - provide a tuple default= instead."
                )
            db_default = foreign_key_field.db_default
            related_model = backward_field.related_model
            related_connection = RelatedRows.get_connection_for(
                related_model, self.connection
            ) or related_model.get_connection(for_write=True)
            if hasattr(db_default, "get_sql"):
                value = RawSQL(db_default.get_sql(related_connection.dialect))
            else:
                value = related_connection.dialect.types.get_db_value(foreign_key_field, db_default, related_model)
        else:
            return None
        if len(foreign_key_field.source_fields) == 1:
            return {cast("str", foreign_key_field.source_field): value}
        if not isinstance(value, (tuple, list)) or len(value) != len(foreign_key_field.source_fields):
            raise ConfigurationError(
                f"on_delete=SET_DEFAULT for composite-target FK "
                f"'{foreign_key_field.model_field_name}' needs a {len(foreign_key_field.source_fields)}-tuple "
                f"default= matching {foreign_key_field.to_field_names}, got {value!r}"
            )
        return dict(zip(foreign_key_field.source_fields, value, strict=True))

    async def _clear_many_to_many_through_rows(self, model: type[Model], pks: list[Any]) -> None:
        """Deletes (``CASCADE``) or disconnects (``SET_NULL``) the auto-generated through-table rows of
        the given rows. A soft delete keeps them unless ``Meta.soft_delete_hard_cascade``.
        """
        # Local import: the relation accessors import the queryset package, which imports the query statements.
        from hare.fields.relations.relation_accessors import RelationAccessors

        if not self.persist_as_hard_delete and not model._meta.soft_delete_hard_cascade:
            return
        for many_to_many_field in DeletionGraph.get_many_to_many_fields(model):
            if (
                many_to_many_field.through_model is not None
                or many_to_many_field.on_delete not in {OnDelete.CASCADE, OnDelete.SET_NULL}
                or (self.only_unconstrained and many_to_many_field.has_database_constraint)
            ):
                continue
            chosen_connection, through_table, criteria = RelatedRows.get_through_row_criteria(
                model, many_to_many_field, pks, self.connection
            )
            if WrittenConnections.is_recording:
                WrittenConnections.record(chosen_connection.connection_alias)
            for criterion in criteria:
                if many_to_many_field.on_delete == OnDelete.CASCADE:
                    query = chosen_connection.query_class.from_(through_table).where(criterion).delete()
                else:
                    query = chosen_connection.query_class.update(through_table).where(criterion)
                    for column_name in many_to_many_field.backward_keys:
                        query = query.set(column_name, None)
                await chosen_connection.execute(*query.get_parameterized_sql())
            root_instance = self.root_instance
            if (
                many_to_many_field.on_delete == OnDelete.CASCADE
                and root_instance is not None
                and type(root_instance) is model
            ):
                many_to_many_relation = cast(
                    "ManyToManyRelation[Any]",
                    RelationAccessors.get_relation(root_instance, many_to_many_field.model_field_name),
                )
                many_to_many_relation._reset_cache_on_rollback(chosen_connection)
                many_to_many_relation._invalidate_local_cache()

    def _get_model_connection(self, model: type[Model]) -> DatabaseClient | None:
        """The connection a write to ``model`` goes through - ``None`` for ``model``'s own default."""
        return RelatedRows.get_connection_for(model, self.connection)

    def _split_into_batches(self, model: type[Model], pks: list[Any]) -> list[list[Any]]:
        """Splits ``pks`` into batches a ``pk__in`` filter binds within the bind-parameter ceiling."""
        model_connection = self._get_model_connection(model) or model.get_connection(for_write=True)
        return RelatedRows.split_into_batches(pks, model_connection, len(model._meta.primary_key_attribute_names))

    @staticmethod
    def _get_written_rows_queryset(
        model: type[Model], pks: list[Any], model_connection: DatabaseClient | None
    ) -> QuerySet[Any]:
        """The ``model`` rows ``pks`` of every tenant, soft-deleted ones included."""
        return RelatedRows.include_soft_deleted(
            RelatedRows.get_base_queryset(model).filter(pk__in=pks).using(model_connection)
        )

    async def _read_root_versions(self, root_pks: list[Any]) -> None:
        """Reads the version of every root row, for the soft-delete write's stale check."""
        optimistic_lock_field = cast("str", self.model._meta.optimistic_lock_field)
        primary_key_attribute_names = self.model._meta.primary_key_attribute_names
        pk_column_count = len(primary_key_attribute_names)
        for batch in self._split_into_batches(self.model, root_pks):
            queryset = self._get_written_rows_queryset(self.model, batch, self.connection)
            for values in await queryset.values_list(*primary_key_attribute_names, optimistic_lock_field):
                root_pk = values[0] if pk_column_count == 1 else tuple(values[:pk_column_count])
                self.row_versions[(self.model, root_pk)] = values[pk_column_count]

    async def _persist_soft_deletes(
        self, model: type[Model], pks: list[Any], *, model_connection: DatabaseClient | None = None
    ) -> None:
        """Soft-deletes the rows with an ``UPDATE`` per batch, bumping the optimistic lock and
        ``auto_now`` fields. A row another write soft-deleted meanwhile keeps its own deletion time.

        Args:
            model: The model of the rows.
            pks: Their primary keys.
            model_connection: Connection to write through instead of the one ``model`` resolves to.

        Raises:
            StaleObjectError: A row's version changed since the cascade read it.
            IntegrityError: A row no longer exists.
        """
        if not pks:
            return
        from hare.query.statements.write.update_query import UpdateQuery

        meta = model._meta
        soft_delete_field = cast("str", meta.soft_delete_field)
        optimistic_lock_field = meta.optimistic_lock_field
        write_connection = model_connection or self._get_model_connection(model)
        pks_by_version: dict[Any, list[Any]] = {}
        for pk in pks:
            version = self.row_versions.get((model, pk)) if optimistic_lock_field else None
            pks_by_version.setdefault(version, []).append(pk)
        for version, version_pks in pks_by_version.items():
            for batch in self._split_into_batches(model, version_pks):
                queryset = self._get_written_rows_queryset(model, batch, write_connection).filter(
                    **{f"{soft_delete_field}__isnull": True}
                )
                if optimistic_lock_field:
                    queryset = queryset.filter(**{optimistic_lock_field: version})
                update_query = UpdateQuery(queryset, {soft_delete_field: self.deleted_at})
                if await update_query != len(batch):
                    await self._raise_for_unwritten_row(model, batch, version, write_connection)

    async def _raise_for_unwritten_row(
        self, model: type[Model], batch: list[Any], version: Any, write_connection: DatabaseClient | None
    ) -> None:
        """Raises for the first row of ``batch`` a soft-delete ``UPDATE`` did not write, unless another
        write soft-deleted every such row already.

        Raises:
            StaleObjectError: ``Meta.optimistic_lock_field`` is set - the row changed or
                disappeared.
            IntegrityError: Otherwise - the row no longer exists.
        """
        meta = model._meta
        optimistic_lock_field = meta.optimistic_lock_field
        primary_key_attribute_names = meta.primary_key_attribute_names
        pk_column_count = len(primary_key_attribute_names)
        written_pks = set()
        queryset = self._get_written_rows_queryset(model, batch, write_connection).filter(
            **{cast("str", meta.soft_delete_field): self.deleted_at}
        )
        if optimistic_lock_field and version is not None:
            queryset = queryset.filter(**{optimistic_lock_field: version + 1})
        for values in await queryset.values_list(*primary_key_attribute_names):
            written_pks.add(values[0] if pk_column_count == 1 else tuple(values))
        unwritten_pks = [pk for pk in batch if pk not in written_pks]
        already_deleted_pks: set[Any] = set()
        if unwritten_pks:
            already_deleted_pks = await RelatedRows.get_soft_deleted_pks(model, unwritten_pks, write_connection)
            if all(pk in already_deleted_pks for pk in unwritten_pks):
                return
        unwritten_pk = next((pk for pk in unwritten_pks if pk not in already_deleted_pks), batch[0])
        if optimistic_lock_field:
            raise StaleObjectError(
                f"{model.__name__} (pk={unwritten_pk}) was modified concurrently - expected version {version}",
                model,
                unwritten_pk,
                version,
            )
        raise IntegrityError(f"Can't delete object that doesn't exist. PK: {unwritten_pk}")

    async def _persist_hard_deletes(
        self, model: type[Model], pks: list[Any], *, model_connection: DatabaseClient | None = None
    ) -> None:
        """Deletes the ``model`` rows ``pks`` - a ``DELETE`` per batch. A row the database's own
        cascade already removed is simply not matched.

        Args:
            model: The model of the rows.
            pks: Primary keys of the ``model`` rows.
            model_connection: Connection to write through instead of the one ``model`` resolves to.
        """
        if not pks:
            return
        from hare.query.statements.write.delete_query import DeleteQuery

        write_connection = model_connection or self._get_model_connection(model)
        for batch in self._split_into_batches(model, pks):
            queryset = self._get_written_rows_queryset(model, batch, write_connection)
            delete_query = DeleteQuery(queryset)
            await delete_query.execute_statement()
