from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from contextvars import ContextVar
from copy import deepcopy
from typing import TYPE_CHECKING, Any, ClassVar, NoReturn

from hare.core.caching.model_cache import ModelCache
from hare.exceptions import FieldError, QueryError
from hare.fields.field import Field
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.models.deletion.soft_deletion import SoftDeletion
from hare.models.write.declarations import PendingRelationDefault
from hare.models.write.rules.constants import GENERATED_REASON, REFUSED_FIELD_MESSAGE
from hare.native.native_modules import NativeModules
from hare.query.constants import PRIMARY_KEY_ALIAS
from hare.query.expressions.expression import Expression

if TYPE_CHECKING:
    from hare.core.hare_context import HareContext
    from hare.models.model import Model
    from hare.models.model_binding import ModelBinding


class InstanceInitialization:
    """How a new instance takes its constructor's arguments: a composite primary key given as pk spread
    over its fields, a relation and its key columns checked to agree, every keyword set, and every
    field left out given its default. The model's constructor (``get_constructor()``) assigns them; the
    special cases it hands over are the functions here."""

    #: One synchronous construction's graph, also used by models a custom constructor creates.
    #: The token is always restored before returning; layouts never retain this execution input.
    construction: ClassVar[ContextVar[tuple[ModelBinding, HareContext | None] | None]] = ContextVar(
        "hare_model_construction", default=None
    )
    copied_values: ClassVar[ContextVar[tuple[Model, dict[str, Any]] | None]] = ContextVar(
        "hare_copied_model_values", default=None
    )

    @staticmethod
    def allocate(
        binding: ModelBinding,
        context: HareContext | None,
        *,
        initialize: bool = False,
        values: dict[str, Any] | None = None,
        copied: dict[str, Any] | None = None,
    ) -> Model:
        """Allocate through the current custom __new__, establishing origin before initialization.

        Native hydration uses allocation alone. An ordinary factory additionally runs __init__
        in this same scope, so nested model construction takes the explicitly selected graph.
        A constructor's matching origin is retained; an absent origin is installed, while
        an instance from another graph or context is rejected.
        """
        if initialize and context is not None:
            context.check_open()
        token = InstanceInitialization.construction.set((binding, context))
        try:
            model = binding._model
            if values is None:
                obj = model.__new__(model)
            else:
                obj = model.__new__(model, **values)
            previous = getattr(obj, "_binding", None)
            if previous is None:
                object.__setattr__(obj, "_binding", binding)
                object.__setattr__(obj, "_context", context)
            elif previous is not binding or obj._context is not context:
                raise QueryError("A model constructor returned an instance from another graph or context")
            if initialize:
                copied_token = InstanceInitialization.copied_values.set((obj, copied)) if copied else None
                try:
                    model.__init__(obj, **({} if values is None else values))
                finally:
                    if copied_token is not None:
                        InstanceInitialization.copied_values.reset(copied_token)
            return obj
        finally:
            InstanceInitialization.construction.reset(token)

    @staticmethod
    def new(binding: ModelBinding, context: HareContext | None, **values: Any) -> Model:
        """Run the ordinary constructor under an explicit registry and application, including offline use."""
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(context):
            return InstanceInitialization.allocate(binding, context, initialize=True, values=values)

    @staticmethod
    def new_from_copied_values(
        binding: ModelBinding, context: HareContext | None, copied: dict[str, Any], values: dict[str, Any]
    ) -> Model:
        """Construct a row with copied Python values and newly supplied values."""
        from hare.query.execution.query_execution import QueryExecution

        with QueryExecution(context):
            return InstanceInitialization.allocate(
                binding, context, initialize=True, values=copied | values, copied=copied
            )

    @staticmethod
    @ModelCache.fact()
    def get_constructor(binding: ModelBinding) -> Any:
        """The constructor of ``model``'s instances (``rust.native.rows.InstanceConstructor``) - how its
        keywords and defaults are assigned, a fact of the model; the values are the construction's own.

        Args:
            binding: The model.

        Returns:
            The constructor.
        """
        # Local import: the model module imports this one.
        from hare.models.model import Model

        model = binding._model
        relations = binding.forward_key_relation_names
        relation_defaults: dict[str, RelationalField[Any]] = {}
        for name in relations:
            relation: RelationalField[Any] = binding.fields_map[name]  # type: ignore[assignment]
            if len(relation.source_fields) > 1 and callable(relation.default):
                relation_defaults[name] = relation
        relation_default_columns = {name for field in relation_defaults.values() for name in field.source_fields}
        generated_pk_field_name = binding.generated_pk_field_name
        fields = [
            (
                name,
                field,
                name in relations,
                frozenset() if name in relations else field.get_assign_normalized_types(),
                field.null,
                name == generated_pk_field_name,
                name == binding.soft_delete_field,
            )
            for name, field in binding.fields_map.items()
            if name in relations or name in binding.fields_db_projection
        ]
        automatic_keys = tuple(
            name for name, field in binding.fields_map.items() if field.pk and field.has_insert_default()
        )
        # A new instance has nothing for Model.__setattr__ to refresh on a plain column; a relation, an
        # automatic primary key and a model overriding __setattr__ still go through it.
        sets_columns_directly = model.__setattr__ is Model.__setattr__
        defaults = []
        # A fetch field is filled by its own query; a field the database computes holds no value
        # until a row of the instance is read.
        for name in binding.fields - binding.computed_field_names - binding.fetch_fields - relation_default_columns:
            field = binding.fields_map[name]
            field_default = field.default
            has_db_default = field.has_db_default()
            normalized_types = field.get_assign_normalized_types()
            # An automatic primary key left None hands the key to the database - Model.__setattr__ has nothing
            # to clear on a new instance then; a default value of it goes through Model.__setattr__.
            sets_directly = (
                sets_columns_directly
                and name in binding.fields_db_projection
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
        defaults.extend(
            (name, field, setattr, False, False, frozenset(), False, None) for name, field in relation_defaults.items()
        )
        return NativeModules.rows.InstanceConstructor(
            binding, fields, automatic_keys, defaults, sets_columns_directly, Expression, InstanceInitialization
        )

    @staticmethod
    def add_pending_default(obj: Model, field_name: str, default: Any) -> None:
        """Keeps an async default to be awaited when the obj is saved - in a dict of the
        obj's own, made with its first one.

        Args:
            obj: The obj.
            field_name: The field.
            default: The async callable or a shared pending relation default.
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
        field_object = obj._binding.fields_map[key]
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
        if (
            isinstance(field_object, RelationalField)
            and len(field_object.source_fields) > 1
            and callable(field_object.default)
        ):
            InstanceInitialization.assign_relation_default(obj, field_object, set_value)
            return
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
    def assign_relation_default(
        obj: Model, relation: RelationalField[Any], set_value: Callable[[Any, str, Any], None]
    ) -> None:
        """Evaluate one composite relation default for its missing columns only.

        Explicit values and explicit pending column callables take precedence. An async
        factory is retained once and shared by the column entries, so replacing a column
        later cancels only that column's pending value.
        """
        missing = tuple(
            name for name in relation.source_fields if not hasattr(obj, name) and name not in obj._await_when_save
        )
        if not missing:
            return
        if relation._default_is_coroutine:
            pending = PendingRelationDefault(relation.model_field_name, relation.default)
            for name in missing:
                InstanceInitialization.add_pending_default(obj, name, pending)
        else:
            InstanceInitialization.assign_relation_default_value(
                obj, relation.model_field_name, relation.default(), missing, set_value
            )

    @staticmethod
    def assign_relation_default_value(
        obj: Model,
        relation_name: str,
        value: Any,
        field_names: Collection[str],
        set_value: Callable[[Any, str, Any], None] = setattr,
    ) -> None:
        """Distribute one returned relation key through the fields' default normalization."""
        relation: RelationalField[Any] = obj._binding.fields_map[relation_name]  # type: ignore[assignment]
        for position, name in enumerate(relation.source_fields):
            if name in field_names:
                field = obj._binding.fields_map[name]
                component = RelationValues.get_default_component(value, position)
                set_value(obj, name, field.get_default_value_on_assign(component))

    @staticmethod
    def set_constructed_defaults(obj: Model, field_names: Iterable[str]) -> None:
        """Gives the fields ``Model.construct()`` got no value for their defaults, without a
        database or an event loop: an async default is left as None.

        Synchronous defaults use the constructor's normalization and copying rules.

        Args:
            obj: The obj being constructed.
            field_names: The fields left out.
        """
        fields_map = obj._binding.fields_map
        _setattr = object.__setattr__
        for key in field_names:
            field = fields_map[key]
            relation = getattr(field, "reference", None)
            if field._default_is_coroutine:
                _setattr(obj, key, None)
            elif relation is not None and len(relation.source_fields) > 1 and callable(relation.default):
                InstanceInitialization.assign_relation_default(obj, relation, _setattr)
            else:
                InstanceInitialization.assign_default(obj, key, _setattr)

    @staticmethod
    def check_relation_kwargs_agree(
        obj: Model, relation_field: RelationalField[Any], key_values: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> None:
        """
        Checks that a relation passed together with its own source column(s) names the same row.

        Args:
            obj: The model obj.
            relation_field: The FK/O2O field being assigned.
            key_values: The already checked referenced key, including NULL components.
            kwargs: Every value being assigned in the same call.

        Raises:
            QueryError: If a source column in ``kwargs`` holds a different value than the relation.
        """
        for source_field, to_field_instance, expected_value in zip(
            relation_field.source_fields, relation_field.to_field_instances, key_values, strict=True
        ):
            if source_field not in kwargs:
                continue
            given_value = obj._binding.fields_map[source_field].to_python(kwargs[source_field])
            if given_value != expected_value:
                message_field = (
                    relation_field
                    if relation_field.sensitive
                    else (to_field_instance if to_field_instance.sensitive else obj._binding.fields_map[source_field])
                )
                raise QueryError(
                    f"Conflicting values for {relation_field.model_field_name!r} "
                    f"({message_field.get_value_for_message(expected_value)}) and {source_field!r} "
                    f"({message_field.get_value_for_message(given_value)}) - pass only one of them, or matching values"
                )

    @staticmethod
    def expand_pk_kwarg(binding: ModelBinding, kwargs: Mapping[str, Any]) -> dict[str, Any]:
        """Expands a ``pk`` kwarg into the real primary-key field name(s) it aliases, the same
        way ``.filter(pk=...)`` already resolves it for reads.

        Args:
            binding: The model.
            kwargs: Raw constructor/create kwargs, containing ``pk``.

        Returns:
            A new dict with ``pk`` replaced by the real pk field name(s).

        Raises:
            QueryError: ``pk`` is given together with one of the real pk field names it
                aliases, or (for a composite pk) its value isn't a matching-length tuple.
        """
        model = binding._model
        binding.raise_if_no_primary_key(f"{model.__name__}(pk=...)")
        pk_value = kwargs[PRIMARY_KEY_ALIAS]
        if binding.has_composite_primary_key:
            pk_kwargs = dict(binding.get_composite_primary_key_parts(pk_value))
        else:
            pk_kwargs = {binding.primary_key_attribute: pk_value}  # type: ignore[dict-item]
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
        key_values = RelationValues.get_checked_relation_key_values(obj._binding, key, value, context=obj._context)
        if value is None and not relation_field.null:
            raise QueryError(f"{model.__name__}.{key} doesn't take None")
        InstanceInitialization.check_relation_kwargs_agree(obj, relation_field, key_values, kwargs)
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
    def raise_for_none(binding: ModelBinding, key: str) -> NoReturn:
        """Raises for None given to a field that doesn't take it.

        Args:
            binding: The model.
            key: The field.

        Raises:
            QueryError: Always.
        """
        raise QueryError(f"{binding._model.__name__}.{key} doesn't take None")

    @staticmethod
    def take_callable_kwarg(value: Callable[[], Any], field_object: Field[Any]) -> tuple[bool, Any]:
        """Prepare a callable value without changing an instance's pending defaults.

        An async callable is returned for publication after every argument is prepared;
        a synchronous one is called and converted now.

        Args:
            value: The callable.
            field_object: The field.

        Returns:
            Whether to assign now, and the converted value or pending callable.
        """
        if Field._is_async_default(value):
            return False, value
        return True, field_object.to_python(value())

    @staticmethod
    def raise_for_computed_fields(binding: ModelBinding, values: dict[str, Any]) -> NoReturn:
        """Raises for a value given to a field the database computes - it would never be written.

        Args:
            binding: The model.
            values: The values given, by field name.

        Raises:
            QueryError: Always.
        """
        model = binding._model
        names = sorted(binding.computed_field_names.intersection(values))
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
        meta = obj._binding
        if key in meta.backward_foreign_key_fields or key in meta.backward_one_to_one_fields:
            raise QueryError(f"{type(obj).__name__}.{key} is a backward relation - set it on the related model")
        if key in meta.many_to_many_fields:
            raise QueryError(
                f"{type(obj).__name__}.{key} is a many-to-many relation - add its objects once the object is "
                f"saved: obj.{key}.add(...)"
            )
        raise FieldError(f"Unknown field {key!r} for model {meta.full_name}")
