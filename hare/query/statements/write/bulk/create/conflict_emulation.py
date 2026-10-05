from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.exceptions import IntegrityError
from hare.models.write.constraints.declared_uniqueness import DeclaredUniqueness
from hare.models.write.constraints.unique_checks import UniqueChecks

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model
    from hare.query.statements.write.bulk.create.bulk_create_query import BulkCreateQuery


class ConflictEmulation:
    """``bulk_create()``'s handling of conflicting rows on a database with no ``ON CONFLICT`` - its
    uniqueness checked by hare (``Features.checks_constraints_before_write``): the stored rows holding
    the values of the batch read first, an object of such values skipped (``ignore_conflicts``) or its
    row updated (``update_fields``), the others inserted. A row written in between the read and the
    writes isn't seen - not atomic as ``ON CONFLICT`` is."""

    @classmethod
    async def apply(cls, bulk_create: BulkCreateQuery[Any]) -> None:
        """Skips or updates the conflicting objects, leaving the others to the insert - its conflict
        handling cleared.

        Args:
            bulk_create: The bulk insert.

        Raises:
            IntegrityError: Two objects of an ``update_fields`` batch hold one value of the conflict target.
        """
        model = bulk_create.model
        connection = bulk_create._connection
        targets = cls.get_targets(bulk_create)
        inserted: list[Model] = []
        updated: list[Model] = []
        seen_by_target: list[dict[tuple[Any, ...], Model]] = [{} for _ in targets]
        existing_by_target = [
            await UniqueChecks.fetch_existing(
                model,
                connection,
                target,
                list(
                    {
                        values
                        for instance in bulk_create._objects
                        if target.holds_for(instance) and (values := target.get_values(instance)) is not None
                    }
                ),
            )
            for target in targets
        ]
        for instance in bulk_create._objects:
            conflict_key: Any = None
            conflicts = False
            for target, seen, existing in zip(targets, seen_by_target, existing_by_target, strict=True):
                if not target.holds_for(instance):
                    continue
                values = target.get_values(instance)
                if values is None:
                    continue
                if values in seen:
                    if bulk_create._update_fields:
                        raise IntegrityError(
                            f"{model.__name__}: two objects of the batch hold {values!r} of the conflict target - an "
                            "update of a row can't take both"
                        )
                    conflicts = True
                    break
                seen[values] = instance
                if values in existing:
                    conflicts = True
                    conflict_key = existing[values][0]
                    break
            if not conflicts:
                inserted.append(instance)
            elif bulk_create._update_fields and conflict_key is not None:
                # The object stands for the stored row it updates.
                instance.pk = conflict_key
                updated.append(instance)
        if updated:
            queryset = model._meta.manager.get_queryset().using(connection)
            await queryset.bulk_update(updated, list(bulk_create._update_fields or ()))
            for instance in updated:
                object.__setattr__(instance, "_saved_in_db", True)
        bulk_create._objects = inserted
        bulk_create._ignore_conflicts = False
        bulk_create._update_fields = None
        bulk_create._on_conflict = None
        if connection.features.copies_bulk_inserts and not bulk_create._returning:
            bulk_create._use_copy = True

    @staticmethod
    def get_targets(bulk_create: BulkCreateQuery[Any]) -> list[DeclaredUniqueness]:
        """The fields a conflict is on - the ones named, else every set declared unique for skipped
        objects and the primary key for updated ones.

        Args:
            bulk_create: The bulk insert.

        Returns:
            The sets of fields.
        """
        model = bulk_create.model
        if bulk_create._on_conflict:
            field_names = bulk_create.get_db_field_names(model, bulk_create._on_conflict, "on_conflict")
            attribute_names = tuple(DeclaredUniqueness.get_attribute_names(model, field_names))
            return [DeclaredUniqueness(attribute_names, f"the conflict target {', '.join(field_names)}")]
        declared = DeclaredUniqueness.get_declared(model)
        if bulk_create._update_fields:
            return declared[:1]
        return declared
