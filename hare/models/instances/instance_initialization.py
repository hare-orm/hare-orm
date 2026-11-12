from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from copy import deepcopy
from typing import TYPE_CHECKING, Any, NoReturn

from hare.classes.value_descriptions import ValueDescriptions
from hare.core.caching.model_cache import ModelCache
from hare.exceptions import FieldError, QueryError
from hare.fields.field import Field
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.models.deletion.soft_deletion import SoftDeletion
from hare.models.write.rules.constants import GENERATED_REASON, REFUSED_FIELD_MESSAGE
from hare.native.native_modules import NativeModules
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.expressions.expression import Expression

if TYPE_CHECKING:
    from hare.models.model import Model


class InstanceInitialization:
    """How a new instance takes its constructor's arguments: a composite primary key given as pk spread
    over its fields, a relation and its key columns checked to agree, every keyword set, and every
    field left out given its default. The model's constructor (``get_constructor()``) assigns them; the
    special cases it hands over are the functions here."""

    @staticmethod
    @ModelCache.fact()
    def get_constructor(model: type[Model]) -> Any:
        """The constructor of ``model``'s instances (``rust.native.rows.InstanceConstructor``) - how its
        keywords and defaults are assigned, a fact of the model; the values are the construction's own.

        Args:
            model: The model.

        Returns:
            The constructor.
        """
        # Local import: the model module imports this one.
        from hare.models.model import Model

        meta = model._meta
        relations = meta.foreign_key_fields | meta.one_to_one_fields
        generated_pk_field_name = meta.generated_pk_field_name
        fields = [
            (
                name,
                field,
                name in relations,
                frozenset() if name in relations else field.get_assign_normalized_types(),
                field.null,
                name == generated_pk_field_name,
                name == meta.soft_delete_field,
            )
            for name, field in meta.fields_map.items()
            if name in relations or name in meta.fields_db_projection
        ]
        automatic_keys = tuple(
            name
            for name, field in meta.fields_map.items()
            if field.pk and (field.generated or field.default is not None or field.has_db_default())
        )
        # A new instance has nothing for Model.__setattr__ to refresh on a plain column; a relation, an
        # automatic primary key and a model overriding __setattr__ still go through it.
        sets_columns_directly = model.__setattr__ is Model.__setattr__
        defaults = []
        # A fetch field is filled by its own query; a field the database computes holds no value
        # until a row of the instance is read.
        for name in meta.fields - meta.computed_field_names - meta.fetch_fields:
            field = meta.fields_map[name]
            field_default = field.default
            has_db_default = field.has_db_default()
            normalized_types = field.get_assign_normalized_types()
            # An automatic primary key left None hands the key to the database - Model.__setattr__ has nothing
            # to clear on a new instance then; a default value of it goes through Model.__setattr__.
            sets_directly = (
                sets_columns_directly
                and name in meta.fields_db_projection
                and (name != generated_pk_field_name or (field_default is None and not has_db_default))
            )
            is_static = (field_default is None and not has_db_default) or (
                not callable(field_default)
                and isinstance(field_default, (int, float, str, bool, bytes))
                and type(field_default) in normalized_types
            )
            defaults.append(
                (
                    name,
                    field,
                    object.__setattr__ if sets_directly else setattr,
                    sets_directly,
                    has_db_default,
                    normalized_types,
                    is_static,
                    field_default if is_static else None,
                )
            )
        return NativeModules.rows.InstanceConstructor(
            model, fields, automatic_keys, defaults, sets_columns_directly, Expression, InstanceInitialization
        )

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
        InstanceInitialization.assign_field_default(
            obj,
            key,
            field_object,
            set_value,
            field_object.has_db_default(),
            field_object.get_assign_normalized_types(),
        )

    @staticmethod
    def assign_field_default(
        obj: Model,
        key: str,
        field_object: Field[Any],
        set_value: Callable[[Any, str, Any], None],
        has_db_default: bool,
        normalized_types: frozenset[type],
    ) -> None:
        """Sets a field the constructor wasn't given a value for to its default.

        Args:
            obj: The model obj.
            key: The field name.
            field_object: The field.
            set_value: How the value is set.
            has_db_default: Whether the field has a ``db_default``.
            normalized_types: The types a default is assigned as it is, without ``to_python()``.
        """
        field_default = field_object.default
        if field_default is None:
            set_value(obj, key, field_object.get_db_default_value() if has_db_default else None)
        elif field_object._default_is_coroutine:
            InstanceInitialization.add_pending_default(obj, key, field_default)
        elif callable(field_default):
            value = field_default()
            if value is not None and type(value) not in normalized_types:
                value = field_object.get_default_value_on_assign(value)
            set_value(obj, key, value)
        elif field_object.get_binding().static_default_is_normalized is not True:
            set_value(obj, key, field_object.get_static_default_value())
        elif isinstance(field_default, (int, float, str, bool, bytes)):
            set_value(obj, key, field_default)
        else:
            set_value(obj, key, deepcopy(field_default))

    @staticmethod
    def set_constructed_defaults(obj: Model, field_names: Iterable[str]) -> None:
        """Gives the fields ``Model.construct()`` got no value for their defaults, without a
        database or an event loop: an async default is left as None.

        Synchronous defaults use the constructor's normalization and copying rules.

        Args:
            obj: The obj being constructed.
            field_names: The fields left out.
        """
        fields_map = obj._meta.fields_map
        _setattr = object.__setattr__
        for key in field_names:
            if fields_map[key]._default_is_coroutine:
                _setattr(obj, key, None)
            else:
                InstanceInitialization.assign_default(obj, key, _setattr)

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
                    f"Conflicting values for {relation_field.model_field_name!r} ({expected_value!r}) "
                    f"and {source_field!r} ({given_value!r}) - pass only one of them, or matching values"
                )

    @staticmethod
    def expand_pk_kwarg(model: type[Model], kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """Expands a ``pk`` kwarg into the real primary-key field name(s) it aliases, the same
        way ``.filter(pk=...)`` already resolves it for reads.

        Args:
            model: The model.
            kwargs: Raw constructor/create kwargs, containing ``pk``.

        Returns:
            A new dict with ``pk`` replaced by the real pk field name(s).

        Raises:
            QueryError: ``pk`` is given together with one of the real pk field names it
                aliases, or (for a composite pk) its value isn't a matching-length tuple.
        """
        meta = model._meta
        meta.raise_if_no_primary_key(f"{model.__name__}(pk=...)")
        pk_value = kwargs[PRIMARY_KEY_ALIAS]
        if meta.has_composite_primary_key:
            if not isinstance(pk_value, tuple) or len(pk_value) != len(meta.primary_key_attribute):
                raise QueryError(
                    f"Composite pk must be set to a {len(meta.primary_key_attribute)}-tuple matching "
                    f"{meta.primary_key_attribute}, got {ValueDescriptions.get_description(pk_value)}"
                )
            pk_kwargs = dict(zip(meta.primary_key_attribute, pk_value, strict=True))
        else:
            pk_kwargs = {meta.primary_key_attribute: pk_value}  # type: ignore[dict-item]
        for pk_field_name in pk_kwargs:
            if pk_field_name in kwargs:
                raise QueryError(f"Can't set both 'pk' and {pk_field_name!r} - they name the same field")
        expanded_kwargs = {key: value for key, value in kwargs.items() if key != PRIMARY_KEY_ALIAS}
        expanded_kwargs.update(pk_kwargs)
        return expanded_kwargs

    @staticmethod
    def get_kwargs_without_unset_automatic_pk(
        kwargs: dict[str, Any], automatic_keys: tuple[str, ...]
    ) -> dict[str, Any]:
        """Drops a primary key passed as ``None`` when the database generates it or the field has
        a ``default``/``db_default`` - "not assigned yet", so the generated or default value
        applies, and ``Model(**dict(obj))`` round-trips instead of rejecting the ``None``.

        Args:
            kwargs: Raw constructor/create kwargs.
            automatic_keys: The model's automatic primary-key fields.

        Returns:
            ``kwargs`` itself if nothing was dropped, otherwise a filtered copy.
        """
        for key in automatic_keys:
            if kwargs.get(key, True) is None:
                return {
                    field_key: field_value
                    for field_key, field_value in kwargs.items()
                    if not (field_value is None and field_key in automatic_keys)
                }
        return kwargs

    @staticmethod
    def check_relation_kwarg(
        obj: Model, relation_field: RelationalField[Any], key: str, value: Any, kwargs: dict[str, Any]
    ) -> tuple[str, ...]:
        """Checks a relation given to the constructor: its type, None only for a nullable one, and
        its source columns given too naming the same row.

        Args:
            obj: The model obj.
            relation_field: The FK/O2O field.
            key: Its name.
            value: The related obj, or None.
            kwargs: Every value given in the same call.

        Returns:
            The relation's source columns - given by it.

        Raises:
            QueryError: None for a relation that doesn't take it, or a source column given another value.
        """
        model = type(obj)
        RelationValues.validate_relation_type(model, key, value)
        if value is None and not relation_field.null:
            raise QueryError(f"{model.__name__}.{key} doesn't take None")
        InstanceInitialization.check_relation_kwargs_agree(obj, relation_field, value, kwargs)
        return relation_field.source_fields

    @staticmethod
    def check_soft_delete_kwarg(obj: Model, key: str) -> None:
        """Checks a value given to the soft-delete field (``SoftDeletion.check_soft_delete_write_allowed()``).

        Args:
            obj: The model obj.
            key: The soft-delete field.
        """
        SoftDeletion.check_soft_delete_write_allowed(obj, key)

    @staticmethod
    def raise_for_none(model: type[Model], key: str) -> NoReturn:
        """Raises for None given to a field that doesn't take it.

        Args:
            model: The model.
            key: The field.

        Raises:
            QueryError: Always.
        """
        raise QueryError(f"{model.__name__}.{key} doesn't take None")

    @staticmethod
    def take_callable_kwarg(
        obj: Model, key: str, value: Callable[[], Any], field_object: Field[Any]
    ) -> tuple[bool, Any]:
        """Takes a callable given as a field's value: an async one is awaited when the obj is saved,
        another is called for the value now.

        Args:
            obj: The model obj.
            key: The field name.
            value: The callable.
            field_object: The field.

        Returns:
            Whether there is a value to assign now, and the value.
        """
        if Field._is_async_default(value):
            InstanceInitialization.add_pending_default(obj, key, value)
            return False, None
        return True, field_object.to_python(value())

    @staticmethod
    def raise_for_computed_fields(model: type[Model], values: dict[str, Any]) -> NoReturn:
        """Raises for a value given to a field the database computes - it would never be written.

        Args:
            model: The model.
            values: The values given, by field name.

        Raises:
            QueryError: Always.
        """
        names = sorted(model._meta.computed_field_names.intersection(values))
        raise QueryError(
            REFUSED_FIELD_MESSAGE.format(name=names[0], method=f"{model.__name__}()", reason=GENERATED_REASON)
        )

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
        if key in meta.backward_foreign_key_fields or key in meta.backward_one_to_one_fields:
            raise QueryError(f"{type(obj).__name__}.{key} is a backward relation - set it on the related model")
        if key in meta.many_to_many_fields:
            raise QueryError(
                f"{type(obj).__name__}.{key} is a many-to-many relation - add its objects once the object is "
                f"saved: obj.{key}.add(...)"
            )
        raise FieldError(f"Unknown field {key!r} for model {meta.full_name}")
