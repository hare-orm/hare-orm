from __future__ import annotations

from collections.abc import Sequence
from itertools import repeat
from operator import attrgetter, is_
from typing import TYPE_CHECKING, Any

from hare.exceptions import IncompleteInstanceError, QueryError
from hare.fields.database_default import DatabaseDefault
from hare.fields.relations.fields.backward_foreign_key_relation import BackwardForeignKeyRelation
from hare.fields.relations.fields.declarations import BackwardOneToOneRelation
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.query.expressions import Expression

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models import Model


class BulkUpdateChecks:
    """What ``bulk_update()`` checks before it writes: objects with a key of their own each, fields
    that are columns it may write, and values it can bind."""

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
                f"tenant_field '{tenant_field}' still has an unresolved async default= "
                f"value: pk(s) {pending_pks}. Save or refresh these objects first, or set "
                f"'{tenant_field}' explicitly, before calling bulk_update()."
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
                    f"{model.__name__} is a partial model, Meta.optimistic_lock_field '{optimistic_lock_field}' "
                    f"is not available - every bulk_update() needs to read it for the staleness check, "
                    f"even when only updating other fields: pk(s) {missing_version}"
                )

    @staticmethod
    def check_fields(model: type[Model], fields: list[str]) -> None:
        """Raises for a field ``bulk_update()`` doesn't write: the soft-delete and optimistic lock
        fields, a generated field, and a relation without a column on the model's table.

        Args:
            model: The model.
            fields: The fields written.

        Raises:
            QueryError: A field can't be written.
        """
        meta = model._meta
        if meta.soft_delete_field in fields:
            raise QueryError(
                f"Cannot set '{meta.soft_delete_field}' via bulk_update() - use .delete()/.restore() instead"
            )
        if meta.optimistic_lock_field in fields:
            raise QueryError(
                f"Cannot set '{meta.optimistic_lock_field}' via bulk_update() - it's bumped automatically"
            )
        generated_fields = [field for field in fields if getattr(meta.fields_map.get(field), "generated", False)]
        if generated_fields:
            raise QueryError(
                f"bulk_update() on {model.__name__} can't target generated field(s) "
                f"{generated_fields} - they're computed by the database, not written to."
            )
        # A forward relation is written through its key column(s); a reverse or many-to-many
        # relation has no column on this table.
        unsupported_fields = [
            field
            for field in fields
            if isinstance(
                meta.fields_map.get(field),
                (ManyToManyFieldInstance, BackwardForeignKeyRelation, BackwardOneToOneRelation),
            )
        ]
        if unsupported_fields:
            raise QueryError(
                f"bulk_update() doesn't support relation field(s) {unsupported_fields} - they have no "
                "column on this model's own table to update."
            )

    @staticmethod
    def check_values(model: type[Model], objects: Sequence[Model], fields: list[str]) -> None:
        """Raises for a value ``bulk_update()`` can't bind. Every object of a statement writes the
        same columns - a field still holding ``DatabaseDefault`` can't be left out for one object -
        and every value is a parameter, which an expression can't be. A field waiting for an async
        default has no value yet; it is resolved before the write.

        Args:
            model: The model.
            objects: The objects written.
            fields: The fields written.

        Raises:
            QueryError: A value is ``DatabaseDefault`` or an expression.
        """
        # One pass over the values - each a plain one but for these two refusals.
        unset_db_default_fields: set[str] = set()
        expression_fields: set[str] = set()
        refused_types = (DatabaseDefault, Expression)
        for obj in objects:
            pending_defaults = obj._await_when_save
            for field in fields:
                if field in pending_defaults:
                    continue
                value = getattr(obj, field)
                if isinstance(value, refused_types):
                    if isinstance(value, DatabaseDefault):
                        unset_db_default_fields.add(field)
                    else:
                        expression_fields.add(field)
        if unset_db_default_fields:
            raise QueryError(
                f"bulk_update() on {model.__name__} received object(s) whose {sorted(unset_db_default_fields)} "
                "field(s) still hold their DatabaseDefault sentinel (never populated, e.g. via "
                "bulk_create(returning=False)) - give them a real value first, or exclude the field(s) from "
                "bulk_update()'s own fields= argument."
            )
        if expression_fields:
            raise QueryError(
                f"bulk_update() on {model.__name__} doesn't support F()/expression values, but "
                f"{sorted(expression_fields)} hold one - use QuerySet.update() to apply an expression to "
                "rows matching a filter, or save() on each object."
            )

    @staticmethod
    def get_auto_now_field_names(model: type[Model], objects: Sequence[Model], fields: list[str]) -> list[str]:
        """The ``auto_now`` fields ``bulk_update()`` writes besides the fields it is given - every
        one, whatever the fields name.

        Args:
            model: The model.
            objects: The objects written.
            fields: The fields given.

        Returns:
            The fields.

        Raises:
            IncompleteInstanceError: An object lacks an ``auto_now`` field.
        """
        auto_now_field_names = []
        for field_name, field_object in model._meta.fields_map.items():
            if getattr(field_object, "auto_now", False) and field_name not in fields:
                if not all(map(hasattr, objects, repeat(field_name))):
                    raise IncompleteInstanceError(
                        f"{model.__name__} is a partial model, auto_now field '{field_name}' is not "
                        "available - every bulk_update() needs to read and bump it, even when only "
                        "updating other fields"
                    )
                auto_now_field_names.append(field_name)
        return auto_now_field_names
