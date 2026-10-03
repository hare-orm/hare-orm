from __future__ import annotations

import datetime
from contextlib import nullcontext
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, ClassVar, cast

from hare.exceptions import ConfigurationError, IntegrityError, ProtectedError, StaleObjectError
from hare.fields.enums import OnDelete
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.models.constants import RESTRICTING_ON_DELETE_ACTIONS
from hare.models.deletion.deletion_collector import DeletionCollector
from hare.models.deletion.deletion_graph import DeletionGraph
from hare.models.deletion.deletion_plan import DeletionPlan, RowKey
from hare.models.deletion.related_rows import RelatedRows
from hare.models.enums import DeletionAction
from hare.models.tenancy import Tenancy
from hare.query.expressions import RawSQL
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
    from hare.fields.relations.fields.backward_one_to_one_relation import BackwardOneToOneRelation
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation

#: The actions of rows their own model's delete takes over - the cascade doesn't go below them.
HANDED_OVER_ACTIONS = frozenset({DeletionAction.OWN_DELETE, DeletionAction.OWN_DISPATCH})


class CascadeDeletion:
    """Carries out the ``on_delete`` cascade below root rows of one model: collects the plan, checks
    PROTECT/RESTRICT, then writes it - one statement per relation and batch. Every write shares one
    deletion time; the caller runs the cascade in one transaction.

    Args:
        model: The model of the root rows.
        db: The connection of the cascade; a model on another connection uses its own.
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
        db: DatabaseClient | None,
        *,
        only_unconstrained: bool,
        persist_as_hard_delete: bool,
        bottom_up_persist: bool = False,
        deleted_at: datetime.datetime | None = None,
        root_instance: Model | None = None,
    ) -> None:
        self.model = model
        self.db = db
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
        instance: Model,
        db: DatabaseClient | None,
        *,
        only_unconstrained: bool,
        persist_as_hard_delete: bool,
        bottom_up_persist: bool = False,
        deleted_at: datetime.datetime | None = None,
    ) -> bool:
        """Carries out the cascade below one instance being deleted - its own row is the caller's.

        Args:
            instance: The row being deleted.
            db: The connection the delete runs on.
            only_unconstrained: See the class.
            persist_as_hard_delete: See the class.
            bottom_up_persist: See the class.
            deleted_at: See the class.

        Returns:
            Whether a cascade cycle led back to ``instance`` - the database may already have removed
            its row.

        Raises:
            ProtectedError: A PROTECT relation guards a row the cascade reaches.
            IntegrityError: ``RESTRICT``/``NO_ACTION`` rows point at a row the cascade reaches.
        """
        cascade_deletion = CascadeDeletion(
            type(instance),
            db,
            only_unconstrained=only_unconstrained,
            persist_as_hard_delete=persist_as_hard_delete,
            bottom_up_persist=bottom_up_persist,
            deleted_at=deleted_at,
            root_instance=instance,
        )
        return bool(await cascade_deletion.run([instance.pk]))

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
            self.db,
            root_action=DeletionAction.DELETE if self.persist_as_hard_delete else DeletionAction.SOFT_DELETE,
            only_unconstrained=self.only_unconstrained,
            hands_over_rows=True,
            reads_versions=not self.persist_as_hard_delete,
        ).collect(self.model, root_pks)
        self.row_versions = plan.row_versions
        if persist_roots and not self.persist_as_hard_delete and self.model._meta.optimistic_lock_field:
            await self._read_root_versions(root_pks)
        await self._check_guarding_rows(plan)
        # A tree of one model's rows that nothing else points at: every wave's rows only change
        # themselves, so each kind of write is made for the whole tree at once.
        writes_tree_at_once = self._is_tree_of_one_model(plan)
        tree_soft_deleted_pks: list[Any] = []
        hard_deleted_waves: list[dict[type[Model], list[Any]]] = []
        for wave_index, wave in enumerate(plan.waves):
            for model, rows in wave.items():
                walked_pks = [pk for pk, action in rows.items() if action not in HANDED_OVER_ACTIONS]
                if walked_pks:
                    await self._update_pointing_foreign_keys(model, walked_pks)
                    await self._clear_m2m_through_rows(model, walked_pks)
            if wave_index == 0:
                continue
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
                    if writes_tree_at_once:
                        tree_soft_deleted_pks.extend(soft_deleted_pks)
                    else:
                        await self._persist_soft_deletes(model, soft_deleted_pks)
                if deleted_pks := pks_by_action.get(DeletionAction.DELETE):
                    hard_deleted[model] = deleted_pks
            hard_deleted_waves.append(hard_deleted)
        await self._persist_soft_deletes(self.model, tree_soft_deleted_pks)
        if writes_tree_at_once:
            hard_deleted_waves = self._merge_tree_waves(hard_deleted_waves)
        for backward_field, target_values in plan.rows_without_key:
            # No key to list them by - deleted by the relation's own condition, as the database's
            # own ON DELETE CASCADE would.
            for queryset in RelatedRows.get_pointing_querysets(backward_field, target_values, self.db):
                await queryset.delete()
        # A hard delete writes nothing before the whole tree is known: the database's cascade of an
        # earlier row would remove rows whose unconstrained relations the walk never found.
        for hard_deleted in reversed(hard_deleted_waves) if self.bottom_up_persist else hard_deleted_waves:
            for model, pks in hard_deleted.items():
                await self._persist_hard_deletes(model, pks)
        if persist_roots:
            if self.persist_as_hard_delete:
                await self._persist_hard_deletes(self.model, root_pks, model_db=self.db)
            else:
                await self._persist_soft_deletes(self.model, root_pks, model_db=self.db)
        return plan

    def _is_tree_of_one_model(self, plan: DeletionPlan) -> bool:
        """Whether every row of the plan is of the root model, which only its own ``on_delete=CASCADE``
        relation points at, and the delete writes each row itself."""
        model = plan.root_model
        relations = DeletionGraph.get_backward_relations(model)
        return (
            len(relations) == 1
            and relations[0][0].related_model is model
            and relations[0][1].on_delete == OnDelete.CASCADE
            and not DeletionGraph.get_m2m_fields(model)
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
        model_db = self._get_model_db(model)
        depth_limit = (model_db or model.get_connection(for_write=True)).features.cascade_depth_limit
        band_size = len(hard_deleted_waves) if depth_limit is None else max(1, depth_limit - 1)
        bands: list[dict[type[Model], list[Any]]] = []
        deepest_first = list(reversed(hard_deleted_waves))
        for start in range(0, len(deepest_first), band_size):
            bands.append(
                {model: [pk for wave in deepest_first[start : start + band_size] for pk in wave.get(model, ())]}
            )
        # Written in reverse below - the deepest band first.
        bands.reverse()
        return bands

    async def _get_reachable_keys(self, plan: DeletionPlan) -> frozenset[RowKey]:
        """Every row the cascade reaches from its roots through ``on_delete=CASCADE`` - a guarding
        row among them goes with the rows it guards, so it doesn't block. Read on first use."""
        if self.reachable_keys is None:
            self.reachable_keys = await DeletionCollector.get_reachable_keys(self.model, plan.root_pks, self.db)
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
                protecting_rows = await DeletionCollector.find_protecting_rows(model, pks, self.db, exclude=plan_keys)
                if protecting_rows is not None:
                    # A guard deeper in the tree, or beside the guarded row, goes with it.
                    protecting_rows = await DeletionCollector.find_protecting_rows(
                        model, pks, self.db, exclude=await self._get_reachable_keys(plan)
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
            self.db,
            guarding_actions=RESTRICTING_ON_DELETE_ACTIONS,
            exclude=plan_keys,
            only_unconstrained=self.only_unconstrained,
        )
        if restricting_group is not None:
            # A restricting row deeper in the tree, or beside this one, goes with it.
            restricting_group = await DeletionCollector.find_first_guarding_rows(
                model,
                pks,
                self.db,
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
        model_db = RelatedRows.get_connection_for(model, self.db)
        tenant_field = model._meta.tenant_field
        for row in await self._get_written_rows_queryset(model, pks, model_db):
            tenant = getattr(row, tenant_field) if tenant_field else None
            with Tenancy.scope(tenant) if tenant is not None else nullcontext():
                await row.delete(using=model_db)

    async def _delete_with_own_dispatch(self, model: type[Model], pks: list[Any]) -> None:
        """Deletes the ``model`` rows ``pks`` the way their own model deletes (soft or hard, with
        its own cascade) - through ``QuerySet.delete()`` over every tenant, which skips rows
        already soft-deleted."""
        model_db = RelatedRows.get_connection_for(model, self.db)
        await RelatedRows.get_base_queryset(model).filter(pk__in=pks).using(model_db).delete()

    async def _update_pointing_foreign_keys(self, model: type[Model], pks: list[Any]) -> None:
        """Applies ``on_delete=SET_NULL``/``SET_DEFAULT`` to the rows pointing at the ``model``
        rows ``pks``, soft-deleted ones and every tenant's included.

        Raises:
            ConfigurationError: A ``SET_DEFAULT`` composite FK has no fitting default.
        """
        for backward_field, fk_field in DeletionGraph.get_backward_relations(model):
            if fk_field.on_delete not in (OnDelete.SET_NULL, OnDelete.SET_DEFAULT) or (
                self.only_unconstrained and fk_field.has_database_constraint
            ):
                continue
            if fk_field.on_delete == OnDelete.SET_NULL:
                # The shadow *_id column(s), not the relation attribute - .update() on an FK field
                # expects a Model instance as the value.
                update_values: dict[str, Any] | None = dict.fromkeys(fk_field.source_fields)
            else:
                update_values = self._get_set_default_values(backward_field, fk_field)
            if update_values is None:
                continue
            target_values = await RelatedRows.get_target_values(model, fk_field, pks, self.db)
            for queryset in RelatedRows.get_pointing_querysets(backward_field, target_values, self.db):
                await queryset.update(**update_values)

    def _get_set_default_values(
        self,
        backward_field: BackwardFKRelation[Any] | BackwardOneToOneRelation[Any],
        fk_field: ForeignKeyFieldInstance[Any],
    ) -> dict[str, Any] | None:
        """The shadow column values ``on_delete=SET_DEFAULT`` writes for ``fk_field``.

        Returns:
            ``{shadow column: value}``, or ``None`` when the field has no default at all.

        Raises:
            ConfigurationError: A composite FK has only a ``db_default``, or a default that
                doesn't fit its columns.
        """
        if fk_field.default is not None:
            default = fk_field.default
            value = default() if callable(default) else default
        elif fk_field.has_db_default():
            # QuerySet.update() has no notion of "the column's own DB-level default", so the
            # db_default is rendered as SQL (or bound as a plain literal).
            if len(fk_field.source_fields) > 1:
                raise ConfigurationError(
                    f"on_delete=SET_DEFAULT with only a db_default (no Python-level "
                    f"default=) isn't supported for composite-target FK "
                    f"'{fk_field.model_field_name}' - provide a tuple default= instead."
                )
            db_default = fk_field.db_default
            related_model = backward_field.related_model
            related_db = RelatedRows.get_connection_for(related_model, self.db) or related_model.get_connection(
                for_write=True
            )
            if hasattr(db_default, "get_sql"):
                value = RawSQL(db_default.get_sql(related_db.dialect))
            else:
                value = related_db.dialect.types.get_db_value(fk_field, db_default, related_model)
        else:
            return None
        if len(fk_field.source_fields) == 1:
            return {cast("str", fk_field.source_field): value}
        if not isinstance(value, (tuple, list)) or len(value) != len(fk_field.source_fields):
            raise ConfigurationError(
                f"on_delete=SET_DEFAULT for composite-target FK "
                f"'{fk_field.model_field_name}' needs a {len(fk_field.source_fields)}-tuple "
                f"default= matching {fk_field.to_field_names}, got {value!r}"
            )
        return dict(zip(fk_field.source_fields, value, strict=True))

    async def _clear_m2m_through_rows(self, model: type[Model], pks: list[Any]) -> None:
        """Deletes (``CASCADE``) or disconnects (``SET_NULL``) the auto-generated through-table rows of
        the given rows. A soft delete keeps them unless ``Meta.soft_delete_hard_cascade``.
        """
        if not self.persist_as_hard_delete and not model._meta.soft_delete_hard_cascade:
            return
        for m2m_field in DeletionGraph.get_m2m_fields(model):
            if (
                m2m_field.through_model is not None
                or m2m_field.on_delete not in (OnDelete.CASCADE, OnDelete.SET_NULL)
                or (self.only_unconstrained and m2m_field.has_database_constraint)
            ):
                continue
            chosen_db, through_table, criteria = RelatedRows.get_through_row_criteria(model, m2m_field, pks, self.db)
            for criterion in criteria:
                if m2m_field.on_delete == OnDelete.CASCADE:
                    query = chosen_db.query_class.from_(through_table).where(criterion).delete()
                else:
                    query = chosen_db.query_class.update(through_table).where(criterion)
                    for column_name in m2m_field.backward_keys:
                        query = query.set(column_name, None)
                await chosen_db.execute(*query.get_parameterized_sql())
            root_instance = self.root_instance
            if m2m_field.on_delete == OnDelete.CASCADE and root_instance is not None and type(root_instance) is model:
                m2m_relation = cast("ManyToManyRelation[Any]", root_instance._get_relation(m2m_field.model_field_name))
                m2m_relation._reset_cache_on_rollback(chosen_db)
                m2m_relation._invalidate_local_cache()

    def _get_model_db(self, model: type[Model]) -> DatabaseClient | None:
        """The connection a write to ``model`` goes through - ``None`` for ``model``'s own default."""
        return RelatedRows.get_connection_for(model, self.db)

    def _split_into_batches(self, model: type[Model], pks: list[Any]) -> list[list[Any]]:
        """Splits ``pks`` into batches a ``pk__in`` filter binds within the bind-parameter ceiling."""
        model_db = self._get_model_db(model) or model.get_connection(for_write=True)
        return RelatedRows.split_into_batches(pks, model_db, len(model._meta.pk_attr_names))

    def _get_written_rows_queryset(
        self, model: type[Model], pks: list[Any], model_db: DatabaseClient | None
    ) -> QuerySet[Any]:
        """The ``model`` rows ``pks`` of every tenant, soft-deleted ones included."""
        return RelatedRows.include_soft_deleted(
            RelatedRows.get_base_queryset(model).filter(pk__in=pks).using(model_db)
        )

    async def _read_root_versions(self, root_pks: list[Any]) -> None:
        """Reads the version of every root row, for the soft-delete write's stale check."""
        optimistic_lock_field = cast("str", self.model._meta.optimistic_lock_field)
        pk_attr_names = self.model._meta.pk_attr_names
        pk_column_count = len(pk_attr_names)
        for batch in self._split_into_batches(self.model, root_pks):
            queryset = self._get_written_rows_queryset(self.model, batch, self.db)
            for values in await queryset.values_list(*pk_attr_names, optimistic_lock_field):
                root_pk = values[0] if pk_column_count == 1 else tuple(values[:pk_column_count])
                self.row_versions[(self.model, root_pk)] = values[pk_column_count]

    async def _persist_soft_deletes(
        self, model: type[Model], pks: list[Any], *, model_db: DatabaseClient | None = None
    ) -> None:
        """Soft-deletes the rows with an ``UPDATE`` per batch, bumping the optimistic lock and
        ``auto_now`` fields. A row another write soft-deleted meanwhile keeps its own deletion time.

        Args:
            model: The model of the rows.
            pks: Their primary keys.
            model_db: Connection to write through instead of the one ``model`` resolves to.

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
        write_db = model_db or self._get_model_db(model)
        pks_by_version: dict[Any, list[Any]] = {}
        for pk in pks:
            version = self.row_versions.get((model, pk)) if optimistic_lock_field else None
            pks_by_version.setdefault(version, []).append(pk)
        for version, version_pks in pks_by_version.items():
            for batch in self._split_into_batches(model, version_pks):
                queryset = self._get_written_rows_queryset(model, batch, write_db).filter(
                    **{f"{soft_delete_field}__isnull": True}
                )
                if optimistic_lock_field:
                    queryset = queryset.filter(**{optimistic_lock_field: version})
                update_query = UpdateQuery(queryset, {soft_delete_field: self.deleted_at})
                if await update_query != len(batch):
                    await self._raise_for_unwritten_row(model, batch, version, write_db)

    async def _raise_for_unwritten_row(
        self, model: type[Model], batch: list[Any], version: Any, write_db: DatabaseClient | None
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
        pk_attr_names = meta.pk_attr_names
        pk_column_count = len(pk_attr_names)
        written_pks = set()
        queryset = self._get_written_rows_queryset(model, batch, write_db).filter(
            **{cast("str", meta.soft_delete_field): self.deleted_at}
        )
        if optimistic_lock_field and version is not None:
            queryset = queryset.filter(**{optimistic_lock_field: version + 1})
        for values in await queryset.values_list(*pk_attr_names):
            written_pks.add(values[0] if pk_column_count == 1 else tuple(values))
        unwritten_pks = [pk for pk in batch if pk not in written_pks]
        already_deleted_pks: set[Any] = set()
        if unwritten_pks:
            already_deleted_pks = await RelatedRows.get_soft_deleted_pks(model, unwritten_pks, write_db)
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
        self, model: type[Model], pks: list[Any], *, model_db: DatabaseClient | None = None
    ) -> None:
        """Deletes the ``model`` rows ``pks`` - a ``DELETE`` per batch. A row the database's own
        cascade already removed is simply not matched.

        Args:
            model: The model of the rows.
            pks: Primary keys of the ``model`` rows.
            model_db: Connection to write through instead of the one ``model`` resolves to.
        """
        if not pks:
            return
        from hare.query.statements.write.delete_query import DeleteQuery

        write_db = model_db or self._get_model_db(model)
        for batch in self._split_into_batches(model, pks):
            queryset = self._get_written_rows_queryset(model, batch, write_db)
            delete_query = DeleteQuery(queryset)
            await delete_query.execute_statement()
