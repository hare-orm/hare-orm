from __future__ import annotations

from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import TYPE_CHECKING, Any, NoReturn, cast

from hare.exceptions import FieldError, QueryError
from hare.fields.field import Field
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.deletion.soft_deletion import SoftDeletion
from hare.query.expressions.expression import Expression

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.models.model import Model


class InstanceInitialization:
    """How a new instance takes its constructor's arguments: a composite primary key given as pk spread
    over its fields, a relation and its key columns checked to agree, every keyword set, and every
    field left out given its default."""

    @staticmethod
    def add_pending_default(obj: Model, field_name: str, default: Any) -> None:
        """Keeps an async default to be awaited when the obj is saved - in a dict of the
        obj's own, made with its first one.

        Args:
            obj: The obj.
            field_name: The field.
            default: The async default.
        """
        pending_defaults = obj.__dict__.get("_await_when_save")
        if pending_defaults is None:
            pending_defaults = {}
            # Not a field - set past the overridden __setattr__.
            object.__setattr__(obj, "_await_when_save", pending_defaults)
        pending_defaults[field_name] = default

    @staticmethod
    def assign_default(obj: Model, key: str, set_value: Callable[[Any, str, Any], None]) -> None:
        """Sets a field the constructor wasn't given a value for to its default.

        Args:
            obj: The model obj.
            key: The field name.
            set_value: How the value is set - ``object.__setattr__`` for a plain column of the new
                obj, else ``setattr``.
        """
        field_object = obj._meta.fields_map[key]
        field_default = field_object.default
        if field_object._default_is_coroutine:
            InstanceInitialization.add_pending_default(obj, key, field_default)
        elif callable(field_default):
            value = field_default()
            normalized_types = field_object.assign_normalized_types
            if normalized_types is None or (value is not None and type(value) not in normalized_types):
                value = field_object.get_default_value_on_assign(value)
            set_value(obj, key, value)
        elif field_default is not None:
            # A default already in its assigned form (checked once per field) skips the to_python
            # call - the common case stays a plain assignment.
            if field_object.static_default_is_normalized is not True:
                set_value(obj, key, field_object.get_static_default_value())
            elif isinstance(field_default, (int, float, str, bool, bytes)):
                set_value(obj, key, field_default)
            else:
                set_value(obj, key, deepcopy(field_default))
        elif field_object.has_db_default():
            set_value(obj, key, field_object.get_db_default_value())
        else:
            set_value(obj, key, None)

    @staticmethod
    def set_constructed_defaults(obj: Model, field_names: Iterable[str]) -> None:
        """Gives the fields ``Model.construct()`` got no value for their defaults, without a
        database or an event loop: an async default is left as None.

        Args:
            obj: The obj being constructed.
            field_names: The fields left out.
        """
        fields_map = obj._meta.fields_map
        _setattr = object.__setattr__
        for key in field_names:
            default_field = fields_map[key]
            field_default = default_field.default
            if default_field._default_is_coroutine:
                _setattr(obj, key, None)
            elif callable(field_default):
                _setattr(obj, key, default_field.get_default_value_on_assign(field_default()))
            elif field_default is not None:
                # Copied and normalized as in __init__ - a shared literal default
                # (JSONField(default={"a": 1})) would otherwise be one object across every obj.
                _setattr(obj, key, default_field.get_static_default_value())
            elif default_field.has_db_default():
                _setattr(obj, key, default_field.get_db_default_value())
            else:
                _setattr(obj, key, None)

    @staticmethod
    def check_relation_kwargs_agree(
        obj: Model, relation_field: RelationalField[Any], value: Any, kwargs: dict[str, Any]
    ) -> None:
        """
        Checks that a relation passed together with its own source column(s) names the same row.

        Args:
            obj: The model obj.
            relation_field: The FK/O2O field being assigned.
            value: The related obj (or ``None``) passed for ``relation_field``.
            kwargs: Every value being assigned in the same call.

        Raises:
            QueryError: If a source column in ``kwargs`` holds a different value than the relation.
        """
        for source_field, to_field_instance in zip(
            relation_field.source_fields, relation_field.to_field_instances, strict=True
        ):
            if source_field not in kwargs:
                continue
            expected_value = None if value is None else getattr(value, to_field_instance.model_field_name)
            given_value = obj._meta.fields_map[source_field].to_python(kwargs[source_field])
            if given_value != expected_value:
                raise QueryError(
                    f"Conflicting values for '{relation_field.model_field_name}' ({expected_value!r}) "
                    f"and '{source_field}' ({given_value!r}) - pass only one of them, or matching values"
                )

    @staticmethod
    def expand_pk_kwarg(obj: Model, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Expands a ``pk`` kwarg into the real primary-key field name(s) it aliases, the same
        way ``.filter(pk=...)`` already resolves it for reads.

        Args:
            obj: The model obj.
            kwargs: Raw constructor/create kwargs, containing ``pk``.

        Returns:
            A new dict with ``pk`` replaced by the real pk field name(s).

        Raises:
            QueryError: ``pk`` is given together with one of the real pk field names it
                aliases, or (for a composite pk) its value isn't a matching-length tuple.
        """
        meta = obj._meta
        pk_value = kwargs["pk"]
        if meta.has_composite_primary_key:
            if not isinstance(pk_value, tuple) or len(pk_value) != len(meta.primary_key_attribute):
                raise QueryError(
                    f"Composite pk must be set to a {len(meta.primary_key_attribute)}-tuple matching "
                    f"{meta.primary_key_attribute}, got {pk_value!r}"
                )
            pk_kwargs = dict(zip(meta.primary_key_attribute, pk_value, strict=True))
        else:
            pk_kwargs = {cast("str", meta.primary_key_attribute): pk_value}
        for pk_field_name in pk_kwargs:
            if pk_field_name in kwargs:
                raise QueryError(f"Can't set both 'pk' and '{pk_field_name}' - they name the same field")
        expanded_kwargs = {key: value for key, value in kwargs.items() if key != "pk"}
        expanded_kwargs.update(pk_kwargs)
        return expanded_kwargs

    @staticmethod
    def get_kwargs_without_unset_automatic_pk(obj: Model, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Drops a primary key passed as ``None`` when the database generates it or the field has
        a ``default``/``db_default`` - "not assigned yet", so the generated or default value
        applies, and ``Model(**dict(obj))`` round-trips instead of rejecting the ``None``.

        Args:
            obj: The model obj.
            kwargs: Raw constructor/create kwargs.

        Returns:
            ``kwargs`` itself if nothing was dropped, otherwise a filtered copy.
        """
        fields_map = obj._meta.fields_map
        unset_keys = [
            key
            for key, value in kwargs.items()
            if value is None
            and key in fields_map
            and fields_map[key].pk
            and (fields_map[key].generated or fields_map[key].default is not None or fields_map[key].has_db_default())
        ]
        if not unset_keys:
            return kwargs
        return {key: value for key, value in kwargs.items() if key not in unset_keys}

    @staticmethod
    def set_kwargs(obj: Model, kwargs: dict[str, Any], *, assigns_directly: bool = False) -> set[str]:
        """Validates, converts and assigns field values given by name.

        Args:
            obj: The model obj.
            kwargs: The values, by field name (``pk`` included).
            assigns_directly: Whether a value may skip ``Model.__setattr__`` - for a new obj (nothing
                cached, pending or saved on it yet to refresh) of a model keeping ``Model.__setattr__``.

        Returns:
            The names of the fields given, a relation's source columns included.
        """
        meta = obj._meta
        if meta.generic_foreign_key_fields:
            kwargs = GenericForeignKeys.expand_kwargs(meta, kwargs, apply_defaults=False)
        if "pk" in kwargs:
            kwargs = InstanceInitialization.expand_pk_kwarg(obj, kwargs)
        kwargs = InstanceInitialization.get_kwargs_without_unset_automatic_pk(obj, kwargs)

        # Assign values and do type conversions
        passed_fields = {*kwargs.keys()} | meta.fetch_fields

        # Every value is validated and converted before the first is assigned - a failure leaves the
        # obj untouched. Relations are assigned after the plain fields.
        direct_assignments: list[tuple[str, Any]] = []
        relation_assignments: list[tuple[str, Any]] = []

        for key, value in kwargs.items():
            if key in meta.foreign_key_fields or key in meta.one_to_one_fields:
                relation_field = cast("RelationalField[Any]", meta.fields_map[key])
                RelationValues.validate_relation_type(type(obj), key, value)
                if value is None and not relation_field.null:
                    raise QueryError(f"{key} is non nullable field, but null was passed")
                InstanceInitialization.check_relation_kwargs_agree(obj, relation_field, value, kwargs)
                relation_assignments.append((key, value))
                passed_fields.update(relation_field.source_fields)
            elif key in meta.fields_db_projection:
                field_object = meta.fields_map[key]
                if key == meta.soft_delete_field:
                    SoftDeletion.check_soft_delete_write_allowed(obj, key)
                normalized_types = field_object.assign_normalized_types
                if normalized_types is None:
                    normalized_types = field_object.assign_normalized_types = (
                        field_object.get_assign_normalized_types()
                    )
                if type(value) in normalized_types or isinstance(value, Expression):
                    # Already the type to_python() returns unchanged (and not a callable) - the
                    # common case, assigned as it is. An expression isn't converted either, as in a
                    # plain `obj.field = F(...)` assignment: the write applies it.
                    direct_assignments.append((key, value))
                elif callable(value):
                    InstanceInitialization.take_callable_kwarg(obj, key, value, field_object, direct_assignments)
                else:
                    if value is None and not field_object.null:
                        raise QueryError(f"{key} is non nullable field, but null was passed")
                    direct_assignments.append((key, field_object.to_python(value)))
            else:
                InstanceInitialization.raise_for_unsettable_kwarg(obj, key)

        # A new obj has no cached related object, pending async default or saved state for
        # Model.__setattr__ to refresh on a plain column - only an automatic primary key keeps its
        # own bookkeeping there.
        generated_pk_field_name = meta.generated_pk_field_name
        for key, value in direct_assignments:
            if assigns_directly and key != generated_pk_field_name:
                object.__setattr__(obj, key, value)
            else:
                setattr(obj, key, value)
        for key, value in relation_assignments:
            setattr(obj, key, value)

        return passed_fields

    @staticmethod
    def take_callable_kwarg(
        obj: Model, key: str, value: Callable[[], Any], field_object: Field[Any], assignments: list[tuple[str, Any]]
    ) -> None:
        """Takes a callable given as a field's value: an async one is awaited when the obj is saved,
        another is called for the value now.

        Args:
            obj: The model obj.
            key: The field name.
            value: The callable.
            field_object: The field.
            assignments: The values to assign, by field name - added to.
        """
        if Field._is_async_default(value):
            InstanceInitialization.add_pending_default(obj, key, value)
        else:
            assignments.append((key, field_object.to_python(value())))

    @staticmethod
    def raise_for_unsettable_kwarg(obj: Model, key: str) -> NoReturn:
        """Raises for a keyword that names no field a constructor can set.

        Args:
            obj: The model obj.
            key: The keyword.

        Raises:
            QueryError: It names a reverse or a many-to-many relation.
            FieldError: It names no field.
        """
        meta = obj._meta
        if key in meta.backward_foreign_key_fields:
            raise QueryError("You can't set backward relations through init, change related model instead")
        if key in meta.backward_one_to_one_fields:
            raise QueryError("You can't set backward one to one relations through init, change related model instead")
        if key in meta.many_to_many_fields:
            raise QueryError("You can't set m2m relations through init, use m2m_manager instead")
        raise FieldError(f"Unknown field '{key}' for model {meta.full_name}")
