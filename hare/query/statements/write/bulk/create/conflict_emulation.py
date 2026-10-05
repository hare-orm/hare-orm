from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import QueryError
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.models.write.constraints.unique_checks import UniqueChecks
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility
from hare.query.statements.constants import UNDECLARED_CONFLICT_TARGET_MESSAGE

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class ConflictEmulation:
    """``bulk_create()``'s handling of conflicting rows on a database with no ``ON CONFLICT`` - its
    uniqueness checked by hare (``Features.checks_constraints_before_write``): the stored rows holding
    the values of the batch read first, an object of such values skipped (``ignore_conflicts``) or its
    row updated (``update_fields``), the others inserted. Of the objects of an ``update_fields`` batch
    sharing a conflict key the later ones update the row after the insert, in order - the last one's
    values win, as the statements of an ``ON CONFLICT`` batch give. A row written in between the read
    and the writes isn't seen - not atomic as ``ON CONFLICT`` is."""

    @classmethod
    async def apply(cls, bulk_create: BulkCreateQuery[Any]) -> None:
        """Skips or updates the conflicting objects, leaving the others to the insert - its conflict
        handling cleared.

        Args:
            bulk_create: The bulk insert.
        """
        model = bulk_create.model
        connection = bulk_create._connection
        targets = cls.get_targets(bulk_create)
        inserted: list[Model] = []
        updated: list[Model] = []
        seen_by_target: list[dict[tuple[Any, ...], Model]] = [{} for _ in targets]
        repeat_counts: dict[tuple[Any, ...], int] = {}
        repeated_rounds: list[list[tuple[Model, Model]]] = []
        existing_by_target = [
            await UniqueChecks.fetch_existing(
                model,
                connection,
                target,
                list(
                    {
                        UniqueChecks.get_value_key(values): values
                        for instance in bulk_create._objects
                        if target.holds_for(instance) and (values := target.get_values(instance)) is not None
                    }.values()
                ),
            )
            for target in targets
        ]
        for instance in bulk_create._objects:
            conflict_key: Any = None
            conflicts = False
            # The values an inserted object takes - an object skipped for a conflict takes none.
            taken_values: list[tuple[dict[tuple[Any, ...], Model], tuple[Any, ...]]] = []
            for target, seen, existing in zip(targets, seen_by_target, existing_by_target, strict=True):
                if not target.holds_for(instance):
                    continue
                found_values = target.get_values(instance)
                if found_values is None:
                    continue
                # Keyed as the existing rows are - an array of the values by its items.
                values = UniqueChecks.get_value_key(found_values)
                if values in seen:
                    if bulk_create._update_fields:
                        round_index = repeat_counts.get(values, 0)
                        repeat_counts[values] = round_index + 1
                        if round_index == len(repeated_rounds):
                            repeated_rounds.append([])
                        repeated_rounds[round_index].append((instance, seen[values]))
                    conflicts = True
                    break
                if values in existing:
                    conflicts = True
                    conflict_key = existing[values][0]
                    if bulk_create._update_fields:
                        # A later object of these values updates the row this one updates.
                        seen[values] = instance
                    break
                taken_values.append((seen, values))
            if not conflicts:
                for seen, values in taken_values:
                    seen[values] = instance
                inserted.append(instance)
            elif bulk_create._update_fields and conflict_key is not None:
                # The object stands for the stored row it updates.
                instance.pk = conflict_key
                updated.append(instance)
        if updated:
            await ConflictEmulation.update_rows(bulk_create, updated, list(bulk_create._update_fields or ()))
        bulk_create._objects = inserted
        bulk_create._repeated_conflict_rounds = repeated_rounds
        bulk_create._repeated_conflict_update_fields = list(bulk_create._update_fields or ())
        bulk_create._ignore_conflicts = False
        bulk_create._update_fields = None
        bulk_create._on_conflict = None
        if connection.features.copies_bulk_inserts and not bulk_create._returning:
            bulk_create._use_copy = True

    @staticmethod
    async def update_repeated(bulk_create: BulkCreateQuery[Any]) -> None:
        """Updates the row of each object repeating a conflict key of an earlier object of the batch -
        once the earlier one is written, round by round.

        Args:
            bulk_create: The bulk insert.
        """
        repeated_rounds = bulk_create._repeated_conflict_rounds
        if not repeated_rounds:
            return
        bulk_create._repeated_conflict_rounds = []
        for repeated in repeated_rounds:
            for instance, earlier_instance in repeated:
                # The object stands for the row its earlier object wrote.
                instance.pk = earlier_instance.pk
            await ConflictEmulation.update_rows(
                bulk_create,
                [instance for instance, _ in repeated],
                bulk_create._repeated_conflict_update_fields,
            )

    @staticmethod
    async def update_rows(bulk_create: BulkCreateQuery[Any], objects: list[Model], update_fields: list[str]) -> None:
        """Updates the rows the objects conflict with, as ``ON CONFLICT DO UPDATE`` does: the
        ``Meta.optimistic_lock_field`` version of a row bumped, never compared with the object's - an
        object gets the bumped version with ``returning=True`` and keeps its own otherwise. The objects'
        written values become their dirty-tracking baseline.

        Args:
            bulk_create: The bulk insert.
            objects: The objects, each holding the primary key of its row - one object per row.
            update_fields: The fields written.
        """
        model = bulk_create.model
        meta = model._meta
        connection = bulk_create._connection
        lock_field_name = meta.optimistic_lock_field
        reads_rows = bulk_create._returning or bulk_create._reads_back_rows
        given_versions = []
        if lock_field_name:
            # The objects hold the stored versions for the update - the one the row holds is bumped.
            given_versions = [getattr(instance, lock_field_name) for instance in objects]
            key_attribute_names = meta.primary_key_attribute_names
            stored_versions = {
                tuple(row[:-1]): row[-1]
                for row in await RowScopes.get_base_queryset(
                    model, RowVisibility(all_tenants=True, include_deleted=True)
                )
                .using(connection)
                .filter(pk__in=[instance.pk for instance in objects])
                .values_list(*key_attribute_names, lock_field_name)
            }
            is_composite = len(key_attribute_names) > 1
            for instance in objects:
                key = instance.pk if is_composite else (instance.pk,)
                if key in stored_versions:
                    object.__setattr__(instance, lock_field_name, stored_versions[key])
        await meta.manager.get_queryset().using(connection).bulk_update(objects, update_fields, returning=reads_rows)
        if lock_field_name and not reads_rows:
            for instance, given_version in zip(objects, given_versions, strict=True):
                object.__setattr__(instance, lock_field_name, given_version)
        for instance in objects:
            object.__setattr__(instance, "_saved_in_db", True)
            if meta.track_dirty_fields:
                DirtyFields.snapshot_dirty_fields(instance)

    @staticmethod
    def get_targets(bulk_create: BulkCreateQuery[Any]) -> list[DeclaredUniqueness]:
        """The fields a conflict is on - the ones named, else every set declared unique for skipped
        objects and the primary key for updated ones.

        Args:
            bulk_create: The bulk insert.

        Returns:
            The sets of fields.

        Raises:
            QueryError: ``on_conflict`` names fields the model doesn't declare unique together.
        """
        model = bulk_create.model
        declared = DeclaredUniqueness.get_declared(model)
        if bulk_create._on_conflict:
            field_names = bulk_create.get_db_field_names(model, bulk_create._on_conflict, "on_conflict")
            attribute_names = tuple(DeclaredUniqueness.get_attribute_names(model, field_names))
            # As a database's ON CONFLICT, the target is a set of fields declared unique for every row.
            if not any(
                uniqueness.condition is None and set(uniqueness.attribute_names) == set(attribute_names)
                for uniqueness in declared
            ):
                raise QueryError(
                    UNDECLARED_CONFLICT_TARGET_MESSAGE.format(model=model.__name__, fields=", ".join(field_names))
                )
            return [DeclaredUniqueness(attribute_names, f"the conflict target {', '.join(field_names)}")]
        if bulk_create._update_fields:
            return declared[:1]
        return declared
