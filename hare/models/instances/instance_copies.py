from __future__ import annotations

from copy import copy
from typing import TYPE_CHECKING, Any, TypeVar

from hare.exceptions import QueryError
from hare.fields.field import Field
from hare.models.constants import CLONE_PK_NOT_GIVEN
from hare.models.instances.field_snapshot import FieldSnapshot
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows

if TYPE_CHECKING:
    from hare.models.model import Model

TModel = TypeVar("TModel", bound="Model")


class InstanceCopies:
    """Structural copies and new-row clones, with isolated mutable and transaction state."""

    @staticmethod
    def get_state(source: Model) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
        """Keep Python's dictionary/slot state, excluding live rollback callbacks from both."""
        state = object.__getstate__(source)
        if isinstance(state, tuple):
            attributes, slots = state
            attributes = dict(attributes or {})
            slots = dict(slots)
            attributes.pop("_pending_rollback_restore_stack", None)
            slots.pop("_pending_rollback_restore_stack", None)
            return attributes, slots
        attributes = dict(state or {})  # type: ignore[call-overload]
        attributes.pop("_pending_rollback_restore_stack", None)
        return attributes

    @staticmethod
    def copy(source: TModel) -> TModel:
        """Copy fields and pending defaults while detaching rollback and reverse caches.

        Forward related objects retain identity. Mutable field values use the normal
        field snapshot contract; unloaded fields remain unloaded. A copy cannot retain
        live transaction callbacks belonging to its source instance.
        """
        cls = type(source)
        obj = cls.__new__(cls)
        state = InstanceCopies.get_state(source)
        attributes, slots = state if isinstance(state, tuple) else (state, {})
        obj.__dict__.update(attributes)
        for name, value in slots.items():
            object.__setattr__(obj, name, value)
        pending_defaults = slots.get("_await_when_save", attributes.get("_await_when_save"))
        if pending_defaults is not None:
            object.__setattr__(obj, "_await_when_save", dict(pending_defaults))
        if source._dirty_snapshot is not None:
            object.__setattr__(obj, "_dirty_snapshot", dict(source._dirty_snapshot))
        meta = source._meta
        for field_name in meta.direct_fields:
            if field_name in attributes or field_name in slots:
                value = getattr(obj, field_name)
                copied_value = FieldSnapshot.copy_value(value)
                if copied_value is not value:
                    object.__setattr__(obj, field_name, copied_value)
        for name in (*meta.backward_foreign_key_fields, *meta.many_to_many_fields):
            cache_name = f"_{name}"
            if cache_name in attributes or cache_name in slots:
                object.__setattr__(obj, cache_name, RelationRows())
        for name in meta.backward_one_to_one_fields:
            cache_name = f"_{name}"
            if cache_name in attributes or cache_name in slots:
                object.__delattr__(obj, cache_name)
        return obj

    @staticmethod
    def clone(source: TModel, pk: Any = CLONE_PK_NOT_GIVEN) -> TModel:
        """Make an unsaved copy with fresh creation defaults and no computed values.

        Mark the clone unsaved before assigning its key, so an explicit generated key
        keeps the normal insertion semantics. A clone retains its source connection
        preference and loaded-field coverage, but no transaction bookkeeping.
        """
        obj = copy(source)
        meta = source._meta
        object.__setattr__(obj, "_dirty_snapshot", None)
        object.__setattr__(obj, "_saved_in_db", False)
        for field_name, field in meta.fields_map.items():
            if getattr(field, "auto_now_add", False):
                setattr(obj, field_name, None)
        for field_name in meta.computed_field_names:
            try:
                object.__delattr__(obj, field_name)
            except AttributeError:
                pass
        if meta.soft_delete_field is not None:
            setattr(obj, meta.soft_delete_field, None)
        if not meta.has_primary_key:
            if pk is not CLONE_PK_NOT_GIVEN:
                meta.raise_if_no_primary_key(f"{type(source).__name__}.clone(pk=...)")
            return obj
        if pk is not CLONE_PK_NOT_GIVEN:
            obj.pk = pk
        elif meta.has_composite_primary_key:
            for name, field in zip(meta.primary_key_attribute, meta.pk_fields, strict=True):
                InstanceCopies.apply_clone_pk_component_default(obj, name, field)
        else:
            pk_field = meta.pk
            if pk_field.generated is not False:
                obj.pk = None
            elif pk_field.default is None and not pk_field.has_db_default():
                raise QueryError(f"{meta.full_name} requires explicit primary key. Please use .clone(pk=<value>)")
            else:
                InstanceCopies.apply_clone_pk_component_default(obj, meta.primary_key_attribute, pk_field)  # type: ignore[arg-type]
        return obj

    @staticmethod
    def apply_clone_pk_component_default(obj: Model, field_name: str, field: Field[Any]) -> None:
        """Gives a component of the copy's composite primary key its default again - the field's
        ``default`` (a coroutine one awaited on save), else its ``db_default``.

        Args:
            obj: The copy.
            field_name: The component's attribute name.
            field: The component's field.

        Raises:
            QueryError: The field has neither a ``default`` nor a ``db_default``.
        """
        # A component of a composite primary key is never generated by the database - the key refuses
        # one when the model is defined.
        if field.default is None and not field.has_db_default():
            raise QueryError(
                f"{obj._meta.full_name} requires an explicit value for composite primary key "
                f"component {field_name!r}. Please use .clone(pk=(<value>, ...))"
            )
        if field._default_is_coroutine:
            # After the assignment: __setattr__ pops the pending default of an assigned field.
            setattr(obj, field_name, None)
        InstanceInitialization.assign_default(obj, field_name, setattr)
