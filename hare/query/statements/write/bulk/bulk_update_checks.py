from __future__ import annotations

from collections.abc import Sequence
from itertools import repeat
from operator import attrgetter, is_
from typing import TYPE_CHECKING, Any

from hare.exceptions import IncompleteInstanceError, QueryError

if TYPE_CHECKING:
    from hare.models import Model


class BulkUpdateChecks:
    """What ``bulk_update()`` checks of its objects before it writes: the tenant of each is resolved,
    and each has a primary key of its own, its ``Meta.optimistic_lock_field`` version loaded."""

    @staticmethod
    def check_tenant_values_resolved(model: type[Model], objects: Sequence[Model]) -> None:
        """Raises for an object whose ``Meta.tenant_field`` still waits for an async default - only
        save() resolves one, so there is no value yet to compare with the active tenant.

        Args:
            model: The model.
            objects: The objects written.

        Raises:
            QueryError: An object's tenant value is an unresolved async default.
        """
        tenant_field = model._meta.tenant_field
        pending_pks = [obj.pk for obj in objects if tenant_field in obj._await_when_save]
        if pending_pks:
            raise QueryError(
                f"bulk_update() on {model.__name__} received object(s) whose "
                f"tenant_field {tenant_field!r} still has an unresolved async default= "
                f"value: pk(s) {pending_pks}. Save or refresh these objects first, or set "
                f"{tenant_field!r} explicitly, before calling bulk_update()."
            )

    @staticmethod
    def check_primary_keys(model: type[Model], objects: Sequence[Model]) -> None:
        """Raises for an object without a primary key, for two objects of one row, and for an object
        whose ``Meta.optimistic_lock_field`` wasn't loaded.

        Args:
            model: The model.
            objects: The objects written.

        Raises:
            QueryError: An object has no primary key, or two have the same one.
            IncompleteInstanceError: An object lacks its optimistic lock field.
        """
        primary_key_attribute = model._meta.primary_key_attribute
        try:
            # A key of one field read off each object at once - the pk property reads it one call deeper.
            pk_values = (
                list(map(attrgetter(primary_key_attribute), objects))
                if type(primary_key_attribute) is str
                else [obj.pk for obj in objects]
            )
        except AttributeError:
            # A key never loaded reads as None through pk.
            pk_values = [obj.pk for obj in objects]
        if (
            any(any(part is None for part in pk) for pk in pk_values)
            if model._meta.has_composite_primary_key
            else any(map(is_, pk_values, repeat(None)))
        ):
            raise QueryError("All bulk_update() objects must have a primary key set.")
        # Two objects with one primary key would be two VALUES rows for one row - which of them is
        # written is unspecified.
        if len(set(pk_values)) != len(pk_values):
            duplicate_pks: list[Any] = []
            pks_seen: set[Any] = set()
            for pk in pk_values:
                if pk in pks_seen and pk not in duplicate_pks:
                    duplicate_pks.append(pk)
                pks_seen.add(pk)
            raise QueryError(
                f"bulk_update() on {model.__name__} received multiple objects with the "
                f"same primary key in a single call - each target row can only be updated once "
                f"per call: pk(s) {duplicate_pks}"
            )
        # The optimistic lock field is compared on every bulk_update() - it has to be loaded.
        if optimistic_lock_field := model._meta.optimistic_lock_field:
            missing_version = [obj.pk for obj in objects if not hasattr(obj, optimistic_lock_field)]
            if missing_version:
                raise IncompleteInstanceError(
                    f"{model.__name__} is a partial model, Meta.optimistic_lock_field {optimistic_lock_field!r} "
                    f"is not available - every bulk_update() needs to read it for the staleness check, "
                    f"even when only updating other fields: pk(s) {missing_version}"
                )
