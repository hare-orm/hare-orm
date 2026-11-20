from __future__ import annotations

from copy import copy, deepcopy
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
    def get_state(source: Model, *, detached: bool = False) -> dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]:
        """Keep Python's dictionary/slot state, excluding live rollback callbacks from both."""
        state = object.__getstate__(source)
        if isinstance(state, tuple):
            attributes, slots = state
            attributes = dict(attributes or {})
            slots = dict(slots)
            attributes.pop("_pending_rollback_restore_stack", None)
            slots.pop("_pending_rollback_restore_stack", None)
            if detached:
                attributes.pop("_context", None)
                slots.pop("_context", None)
            return attributes, slots
        attributes = dict(state or {})  # type: ignore[call-overload]
        attributes.pop("_pending_rollback_restore_stack", None)
        if detached:
            attributes.pop("_context", None)
        return attributes

    @staticmethod
    def allocate(model: type[TModel]) -> TModel:
        """Allocate an unpickled instance without running constructors, defaults or application lookup."""
        return object.__new__(model)

    @staticmethod
    def set_state(obj: Model, state: dict[str, Any] | tuple[dict[str, Any], dict[str, Any]]) -> None:
        """Restore structural identity before dictionary and slot values, keeping the object detached."""
        attributes, slots = state if isinstance(state, tuple) else (state, {})
        binding = slots.get("_binding", attributes.get("_binding"))
        object.__setattr__(obj, "_binding", binding)
        object.__setattr__(obj, "_context", None)
        obj.__dict__.update(attributes)
        for name, value in slots.items():
            object.__setattr__(obj, name, value)

    @staticmethod
    def deep_copy(source: TModel, memo: dict[int, Any]) -> TModel:
        """Copy values and loaded relations while retaining their in-process binding and origin."""
        obj = object.__new__(type(source))
        memo[id(source)] = obj
        if source._context is not None:
            memo[id(source._context)] = source._context
        object.__setattr__(obj, "_binding", source._binding)
        object.__setattr__(obj, "_context", source._context)
        state = deepcopy(InstanceCopies.get_state(source), memo)
        InstanceCopies.set_state(obj, state)
        return obj

    @staticmethod
    def copy(source: TModel, *, for_insert: bool = False) -> TModel:
        """Copy fields and pending defaults while detaching rollback and reverse caches.

        Forward related objects retain identity. Mutable field values use the normal
        field snapshot contract; unloaded fields remain unloaded. A copy cannot retain
        live transaction callbacks belonging to its source instance. Preparing an insert skips
        computed field values and the dirty baseline which the clone discards.
        """
        cls = type(source)
        obj = object.__new__(cls)
        state = InstanceCopies.get_state(source)
        attributes, slots = state if isinstance(state, tuple) else (state, {})
        obj.__dict__.update(attributes)
        for name, value in slots.items():
            object.__setattr__(obj, name, value)
        pending_defaults = slots.get("_await_when_save", attributes.get("_await_when_save"))
        if pending_defaults is not None:
            object.__setattr__(obj, "_await_when_save", dict(pending_defaults))
        if not for_insert and source._dirty_snapshot is not None:
            object.__setattr__(obj, "_dirty_snapshot", dict(source._dirty_snapshot))
        meta = source._binding
        for field_name in meta.direct_fields:
            if for_insert and field_name in meta.computed_field_names:
                continue
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
        from hare.models.model import Model

        obj = InstanceCopies.copy(source, for_insert=True) if type(source).__copy__ is Model.__copy__ else copy(source)
        meta = source._binding
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
            # A relation default supplies the entire missing key once. Remove the
            # old components before assigning any fresh default, as construction does.
            pending_defaults = obj.__dict__.get("_await_when_save")
            for name in meta.primary_key_attribute:
                try:
                    object.__delattr__(obj, name)
                except AttributeError:
                    pass
                if pending_defaults is not None:
                    pending_defaults.pop(name, None)
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
                f"{obj._binding.full_name} requires an explicit value for composite primary key "
                f"component {field_name!r}. Please use .clone(pk=(<value>, ...))"
            )
        relation = getattr(field, "reference", None)
        if relation is not None and len(relation.source_fields) > 1 and callable(relation.default):
            InstanceInitialization.assign_relation_default(obj, relation, setattr)
            return
        if field._default_is_coroutine:
            # After the assignment: __setattr__ pops the pending default of an assigned field.
            setattr(obj, field_name, None)
        InstanceInitialization.assign_default(obj, field_name, setattr)
