from __future__ import annotations

from collections.abc import Sequence
from itertools import repeat
from operator import attrgetter, is_
from typing import TYPE_CHECKING, Any

from hare.exceptions import IncompleteInstanceError, QueryError

if TYPE_CHECKING:
    from hare.models import Model
    from hare.models.model_binding import ModelBinding


class BulkUpdateChecks:
    """What ``bulk_update()`` checks of its objects before it writes: the tenant of each is resolved,
    and each has a primary key of its own, its ``Meta.optimistic_lock_field`` version loaded."""

    @staticmethod
    def check_tenant_defaults_ready(binding: ModelBinding, objects: Sequence[Model]) -> None:
        """Requires a tenant value before checking the object's tenant scope.

        Pending defaults are evaluated after this check, so an async tenant default cannot
        supply the value needed to compare the object with the active tenant.

        Args:
            binding: The model binding.
            objects: The objects written.

        Raises:
            QueryError: An object's tenant value is an unresolved async default.
        """
        model = binding._model
        tenant_field = binding.tenant_field
        pending_pks = [obj.pk for obj in objects if tenant_field in obj._await_when_save]
        if pending_pks:
            raise QueryError(
                f"bulk_update() on {model.__name__} received object(s) whose "
                f"tenant_field {tenant_field!r} still has an unresolved async default= "
                f"value: pk(s) {pending_pks}. Save or refresh these objects first, or set "
                f"{tenant_field!r} explicitly, before calling bulk_update()."
            )

    @staticmethod
    def check_primary_keys(binding: ModelBinding, objects: Sequence[Model]) -> None:
        """Raises for an object without a primary key and for an object whose
        ``Meta.optimistic_lock_field`` wasn't loaded.

        Args:
            binding: The model binding.
            objects: The objects written.

        Raises:
            QueryError: An object has no primary key.
            IncompleteInstanceError: An object lacks its optimistic lock field.
        """
        model = binding._model
        primary_key_attribute = binding.primary_key_attribute
        try:
            # A key of one field read off each object at once - the pk property reads it one call deeper.
            pk_values = (
                list(map(attrgetter(primary_key_attribute), objects))
                if type(primary_key_attribute) is str
                else [obj.pk for obj in objects]
            )
        except AttributeError:
            # Use the public key accessor when a direct key attribute is unavailable.
            pk_values = [obj.pk for obj in objects]
        if (
            any(any(part is None for part in pk) for pk in pk_values)
            if binding.has_composite_primary_key
            else any(map(is_, pk_values, repeat(None)))
        ):
            raise QueryError("All bulk_update() objects must have a primary key set.")
        # The optimistic lock field is compared on every bulk_update() - it has to be loaded.
        if optimistic_lock_field := binding.optimistic_lock_field:
            missing_version = [obj.pk for obj in objects if not hasattr(obj, optimistic_lock_field)]
            if missing_version:
                raise IncompleteInstanceError(
                    f"{model.__name__} is a partial model, Meta.optimistic_lock_field {optimistic_lock_field!r} "
                    f"is not available - every bulk_update() needs to read it for the staleness check, "
                    f"even when only updating other fields: pk(s) {missing_version}"
                )

    @staticmethod
    def raise_if_row_written_twice(
        binding: ModelBinding, objects: Sequence[Model], stored_pk_values: Sequence[tuple[Any, ...]]
    ) -> None:
        """Raises for two objects of one row - their keys equal as the database stores them (an
        ``EmailField`` lowercases the domain): two VALUES rows for one row, which of them is written
        unspecified.

        Args:
            binding: The model binding.
            objects: The objects written.
            stored_pk_values: Primary keys read from this call's already converted rows.

        Raises:
            QueryError: Two objects have the same primary key.
        """
        model = binding._model
        if len(set(stored_pk_values)) == len(stored_pk_values):
            return
        duplicate_pks: list[Any] = []
        pks_seen: set[Any] = set()
        for obj, stored_pk in zip(objects, stored_pk_values, strict=True):
            if stored_pk in pks_seen and obj.pk not in duplicate_pks:
                duplicate_pks.append(obj.pk)
            pks_seen.add(stored_pk)
        raise QueryError(
            f"bulk_update() on {model.__name__} received multiple objects with the "
            f"same primary key in a single call - each target row can only be updated once "
            f"per call: pk(s) {duplicate_pks}"
        )
