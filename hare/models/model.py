from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from copy import copy, deepcopy
from typing import TYPE_CHECKING, Any, ClassVar, Generator, NoReturn, TypeVar, cast

from hare.core.connections import Connections
from hare.ddl.indexes.index import Index
from hare.dialects.base.client.transaction_client import TransactionClient
from hare.dialects.base.savepoint_span import current_savepoint_span
from hare.exceptions import (
    CascadeDepthLimitError,
    ConfigurationError,
    FieldError,
    IncompleteInstanceError,
    IntegrityError,
    NoValuesFetched,
    QueryError,
    StaleObjectError,
    ValidationError,
)
from hare.fields.base.field import Field
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.fields.data.json.json_field import JSONField
from hare.fields.enums import RelationLoadStrategy
from hare.fields.relations.fields.backward_fk_relation import BackwardFKRelation
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.fields.relations.fields.many_to_many_field_instance import ManyToManyFieldInstance
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.fields.relations.fields.relational_field import RelationalField
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.constants import EMPTY
from hare.models.deletion.cascade_deletion import CascadeDeletion
from hare.models.deletion.cascade_restore import CascadeRestore
from hare.models.deletion.delete_preview_builder import DeletePreviewBuilder
from hare.models.deletion.deletion_collector import DeletionCollector
from hare.models.deletion.deletion_graph import DeletionGraph
from hare.models.deletion.protect_constraint_deferral import ProtectConstraintDeferral
from hare.models.deletion.related_rows import RelatedRows
from hare.models.enums import ModelOption
from hare.models.meta_class import ModelMeta
from hare.models.meta_info import MetaInfo
from hare.models.snapshot import FieldSnapshot
from hare.models.tenancy import Tenancy
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.write_fields import WriteFields
from hare.models.write.write_steps import WriteSteps
from hare.query.base_manager import BaseManager
from hare.query.expressions.base.expression import Expression
from hare.query.queryset import QuerySet, QuerySetSingle
from hare.query.queryset.none_result import NoneAwaitable
from hare.query.queryset.relations.many_to_many_relation import ManyToManyRelation
from hare.query.queryset.relations.related_queryset.related_query_set import RelatedQuerySet
from hare.query.queryset.relations.reverse_relation import ReverseRelation
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility
from hare.sql import Table
from hare.transactions.atomic import Atomic
from hare.transactions.transactions import Transactions
from hare.utils import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    import datetime
    from collections.abc import Iterable

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.deletion.delete_preview import DeletePreview

TModel = TypeVar("TModel", bound="Model")
TPrimaryKey = TypeVar("TPrimaryKey")


class Model(metaclass=ModelMeta):
    """
    Base class for all Hare ORM Models: construction, relation attributes, change tracking and
    the instance's own writes (``save``/``delete``/``hard_delete``/``restore``/
    ``refresh_from_db``). Queries start at ``Model.objects``, not on the class itself.
    """

    # I don' like this here, but it makes auto completion and static analysis much happier
    _meta = MetaInfo(None)

    #: Where the model's queries start: ``Model.objects`` is a queryset of its rows. Set per
    #: model class by the metaclass - the ``objects`` the class declares, ``Meta.manager``, or
    #: a plain ``Manager``.
    objects: ClassVar[BaseManager]

    # Type-only (no value assigned) - every one of these is only ever set via
    # object.__setattr__ (see __init__'s own comments on why), which mypy
    # can't infer an attribute's type from, so this is the only place their types are declared.
    _partial: bool
    _saved_in_db: bool
    _custom_generated_pk: bool
    _await_when_save: dict[str, Any]
    _dirty_snapshot: dict[str, Any] | None
    #: The alias of the connection this instance was loaded from or last saved to - None for an
    #: instance that never touched the database. Set per instance via object.__setattr__.
    _db_connection_name: str | None = None

    def __init__(self, **kwargs: Any) -> None:
        # self._meta is a very common attribute lookup, lets cache it.
        meta = self._meta
        constructor = meta.instance_constructor
        if constructor is None:
            constructor = meta.get_instance_constructor()
        if constructor and constructor.construct(self, kwargs):
            return
        # Not field names - set past the overridden __setattr__.
        _setattr = object.__setattr__
        _setattr(self, "_partial", False)
        _setattr(self, "_saved_in_db", False)
        _setattr(self, "_custom_generated_pk", False)
        _setattr(self, "_await_when_save", {})
        # None yet - a never-persisted instance has no prior state to diff against, so
        # get_dirty_fields() treats every field as dirty relative to nonexistence.
        _setattr(self, "_dirty_snapshot", None)

        # Assign defaults for missing fields. A plain column of the new instance is set directly,
        # as in _set_kwargs() - Model.__setattr__ has nothing to refresh on it yet; a relation, an
        # automatic primary key and a model overriding __setattr__ still go through it.
        sets_columns_directly = type(self).__setattr__ is Model.__setattr__
        fields_db_projection = meta.fields_db_projection
        generated_pk_field_name = meta.generated_pk_field_name
        for key in meta.fields.difference(self._set_kwargs(kwargs, on_new_instance=True)):
            self._assign_default(
                key,
                object.__setattr__
                if sets_columns_directly and key in fields_db_projection and key != generated_pk_field_name
                else setattr,
            )

    def _assign_default(self, key: str, set_value: Callable[[Any, str, Any], None]) -> None:
        """Sets a field the constructor wasn't given a value for to its default.

        Args:
            key: The field name.
            set_value: How the value is set - ``object.__setattr__`` for a plain column of the new
                instance, else ``setattr``.
        """
        field_object = self._meta.fields_map[key]
        field_default = field_object.default
        if field_object._default_is_coroutine:
            self._await_when_save[key] = field_default
        elif callable(field_default):
            value = field_default()
            normalized_types = field_object.assign_normalized_types
            if normalized_types is None or (value is not None and type(value) not in normalized_types):
                value = field_object.get_default_value_on_assign(value)
            set_value(self, key, value)
        elif field_default is not None:
            # A default already in its assigned form (checked once per field) skips the to_python
            # call - the common case stays a plain assignment.
            if field_object.static_default_is_normalized is not True:
                set_value(self, key, field_object.get_static_default_value())
            elif isinstance(field_default, (int, float, str, bool, bytes)):
                set_value(self, key, field_default)
            else:
                set_value(self, key, deepcopy(field_default))
        elif field_object.has_db_default():
            set_value(self, key, field_object.get_db_default_value())
        else:
            set_value(self, key, None)

    def __setattr__(self, key: str, value: Any) -> None:
        meta = self._meta
        hooks = meta.setattr_hooks
        if hooks is None:
            hooks = meta.get_setattr_hooks()
        if key not in hooks:
            # A plain attribute: stored, overriding a pending async default.
            pending_defaults = self.__dict__.get("_await_when_save")
            if pending_defaults:
                pending_defaults.pop(key, None)
            meta.next_setattr(self, key, value)
            return
        # set field value override async default function
        if hasattr(self, "_await_when_save"):
            self._await_when_save.pop(key, None)
        if meta.soft_delete_field is not None and key == meta.soft_delete_field:
            self._check_soft_delete_write_allowed(key)
        if key in meta.fk_fields or key in meta.o2o_fields:
            self._validate_relation_type(key, value)
        elif (cache_key := meta.fk_shadow_columns.get(key)) is not None:
            # A key column assigned directly (author_id = 2) drops the cached related object of its
            # relation.
            if hasattr(self, cache_key):
                object.__delattr__(self, cache_key)
        elif key == meta.generated_pk_field_name:
            # A value assigned to an auto-generated pk of an instance not saved yet reaches the
            # INSERT; None hands the pk back to the database.
            object.__setattr__(
                self, "_custom_generated_pk", value is not None and not getattr(self, "_saved_in_db", False)
            )
        super().__setattr__(key, value)

    def _check_soft_delete_write_allowed(self, key: str) -> None:
        """
        Rejects a direct write to ``Meta.soft_delete_field`` on a persisted instance.

        Args:
            key: The field name about to be assigned.

        Raises:
            QueryError: If ``key`` is the soft-delete field of an already persisted instance.
        """
        if (
            self._meta.soft_delete_field is not None
            and key == self._meta.soft_delete_field
            and getattr(self, "_saved_in_db", False)
            and not getattr(self, "_allow_soft_delete_write", False)
        ):
            raise QueryError(
                f"Cannot set '{key}' directly on a persisted {type(self).__name__} - "
                "use .delete()/.restore() instead of mutating the soft-delete field"
            )

    def _check_relation_kwargs_agree(
        self, relation_field: RelationalField[Any], value: Any, kwargs: dict[str, Any]
    ) -> None:
        """
        Checks that a relation passed together with its own source column(s) names the same row.

        Args:
            relation_field: The FK/O2O field being assigned.
            value: The related instance (or ``None``) passed for ``relation_field``.
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
            given_value = self._meta.fields_map[source_field].to_python(kwargs[source_field])
            if given_value != expected_value:
                raise QueryError(
                    f"Conflicting values for '{relation_field.model_field_name}' ({expected_value!r}) "
                    f"and '{source_field}' ({given_value!r}) - pass only one of them, or matching values"
                )

    def _expand_pk_kwarg(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Expands a ``pk`` kwarg into the real primary-key field name(s) it aliases, the same
        way ``.filter(pk=...)`` already resolves it for reads.

        Args:
            kwargs: Raw constructor/create kwargs, containing ``pk``.

        Returns:
            A new dict with ``pk`` replaced by the real pk field name(s).

        Raises:
            QueryError: ``pk`` is given together with one of the real pk field names it
                aliases, or (for a composite pk) its value isn't a matching-length tuple.
        """
        meta = self._meta
        pk_value = kwargs["pk"]
        if meta.has_composite_primary_key:
            if not isinstance(pk_value, tuple) or len(pk_value) != len(meta.pk_attr):
                raise QueryError(
                    f"Composite pk must be set to a {len(meta.pk_attr)}-tuple matching "
                    f"{meta.pk_attr}, got {pk_value!r}"
                )
            pk_kwargs = dict(zip(meta.pk_attr, pk_value, strict=True))
        else:
            pk_kwargs = {cast("str", meta.pk_attr): pk_value}
        for pk_field_name in pk_kwargs:
            if pk_field_name in kwargs:
                raise QueryError(f"Can't set both 'pk' and '{pk_field_name}' - they name the same field")
        expanded_kwargs = {key: value for key, value in kwargs.items() if key != "pk"}
        expanded_kwargs.update(pk_kwargs)
        return expanded_kwargs

    def _get_kwargs_without_unset_automatic_pk(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        """Drops a primary key passed as ``None`` when the database generates it or the field has
        a ``default``/``db_default`` - "not assigned yet", so the generated or default value
        applies, and ``Model(**dict(instance))`` round-trips instead of rejecting the ``None``.

        Args:
            kwargs: Raw constructor/create kwargs.

        Returns:
            ``kwargs`` itself if nothing was dropped, otherwise a filtered copy.
        """
        fields_map = self._meta.fields_map
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

    def _set_kwargs(self, kwargs: dict[str, Any], *, on_new_instance: bool = False) -> set[str]:
        """Validates, converts and assigns field values given by name.

        Args:
            kwargs: The values, by field name (``pk`` included).
            on_new_instance: Whether the instance is being constructed - nothing is cached, pending
                or saved on it yet for an assignment to refresh.

        Returns:
            The names of the fields given, a relation's source columns included.
        """
        meta = self._meta
        if "pk" in kwargs:
            kwargs = self._expand_pk_kwarg(kwargs)
        kwargs = self._get_kwargs_without_unset_automatic_pk(kwargs)

        # Assign values and do type conversions
        passed_fields = {*kwargs.keys()} | meta.fetch_fields

        # Every value is validated and converted before the first is assigned - a failure leaves the
        # instance untouched. Relations are assigned after the plain fields.
        direct_assignments: list[tuple[str, Any]] = []
        relation_assignments: list[tuple[str, Any]] = []

        for key, value in kwargs.items():
            if key in meta.fk_fields or key in meta.o2o_fields:
                relation_field = cast("RelationalField[Any]", meta.fields_map[key])
                self._validate_relation_type(key, value)
                if value is None and not relation_field.null:
                    raise QueryError(f"{key} is non nullable field, but null was passed")
                self._check_relation_kwargs_agree(relation_field, value, kwargs)
                relation_assignments.append((key, value))
                passed_fields.update(relation_field.source_fields)
            elif key in meta.fields_db_projection:
                field_object = meta.fields_map[key]
                if key == meta.soft_delete_field:
                    self._check_soft_delete_write_allowed(key)
                normalized_types = field_object.assign_normalized_types
                if normalized_types is None:
                    normalized_types = field_object.assign_normalized_types = (
                        field_object.get_assign_normalized_types()
                    )
                if type(value) in normalized_types:
                    # Already the type to_python() returns unchanged (and neither an
                    # Expression nor a callable) - the common case, assigned as it is.
                    direct_assignments.append((key, value))
                elif isinstance(value, Expression):
                    # Not converted at all here, same as a plain `instance.field = F(...)`
                    # attribute assignment (which never goes through this method) - to_db_value()/
                    # execute_update() apply it (or raise a clear error) at write time instead.
                    direct_assignments.append((key, value))
                elif callable(value):
                    if Field._is_async_default(value):
                        self._await_when_save[key] = value
                    else:
                        direct_assignments.append((key, field_object.to_python(value())))
                else:
                    if value is None and not field_object.null:
                        raise QueryError(f"{key} is non nullable field, but null was passed")
                    direct_assignments.append((key, field_object.to_python(value)))
            elif key in meta.backward_fk_fields:
                raise QueryError("You can't set backward relations through init, change related model instead")
            elif key in meta.backward_o2o_fields:
                raise QueryError(
                    "You can't set backward one to one relations through init, change related model instead"
                )
            elif key in meta.m2m_fields:
                raise QueryError("You can't set m2m relations through init, use m2m_manager instead")
            else:
                raise FieldError(f"Unknown field '{key}' for model {self._meta.full_name}")

        if on_new_instance and type(self).__setattr__ is Model.__setattr__:
            # A new instance has no cached related object, pending async default or saved state
            # for Model.__setattr__ to refresh on a plain column - only an automatic primary key
            # keeps its own bookkeeping there.
            generated_pk_field_name = meta.generated_pk_field_name
            for key, value in direct_assignments:
                if key == generated_pk_field_name:
                    setattr(self, key, value)
                else:
                    object.__setattr__(self, key, value)
        else:
            for key, value in direct_assignments:
                setattr(self, key, value)
        for key, value in relation_assignments:
            setattr(self, key, value)

        return passed_fields

    def __str__(self) -> str:
        return f"{self.__class__.__name__} object ({None if self._pk_is_unset() else self.pk})"

    def _pk_is_unset(self) -> bool:
        # `is None`, not truthiness: a composite pk is a non-empty tuple even when unset, and 0 is a
        # valid pk.
        pk = self.pk
        return any(component is None for component in pk) if isinstance(pk, tuple) else pk is None

    def __repr__(self) -> str:
        if not self._pk_is_unset():
            return f"<{self.__class__.__name__}: {self.pk}>"
        return f"<{self.__class__.__name__}>"

    def __hash__(self) -> int:
        if not self._meta.has_primary_key:
            # A row of a model without a primary key is only ever equal to itself.
            return object.__hash__(self)
        if self._pk_is_unset():
            raise TypeError("Model instances without id are unhashable")
        return hash(self.pk)

    def __iter__(self) -> Any:
        # fields_db_projection's keys (model-field names), not self._meta.db_fields (its VALUES -
        # actual DB column names) - same bug class already fixed in refresh_from_db(): a field
        # whose source_field differs from its model field name made getattr(self, field) raise.
        for field in self._meta.fields_db_projection:
            # A field whose async default() hasn't run yet (an unsaved instance) has no attribute
            # at all - reported as None, same as get_dirty_fields() and construct() do.
            if field in self._await_when_save:
                yield field, None
            else:
                try:
                    value = getattr(self, field)
                except AttributeError:
                    raise NoValuesFetched(
                        f"{self.__class__.__name__}.{field} wasn't fetched - a .only()/.defer() query left it out, "
                        "so the instance can't be turned into a dict without it"
                    ) from None
                yield field, value

    def to_dict(self) -> dict[str, Any]:
        """Direct field values as a plain dict, keyed by model field name (not DB column name).
        A field still waiting for its async ``default=`` (unsaved instance) is ``None``.

        Raises:
            NoValuesFetched: A ``.only()``/``.defer()`` query left a field unloaded.
        """
        return dict(self)

    def __getstate__(self) -> dict[str, Any]:
        """Drops the pending rollback-restore bookkeeping before pickling or deep-copying - it refers
        to a live connection and an open transaction of this process.
        """
        state = self.__dict__.copy()
        state.pop("_pending_rollback_restore_stack", None)
        return state

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return False
        pk = self.pk
        other_pk = other.pk  # type: ignore[attr-defined]
        # An unsaved instance equals only itself - two of them both have an unset pk.
        if (isinstance(pk, tuple) and any(component is None for component in pk)) or pk is None:
            return self is other
        return pk == other_pk

    def _get_pk_val(self) -> Any:
        # pk_attr is the field name of a single-column key, a tuple of names for a composite one
        # and () without a key - read straight off it, every `.pk` access goes through here.
        pk_attr = self._meta.pk_attr
        if type(pk_attr) is str:
            return getattr(self, pk_attr, None)
        if not pk_attr:
            return None
        return tuple(getattr(self, name, None) for name in pk_attr)

    def _set_pk_val(self, value: Any) -> None:
        self._meta.raise_if_no_primary_key("setting pk")
        if self._meta.has_composite_primary_key:
            if not isinstance(value, tuple) or len(value) != len(self._meta.pk_attr):
                raise QueryError(
                    f"Composite pk must be set to a {len(self._meta.pk_attr)}-tuple matching "
                    f"{self._meta.pk_attr}, got {value!r}"
                )
            for name, part in zip(self._meta.pk_attr, value, strict=True):
                setattr(self, name, part)
            return
        setattr(self, cast("str", self._meta.pk_attr), value)

    pk = property(_get_pk_val, _set_pk_val)
    """
    Alias to the models Primary Key.
    Can be used as a field name when doing filtering e.g. ``.filter(pk=...)`` etc...
    """

    def __copy__(self: TModel) -> TModel:
        """A structural duplicate. Mutable field values are deep-copied; a cached related object of a
        forward relation is shared; a reverse or many-to-many container is replaced with an
        unfetched one bound to the copy, and a cached reverse one-to-one is dropped.
        """
        cls = type(self)
        obj = cls.__new__(cls)
        obj.__dict__.update(self.__dict__)
        _setattr = object.__setattr__

        # A dict of its own: assigning a field pops its pending default in place.
        _setattr(obj, "_await_when_save", dict(self._await_when_save))

        # A dict of its own: a partial write updates the snapshot in place. Absent on an instance of
        # a model without Meta.track_dirty_fields.
        existing_dirty_snapshot = getattr(self, "_dirty_snapshot", None)
        if existing_dirty_snapshot is not None:
            _setattr(obj, "_dirty_snapshot", dict(existing_dirty_snapshot))

        for field_name in self._meta.direct_fields:
            if field_name not in obj.__dict__:
                continue  # not loaded on a .only()/.defer() partial instance - leave it that way
            value = obj.__dict__[field_name]
            copied_value = FieldSnapshot.copy_value(value)
            if copied_value is not value:
                _setattr(obj, field_name, copied_value)

        meta = self._meta
        for key in meta.backward_fk_fields:
            cache_key = f"_{key}"
            if cache_key in obj.__dict__:
                original_container = cast("ReverseRelation[Any]", obj.__dict__[cache_key])
                _setattr(
                    obj,
                    cache_key,
                    type(original_container)(
                        original_container.model,
                        original_container.relation_fields,
                        obj,
                        original_container.from_fields,
                    ),
                )
        for key in meta.m2m_fields:
            cache_key = f"_{key}"
            if cache_key in obj.__dict__:
                field_object = cast("ManyToManyFieldInstance[Any]", meta.fields_map[key])
                _setattr(obj, cache_key, type(obj.__dict__[cache_key])(obj, field_object))
        for key in meta.backward_o2o_fields:
            cache_key = f"_{key}"
            if cache_key in obj.__dict__:
                object.__delattr__(obj, cache_key)

        # Rollback bookkeeping never transfers to a copy - _register_rollback_restore()'s pending
        # stack (and the live DB clients it references) is scoped to the ORIGINAL instance's own
        # writes inside a still-open transaction, meaningless once duplicated onto a second one.
        if "_pending_rollback_restore_stack" in obj.__dict__:
            object.__delattr__(obj, "_pending_rollback_restore_stack")

        return obj

    def clone(self: TModel, pk: Any = EMPTY) -> TModel:
        """
        Create a new clone of the object that when you do a ``.save()`` will create a new record.

        Args:
            pk: An optionally required value if the model doesn't generate its own primary key.
                Any value you specify here will always be used.

        Returns:
            A copy of the current object without primary key information, and with every
            ``auto_now_add`` field reset so the new row gets its own first-save value.

        Raises:
            QueryError: If pk is required but not provided.
        """
        obj = copy(self)
        # A clone is a row not inserted yet - every field is dirty.
        object.__setattr__(obj, "_dirty_snapshot", None)
        # Marked unsaved before any pk assignment below: assigning a generated pk on an unsaved
        # instance is what makes Model.__setattr__ send it in the INSERT (see its pk branch).
        object.__setattr__(obj, "_saved_in_db", False)
        # An auto_now_add value is only ever filled in while still None, so the copy would
        # otherwise keep the original's timestamp instead of getting its own on first save.
        for field_name, field in self._meta.fields_map.items():
            if getattr(field, "auto_now_add", False):
                setattr(obj, field_name, None)
        if pk is EMPTY:
            if self._meta.has_composite_primary_key:
                # Every component of a composite PK is resolved independently, the same way a
                # single-column PK is below - a component with its own default=/db_default=
                # (e.g. a version field) gets a fresh value; a component with neither raises.
                for field_name, field in zip(self._meta.pk_attr, self._meta.pk_fields, strict=True):
                    obj._apply_clone_pk_component_default(field_name, field)
                return obj
            pk_field: Field[Any] = self._meta.pk
            if pk_field.generated is False and pk_field.default is None and pk_field.has_db_default():
                # A primary key with only a db_default: the INSERT leaves the column to the
                # database.
                obj.pk = pk_field.get_db_default_value()
            elif pk_field.generated is False and pk_field.default is None:
                raise QueryError(
                    f"{self._meta.full_name} requires explicit primary key. Please use .clone(pk=<value>)"
                )
            elif pk_field.generated is False:
                # An app-side default (UUIDField's uuid4): computed anew, the database assigns
                # nothing.
                pk_default = pk_field.default
                if pk_field._default_is_coroutine:
                    # After the assignment: __setattr__ pops the pending default of an assigned
                    # field.
                    obj.pk = None
                    obj._await_when_save[cast("str", self._meta.pk_attr)] = pk_default
                elif callable(pk_default):
                    obj.pk = pk_default()
                else:
                    obj.pk = pk_default
            else:
                obj.pk = None
        else:
            obj.pk = pk
        return obj

    def _apply_clone_pk_component_default(self, field_name: str, field: Field[Any]) -> None:
        """Assign one composite-PK component its cloned value, mirroring the single-column PK
        default/db_default/async-default resolution in ``clone()`` above.

        Args:
            field_name: the model attribute name for this PK component.
            field: the Field object for this PK component.

        Raises:
            QueryError: if the field has neither a default nor a db_default to fall back on.
        """
        # generated=True is never possible here - CompositePrimaryKey's own validation forbids a
        # composite PK member from being DB-generated, so unlike the single-column PK path above,
        # there's no generated=True case to handle.
        if field.default is None and field.has_db_default():
            setattr(self, field_name, field.get_db_default_value())
        elif field.default is None:
            raise QueryError(
                f"{self._meta.full_name} requires an explicit value for composite primary key "
                f"component '{field_name}'. Please use .clone(pk=(<value>, ...))"
            )
        elif field._default_is_coroutine:
            # After the assignment: __setattr__ pops the pending default of an assigned field.
            setattr(self, field_name, None)
            self._await_when_save[field_name] = field.default
        elif callable(field.default):
            setattr(self, field_name, field.default())
        else:
            setattr(self, field_name, field.default)

    @classmethod
    def construct(cls: type[TModel], _saved_in_db: bool = False, **kwargs: Any) -> TModel:
        """Creates an instance without validation, DB checks or relation restrictions - for unit tests
        and serialization without a database.

        Unlike ``__init__`` it doesn't validate values, accepts unsaved related objects and
        reverse/M2M relations, doesn't call ``from_db_value`` and sets async defaults to None.
        Reverse and M2M relations count as fetched.

        Example::

            tournament = Tournament.construct(id=1, name="Test")
            event = Event.construct(name="Game", tournament=tournament, participants=[Team.construct(id=1)])
            assert event.tournament_id == 1
            assert len(event.participants) == 1

        Args:
            _saved_in_db: Whether to mark the instance as saved in DB.
            kwargs: Field values to set on the instance.

        Returns:
            The new instance.
        """
        self = cls.__new__(cls)
        meta = self._meta
        _setattr = object.__setattr__

        _setattr(self, "_partial", False)
        _setattr(self, "_saved_in_db", _saved_in_db)
        _setattr(self, "_custom_generated_pk", False)
        _setattr(self, "_await_when_save", {})
        # No baseline - nothing was read from the database, so every field reports dirty.
        _setattr(self, "_dirty_snapshot", None)

        # Track source fields that are auto-populated from FK/O2O objects
        # so that the default-setting loop doesn't overwrite them with None.
        populated_source_fields: set[str] = set()

        for key, value in kwargs.items():
            if key in meta.backward_fk_fields:
                # Backward FK: wrap in ReverseRelation with _fetched=True
                backward_fk = cast("BackwardFKRelation[Any]", meta.fields_map[key])
                rel: ReverseRelation[Any] = ReverseRelation.get_class_for(backward_fk.related_model)(
                    backward_fk.related_model,
                    backward_fk.relation_fields,
                    self,
                    tuple(f.model_field_name for f in backward_fk.to_field_instances),
                )
                rel._fetched = True
                rel.related_objects = list(value)
                _setattr(self, f"_{key}", rel)
            elif key in meta.m2m_fields:
                # M2M: wrap in ManyToManyRelation with _fetched=True
                field_object = cast("ManyToManyFieldInstance[Any]", meta.fields_map[key])
                m2m_rel: ManyToManyRelation[Any] = ManyToManyRelation.get_class_for(field_object.related_model)(
                    self, field_object
                )
                m2m_rel._fetched = True
                m2m_rel.related_objects = list(value)
                _setattr(self, f"_{key}", m2m_rel)
            elif key in meta.backward_o2o_fields:
                # Backward O2O: store at _{key} for property getter
                _setattr(self, f"_{key}", value)
            elif key in meta.fk_fields or key in meta.o2o_fields:
                # FK/O2O: store at _{key} for property getter, also set source field(s) - one
                # per composite-PK-target component when the target's PK isn't a single column.
                _setattr(self, f"_{key}", value)
                fk_field = cast("RelationalField[Any]", meta.fields_map[key])
                if fk_field.to_field_instances:
                    for source_field, to_field_instance in zip(
                        fk_field.source_fields, fk_field.to_field_instances, strict=True
                    ):
                        if value is not None:
                            _setattr(self, source_field, getattr(value, to_field_instance.model_field_name, None))
                        else:
                            _setattr(self, source_field, None)
                        populated_source_fields.add(source_field)
            else:
                # Data fields, source fields, or unknown fields: store directly
                _setattr(self, key, value)

        # Set defaults for unprovided non-relational fields
        for key in meta.fields.difference(kwargs.keys()):
            if key in meta.fetch_fields:
                continue
            if key in populated_source_fields:
                continue
            default_field = meta.fields_map[key]
            field_default = default_field.default
            if default_field._default_is_coroutine:
                # Async defaults are skipped in construct() since it is synchronous
                _setattr(self, key, None)
            elif callable(field_default):
                _setattr(self, key, default_field.get_default_value_on_assign(field_default()))
            elif field_default is not None:
                # Same copy-and-normalize rule as __init__ - a shared literal default (e.g.
                # JSONField(default={"a": 1})) would otherwise be one object across every instance.
                _setattr(self, key, default_field.get_static_default_value())
            elif default_field.has_db_default():
                _setattr(self, key, default_field.get_db_default_value())
            else:
                _setattr(self, key, None)

        # Same rule as Model.__setattr__ (bypassed above): a not-yet-persisted instance with a
        # value for its auto-generated pk sends it in the INSERT.
        generated_pk_field_name = meta.generated_pk_field_name
        if (
            generated_pk_field_name is not None
            and not _saved_in_db
            and getattr(self, generated_pk_field_name, None) is not None
        ):
            _setattr(self, "_custom_generated_pk", True)

        return self

    def update_from_dict(self: TModel, data: dict[str, Any]) -> TModel:
        """Updates the instance from a dict, converting values by their fields. Unknown keys are
        ignored.

        Args:
            data: The values by field name.

        Returns:
            This instance.

        Raises:
            QueryError: A reverse or many-to-many relation is given, or None for a non-nullable
                field.
            ValidationError: A field rejects a value.
        """
        meta = self._meta
        # Unknown keys are dropped here - _set_kwargs() raises for them.
        known_data = {
            key: value
            for key, value in data.items()
            if key == "pk"
            or key in meta.fk_fields
            or key in meta.o2o_fields
            or key in meta.fields_db_projection
            or key in meta.backward_fk_fields
            or key in meta.backward_o2o_fields
            or key in meta.m2m_fields
        }
        self._set_kwargs(known_data)
        return self

    def _remember_db(self, db: DatabaseClient) -> None:
        """Records the connection this instance was loaded from or saved to.

        Args:
            db: The connection.
        """
        object.__setattr__(self, "_db_connection_name", db.connection_name)

    def _get_connection_for_instance(
        self, for_write: bool = False, model: type[Model] | None = None
    ) -> DatabaseClient:
        """The connection for an operation on this instance or a relation of it: the router's choice,
        else the connection the instance came from, else the model's default one. A related model
        with another default connection keeps its own.

        Args:
            for_write: Whether the connection is chosen for a write.
            model: The model queried - this instance's own when omitted.

        Returns:
            The chosen connection.
        """
        queried_model = model if model is not None else type(self)
        connection_name = self._db_connection_name
        if connection_name is None or queried_model._meta.default_connection != self._meta.default_connection:
            return queried_model.get_connection(for_write)
        from hare.core.context import HareContext

        ctx = HareContext.require_current()
        router = ctx.router
        if router.has_routers:
            db = router.db_for_write(queried_model) if for_write else router.db_for_read(queried_model)
            if db is not None:
                return db
            if queried_model is not type(self) and self._is_routed(for_write):
                return queried_model.get_connection(for_write)
        return ctx.connections.get(connection_name)

    @classmethod
    def _is_routed(cls, for_write: bool = False) -> bool:
        """Whether the router chooses the connection for this model.

        Args:
            for_write: Whether the connection is chosen for a write.

        Returns:
            True when a router has an opinion for this model.
        """
        from hare.core.context import HareContext

        router = HareContext.require_current().router
        if not router.has_routers:
            return False
        db = router.db_for_write(cls) if for_write else router.db_for_read(cls)
        return db is not None

    @classmethod
    def get_connection(cls, for_write: bool = False) -> DatabaseClient:
        """The connection a query on this model would run on now: the router's choice, else the model's
        ``default_connection`` - inside an open transaction on it, the transaction's client.

        Args:
            for_write: Whether the connection is chosen for a write or for a read.

        Returns:
            The chosen connection.

        Raises:
            ConfigurationError: No Hare context is active.
            QueryError: The model is swapped out or has no default connection.
        """
        # Deferred import: the router is read off the active HareContext.
        from hare.core.context import HareContext

        cls._meta.check_not_swapped()
        ctx = HareContext.require_current()
        router = ctx.router
        if for_write:
            db = router.db_for_write(cls)
        else:
            db = router.db_for_read(cls)
        if db is not None:
            return db
        # MetaInfo.db inlined - the context is already at hand, and this is the hottest path of the
        # ORM.
        meta = cls._meta
        default_connection = meta.default_connection
        if default_connection is None:
            raise ConfigurationError(f"default_connection for the model {meta._model} cannot be None")
        return ctx.connections.get(default_connection)

    def __await__(self: TModel) -> Generator[Any, None, TModel]:
        async def _self() -> TModel:
            return self

        return _self().__await__()

    class Meta:
        """Configures the model's metadata.

        Usage:

        .. code-block:: python3

            class Foo(Model):
                ...

                class Meta:
                    table = "custom_table"
                    constraints = (UniqueConstraint(fields=("field_a", "field_b")),)
        """

    async def _set_async_default_field(self) -> None:
        """retrieve value from field's async default value"""
        if hasattr(self, "_await_when_save"):
            for k, v in self._await_when_save.copy().items():
                setattr(self, k, await v())
            # Not a field - set past the overridden __setattr__, which costs more on every save().
            object.__setattr__(self, "_await_when_save", {})

    async def save(
        self,
        using: str | DatabaseClient | None = None,
        update_fields: Iterable[str] | None = None,
        force_create: bool = False,
        force_update: bool = False,
    ) -> None:
        """
        Creates/Updates the current model object.

        Args:
            update_fields: If provided, it should be a tuple/list of fields by name. This is
                the subset of fields that should be updated. On an instance that has a pk it
                always issues an UPDATE by primary key - even for an instance never loaded from
                or saved to the database (``Model(id=known_id, name="x")``) - and raises
                ``IntegrityError`` if no such row exists instead of creating it (use ``create()``
                or ``force_create=True``). It is only ignored when there is no pk yet (a
                DB-generated one), where the row is inserted. An empty collection is a no-op.
            using: Specific DB connection to use instead of default bound
            force_create: Forces creation of the record, with the current pk when it has one
            force_update: Forces updating of the record

        Raises:
            FieldError: If ``update_fields`` names an unknown field or a relation (use the
                relation's source field, e.g. ``author_id``, instead).
            QueryError: If ``update_fields`` is a single string instead of a collection, or
                both ``force_create`` and ``force_update`` are set.
            QueryError: If ``Meta.tenant_field`` is set, the instance's ``tenant_field``
                value doesn't match an ACTIVE tenant scope, or it's unset with no active scope to
                fall back on either; or if ``update_fields`` names ``Meta.soft_delete_field``
                (use ``delete()``/``restore()``).
            IncompleteInstanceError: If the model is partial and the fields are not available
                for persistence.
            IntegrityError: If the model can't be created or updated (specifically if
                force_create or force_update has been set)
            QueryError: If update_fields include pk field.
            StaleObjectError: If ``Meta.optimistic_lock_field`` is set and the row was concurrently
                modified since this instance was read.
        """
        if force_create and force_update:
            raise QueryError("save() can't force both an insert (force_create) and an update (force_update)")
        if (self._saved_in_db or update_fields is not None or force_update) and not force_create:
            self._meta.raise_if_no_primary_key(f"{self.__class__.__name__}.save() of a saved row (an UPDATE)")
        # A tenant-scoped model is written only into the active scope's tenant; with no scope, the
        # instance's own value is trusted. A partial update not touching the tenant field has
        # nothing to check.
        if update_fields is not None:
            update_fields = self._get_validated_field_names(update_fields, "update_fields")
            if not update_fields:
                return
            if self._meta.soft_delete_field in update_fields:
                raise QueryError(
                    f"Cannot set '{self._meta.soft_delete_field}' via save(update_fields=...) - "
                    "use .delete()/.restore() instead"
                )
        tenant_field = self._meta.tenant_field
        if tenant_field and (update_fields is None or tenant_field in update_fields):
            WriteSteps.scope_to_active_tenant(
                self.__class__, (self,), "save", fills_missing=True, requires_active=False
            )

        if getattr(self, "_await_when_save", None):
            await self._set_async_default_field()
        db = Connections.get_client(using) or self._get_connection_for_instance(True)
        if Tenancy.has_tenant_scoped_relations(self.__class__):
            await Tenancy.check_relation_targets(self.__class__, [Tenancy.get_relation_row(self)], db, update_fields)
        writer = self._get_writer(db)
        if self._partial:
            if update_fields:
                for field in update_fields:
                    if not all(hasattr(self, pk_name) for pk_name in self._meta.pk_attr_names):
                        raise IncompleteInstanceError(
                            f"{self.__class__.__name__} is a partial model without primary key "
                            "fetched. Partial update not available"
                        )
                    if not hasattr(self, field):
                        raise IncompleteInstanceError(
                            f"{self.__class__.__name__} is a partial model, field '{field}' is not available"
                        )
                # The optimistic lock field is bumped on every save(), whatever update_fields names
                # - it has to be loaded.
                self._check_optimistic_lock_field_loaded("save")
            else:
                raise IncompleteInstanceError(
                    f"{self.__class__.__name__} is a partial model, can only be saved with the "
                    "relevant update_field provided"
                )

        # Only set when an actual partial UPDATE (update_fields is not None) is the operation
        # that ran - used below to snapshot just the fields that were actually written, instead
        # of laundering every OTHER unsaved in-memory change on this instance as clean too.
        partial_update_fields: list[str] | None = None

        # Captured before the write: a rollback reverts the row, not _saved_in_db, the pk or the
        # dirty baseline of the instance.
        was_insert = False
        pre_write_pk = self.pk
        pre_write_saved_in_db = self._saved_in_db
        pre_write_custom_generated_pk = self._custom_generated_pk
        pre_write_dirty_snapshot = (
            dict(self._dirty_snapshot) if self._meta.track_dirty_fields and self._dirty_snapshot is not None else None
        )

        # Decided before a pk default below fills the pk in - an unset pk means an INSERT.
        pk_is_unset = self.pk is None
        # A pk the database generates has no default to fill in.
        if not force_update and pk_is_unset and self._meta.generated_pk_field_name is None:
            await self._set_unset_pk_default()

        # An INSERT unless an UPDATE is forced or the instance has a row to update: loaded from or
        # saved to the database, or given update_fields, and with a primary key.
        if force_create:
            await self._execute_forced_insert(writer)
            was_insert = True
        elif not force_update and (pk_is_unset or not (self._saved_in_db or update_fields)):
            await writer.execute_insert(self)
            was_insert = True
        else:
            optimistic_lock_field = self._meta.optimistic_lock_field
            old_version = getattr(self, optimistic_lock_field) if optimistic_lock_field else None
            rows = await writer.execute_update(self, update_fields)
            if rows is None:
                rows = await self._count_update_target_rows(db)
            if rows == 0:
                message = f"Can't update object that doesn't exist. PK: {self.pk}"
                if not force_update and not pre_write_saved_in_db:
                    message += (
                        " - save(update_fields=...) on an instance that wasn't loaded from or saved to "
                        "the database always issues an UPDATE by primary key and never creates the row; "
                        "use create() or save(force_create=True) to insert it"
                    )
                self._raise_for_unmatched_update(old_version, message)
            if update_fields is not None:
                partial_update_fields = list(update_fields)

        if was_insert:
            self._register_rollback_restore(db, "_saved_in_db", pre_write_saved_in_db)
            self._register_rollback_restore(db, "pk", pre_write_pk)
        if self._meta.track_dirty_fields:
            self._register_rollback_restore(db, "_dirty_snapshot", pre_write_dirty_snapshot)

        # Not a field name - object.__setattr__ is safe here too, same reasoning as
        # _set_async_default_field's identical bypass a few lines above (and __init__'s own).
        object.__setattr__(self, "_saved_in_db", True)
        self._remember_db(db)
        if was_insert and not pre_write_custom_generated_pk:
            # The pk the database just generated was assigned through Model.__setattr__ while the
            # instance still counted as unsaved - it isn't a caller-supplied value.
            object.__setattr__(self, "_custom_generated_pk", False)
        if self._meta.track_dirty_fields:
            if partial_update_fields is not None:
                self._sync_dirty_snapshot_fields(
                    WriteFields.of(self.__class__).get_written_with(partial_update_fields)
                )
            else:
                self._snapshot_dirty_fields()
        await WriteSteps.report(
            db,
            self.__class__,
            RowOperation.INSERT if was_insert else RowOperation.UPDATE,
            instances=[self],
            fields=None if was_insert else partial_update_fields,
        )

    async def _set_unset_pk_default(self) -> None:
        """Assigns the primary key field's ``default``/``db_default`` when the pk is ``None`` and
        the database doesn't generate it."""
        meta = self._meta
        if not isinstance(meta.pk_attr, str) or meta.generated_pk_field_name is not None:
            return
        if getattr(self, meta.pk_attr, None) is not None:
            return
        pk_field = meta.pk
        default = pk_field.default
        if pk_field._default_is_coroutine:
            setattr(self, meta.pk_attr, await default())
        elif callable(default):
            setattr(self, meta.pk_attr, pk_field.get_default_value_on_assign(default()))
        elif default is not None:
            setattr(self, meta.pk_attr, pk_field.get_static_default_value())
        elif pk_field.has_db_default():
            setattr(self, meta.pk_attr, pk_field.get_db_default_value())

    async def _execute_forced_insert(self, writer: InstanceWriter) -> None:
        """INSERTs this instance for ``save(force_create=True)``, sending an already-set
        DB-generated pk as it is instead of letting the database generate a new one.

        Args:
            writer: The writer of this instance's model on the target connection.
        """
        custom_generated_pk = self._custom_generated_pk
        if self._meta.generated_pk_field_name is not None and self.pk is not None:
            object.__setattr__(self, "_custom_generated_pk", True)
        try:
            await writer.execute_insert(self)
        finally:
            object.__setattr__(self, "_custom_generated_pk", custom_generated_pk)

    async def _count_update_target_rows(self, db: DatabaseClient) -> int:
        """Counts the rows an UPDATE by this instance's pk would match, for a save() with no
        column left to write.

        Args:
            db: The connection the UPDATE would run on.

        Returns:
            ``1`` if the row exists (in the active tenant, when one is active), else ``0``.
        """
        queryset = RowScopes.get_base_queryset(
            self.__class__,
            RowVisibility(include_deleted=True, all_tenants=Tenancy.get_scope(self.__class__) is None),
        )
        return int(await queryset.filter(pk=self.pk).using(db).exists())

    def _check_optimistic_lock_field_loaded(self, operation: str) -> None:
        """Raises if ``Meta.optimistic_lock_field`` is set but a ``.only()``/``.defer()`` query left it
        unloaded - every write reads and bumps it, whichever other fields it touches.

        Args:
            operation: Names the calling method ("save"/"delete"/"restore") in the raised message.

        Raises:
            IncompleteInstanceError: ``Meta.optimistic_lock_field`` is set and not loaded on this instance.
        """
        optimistic_lock_field = self._meta.optimistic_lock_field
        if optimistic_lock_field and not hasattr(self, optimistic_lock_field):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model, Meta.optimistic_lock_field "
                f"'{optimistic_lock_field}' is not available - every {operation}() needs to read and "
                "bump it, whichever other fields it touches"
            )

    def _check_deletable(self) -> None:
        """Checks the instance names one saved row.

        Raises:
            QueryError: The instance was never saved, or its primary key isn't set (after
                ``bulk_create()`` without ``returning=True``).
            IncompleteInstanceError: The instance was loaded without its primary key.
        """
        if not self._saved_in_db:
            raise QueryError("Can't delete unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.pk_attr_names):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model without primary key fetched. Can't delete it."
            )
        if any(getattr(self, pk_name) is None for pk_name in self._meta.pk_attr_names):
            raise QueryError(
                f"Can't delete a {self.__class__.__name__} whose primary key isn't set - bulk_create() leaves a "
                "generated primary key unset unless returning=True"
            )

    def _check_active_tenant_scope_for_write(self, operation: str) -> None:
        """Raises if this instance's ``Meta.tenant_field`` value conflicts with the model's tenant
        scope. Never fills an unset tenant field - the row exists already.

        Args:
            operation: The calling method, for the message.

        Raises:
            QueryError: The scope doesn't allow the instance's tenant, or the tenant is unset with
                no scope to check against.
        """
        tenant_field = self._meta.tenant_field
        if not tenant_field:
            return
        active_tenant = Tenancy.get_scope(self.__class__)
        current_value = getattr(self, tenant_field, None)
        if current_value is None:
            if active_tenant is None:
                raise QueryError(
                    f"{self.__class__.__name__} has Meta.tenant_field '{tenant_field}' set but it's "
                    f"unset on this instance and no tenant is active either - wrap this {operation}() "
                    "call in Tenancy.scope(...)"
                )
            return
        if active_tenant is not None and not Tenancy.allows(self.__class__, active_tenant, current_value):
            raise QueryError(
                f"{operation}() on {self.__class__.__name__} would {operation} a row scoped to "
                f"{tenant_field}={current_value!r}, which does not match the active tenant scope "
                f"({active_tenant!r})"
            )

    async def delete(self, using: str | DatabaseClient | None = None) -> None:
        """Deletes the object. With ``Meta.soft_delete_field`` the row is marked deleted instead, and
        related rows follow their ``on_delete`` as for a real ``DELETE``.

        Args:
            using: Specific DB connection to use instead of default bound

        Raises:
            QueryError: The tenant scope doesn't match, or the object has never been persisted.
            IncompleteInstanceError: The primary key - for a soft delete also
                ``Meta.optimistic_lock_field`` - was not loaded.
            ProtectedError: A PROTECT relation guards this row or one ``on_delete=CASCADE`` would
                remove with it.
            IntegrityError: A ``RESTRICT``/``NO_ACTION`` relation still points at the row, or the
                row no longer exists.
            StaleObjectError: A soft delete of a row modified concurrently
                (``Meta.optimistic_lock_field``).
        """
        self._meta.raise_if_no_primary_key(f"{self.__class__.__name__}.delete() of one row")
        self._check_active_tenant_scope_for_write("delete")
        db = Connections.get_client(using) or self._get_connection_for_instance(True)
        with ChangeEvents.reporting_as_a_whole():
            await self._delete_with_cascade(db, apply_active_tenant_scope_guard=True)
        if ChangeEvents.is_observed():
            await ChangeEvents.report_deletion(
                db, self.__class__, pks=[self.pk], soft=self._meta.soft_delete_field is not None
            )

    async def _delete_with_cascade(self, db: DatabaseClient, *, apply_active_tenant_scope_guard: bool) -> None:
        """Everything ``delete()`` does after its active-tenant-scope check.

        Args:
            db: Connection the delete and its cascade run through.
            apply_active_tenant_scope_guard: ``False`` for a row a cascade found through a real FK
                match - it may belong to another tenant than the active scope (see
                ``_persist_soft_delete``).

        Raises:
            Same as ``delete()``, except the tenant-scope ``QueryError``.
        """
        self._check_deletable()
        soft_delete_field = self._meta.soft_delete_field
        if await self._is_already_soft_deleted(db):
            # Already soft-deleted - nothing to do; its deletion time stays.
            return
        if soft_delete_field:
            self._check_optimistic_lock_field_loaded("delete")
            if DeletionGraph.is_unreferenced(type(self)) and await self._write_soft_delete_field(
                db,
                CascadeDeletion.get_deleted_at(),
                "delete",
                apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                only_live_row=True,
            ):
                # Nothing cascades from or protects the row - one UPDATE of it while it's live. A
                # row it didn't match (soft-deleted meanwhile, gone, a stale version) is settled by
                # the full delete below.
                return
        await DeletionCollector.check_protected(type(self), [self.pk], db)
        if not self._meta.soft_delete_field:
            # The database's own cascade is invisible to Python - a PROTECT further down it is
            # looked for up front.
            await DeletionCollector.check_protected_transitively(type(self), [self.pk], db)
        if self._meta.soft_delete_field:
            # One transaction for the cascade and this instance's own UPDATE - they succeed or fail
            # together.
            deleted_at = CascadeDeletion.get_deleted_at()
            async with db._in_transaction() as transaction_db:
                soft_delete_values = await RelatedRows.lock_soft_delete_values(type(self), [self.pk], transaction_db)
                if (current_soft_delete_value := soft_delete_values.get(self.pk)) is not None:
                    # Soft-deleted since this instance was read (a stale copy, or a concurrent
                    # delete this one waited for) - a no-op like any already-deleted row.
                    self._adopt_soft_delete_value(transaction_db, current_soft_delete_value)
                    return
                await CascadeDeletion.run_below(
                    self,
                    transaction_db,
                    only_unconstrained=False,
                    persist_as_hard_delete=False,
                    deleted_at=deleted_at,
                )
                await self._persist_soft_delete(
                    transaction_db,
                    apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                    deleted_at=deleted_at,
                )
        else:
            await self._delete_row_permanently(db, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard)

    async def hard_delete(self, using: str | DatabaseClient | None = None) -> None:
        """Deletes the row for real, even with ``Meta.soft_delete_field`` and even when it is
        soft-deleted already. Related rows follow their ``on_delete``.

        Args:
            using: Specific DB connection to use instead of default bound

        Raises:
            QueryError: The tenant scope doesn't match, or the object has never been persisted.
            IncompleteInstanceError: The primary key was not loaded.
            ProtectedError: A PROTECT relation guards this row or one ``on_delete=CASCADE`` would
                remove with it.
            IntegrityError: A ``RESTRICT``/``NO_ACTION`` relation still points at the row, or the
                row no longer exists.
        """
        self._check_active_tenant_scope_for_write("delete")
        db = Connections.get_client(using) or self._get_connection_for_instance(True)
        self._check_deletable()
        await DeletionCollector.check_protected(type(self), [self.pk], db)
        await DeletionCollector.check_protected_transitively(type(self), [self.pk], db)
        with ChangeEvents.reporting_as_a_whole():
            await self._delete_row_permanently(db, apply_active_tenant_scope_guard=True)
        if ChangeEvents.is_observed():
            await ChangeEvents.report_deletion(db, self.__class__, pks=[self.pk])

    async def _delete_row_permanently(self, db: DatabaseClient, *, apply_active_tenant_scope_guard: bool) -> None:
        """Issues the real ``DELETE`` shared by ``delete()`` (no soft delete) and ``hard_delete()``,
        cascading in Python whatever the database can't cascade on its own.

        Args:
            db: Connection the delete and its cascade run through.
            apply_active_tenant_scope_guard: See ``_delete_with_cascade``.
        """
        if DeletionGraph.has_unconstrained_relations(type(self)):
            # A relation the database doesn't enforce: its on_delete is carried out in Python, in
            # one transaction with the DELETE.
            async with (
                db._in_transaction() as transaction_db,
                ProtectConstraintDeferral.defer(type(self), transaction_db),
            ):
                root_reached_again = await CascadeDeletion.run_below(
                    self, transaction_db, only_unconstrained=True, persist_as_hard_delete=True
                )
                await self._persist_hard_delete(
                    transaction_db,
                    apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                    allow_already_deleted=root_reached_again,
                )
        elif db.features.cascade_depth_limit is not None and (
            DeletionGraph.has_self_cascading_constrained_relations(type(self))
        ):
            # Where the database's cascade stops at a recursion depth, a deep cascade cycle fails
            # the DELETE half-done - tried in a transaction, and on CascadeDepthLimitError rolled
            # back and carried out in Python, deepest rows first.
            try:
                async with db._in_transaction() as transaction_db:
                    await self._persist_hard_delete(
                        transaction_db, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard
                    )
            except CascadeDepthLimitError:
                async with (
                    db._in_transaction() as transaction_db,
                    ProtectConstraintDeferral.defer(type(self), transaction_db),
                ):
                    root_reached_again = await CascadeDeletion.run_below(
                        self,
                        transaction_db,
                        only_unconstrained=False,
                        persist_as_hard_delete=True,
                        bottom_up_persist=True,
                    )
                    await self._persist_hard_delete(
                        transaction_db,
                        apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                        allow_already_deleted=root_reached_again,
                    )
        elif ProtectConstraintDeferral.is_needed(type(self), db):
            # A protector that this same cascade removes as well must not fail the DELETE - see
            # ProtectConstraintDeferral.
            async with (
                db._in_transaction() as transaction_db,
                ProtectConstraintDeferral.defer(type(self), transaction_db),
            ):
                await self._persist_hard_delete(
                    transaction_db, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard
                )
        else:
            await self._persist_hard_delete(db, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard)

    async def delete_preview(self, *, using: str | DatabaseClient | None = None) -> DeletePreview:
        """Reports what ``delete()`` would remove, soft-delete, null out or reset, and which rows block
        it, without writing anything. An already soft-deleted instance gets an empty preview.

        Args:
            using: Specific DB connection to read through instead of the default one.

        Returns:
            The preview of the delete.

        Raises:
            QueryError: The tenant scope doesn't match, or the object has never been persisted.
            IncompleteInstanceError: The primary key was not loaded.
        """
        self._check_active_tenant_scope_for_write("delete")
        db = Connections.get_client(using) or self._get_connection_for_instance(True)
        if not self._saved_in_db:
            raise QueryError("Can't preview deleting unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.pk_attr_names):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model without primary key fetched. Can't preview deleting it."
            )
        return await DeletePreviewBuilder.build(self, db)

    async def _is_already_soft_deleted(self, db: DatabaseClient | None) -> bool:
        """Whether this instance's row is already soft-deleted - read from the database when a
        ``.only()``/``.defer()`` query left ``Meta.soft_delete_field`` unloaded.

        Args:
            db: Connection to read through when the field isn't loaded.
        """
        soft_delete_field = self._meta.soft_delete_field
        if not soft_delete_field:
            return False
        if hasattr(self, soft_delete_field):
            return getattr(self, soft_delete_field) is not None
        return self.pk in await RelatedRows.get_soft_deleted_pks(type(self), [self.pk], db)

    def _get_writer(self, db: DatabaseClient) -> InstanceWriter:
        """The writer of this instance's model on ``db``.

        Args:
            db: The connection.

        Returns:
            The writer.
        """
        return InstanceWriter(self.__class__, db)

    def _raise_for_unmatched_update(self, old_version: Any, message: str) -> NoReturn:
        """Raises for an UPDATE of this instance's row that matched no row, after putting the bumped
        in-memory version back.

        Args:
            old_version: The version before the UPDATE.
            message: The ``IntegrityError`` message.

        Raises:
            StaleObjectError: ``Meta.optimistic_lock_field`` is set - the row was modified
                concurrently.
            IntegrityError: Otherwise.
        """
        optimistic_lock_field = self._meta.optimistic_lock_field
        if optimistic_lock_field:
            setattr(self, optimistic_lock_field, old_version)
            raise StaleObjectError(
                f"{self.__class__.__name__} (pk={self.pk}) was modified concurrently - expected version {old_version}",
                self.__class__,
                self.pk,
                old_version,
            )
        raise IntegrityError(message)

    async def _write_soft_delete_field(
        self,
        db: DatabaseClient,
        value: datetime.datetime | None,
        action: str,
        apply_active_tenant_scope_guard: bool = True,
        only_live_row: bool = False,
    ) -> bool:
        """Writes ``Meta.soft_delete_field`` of this instance's row - the deletion time for
        ``delete()``, None for ``restore()``. The in-memory value is put back when the write fails,
        matches no row or its transaction rolls back.

        Args:
            db: The connection.
            value: The value written.
            action: ``delete`` or ``restore``, for the error message.
            apply_active_tenant_scope_guard: See ``_persist_soft_delete()``.
            only_live_row: Write the row only while it isn't soft-deleted, and report a row not
                matched instead of raising.

        Returns:
            Whether the row was written.

        Raises:
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row was modified
                concurrently.
            IntegrityError: The row doesn't exist.
        """
        soft_delete_field = cast("str", self._meta.soft_delete_field)
        had_soft_delete_value = hasattr(self, soft_delete_field)
        old_soft_delete_value = getattr(self, soft_delete_field) if had_soft_delete_value else None
        self._set_soft_delete_field(value)
        writer = self._get_writer(db)
        optimistic_lock_field = self._meta.optimistic_lock_field
        old_version = getattr(self, optimistic_lock_field) if optimistic_lock_field else None
        try:
            rows = await writer.execute_update(
                self,
                update_fields=[soft_delete_field],
                apply_active_tenant_scope_guard=apply_active_tenant_scope_guard,
                only_live_row=only_live_row,
            )
        except BaseException:
            self._restore_soft_delete_field(had_soft_delete_value, old_soft_delete_value)
            raise
        if rows == 0:
            self._restore_soft_delete_field(had_soft_delete_value, old_soft_delete_value)
            if only_live_row:
                return False
            self._raise_for_unmatched_update(old_version, f"Can't {action} object that doesn't exist. PK: {self.pk}")
        self._register_rollback_restore(
            db,
            soft_delete_field,
            old_soft_delete_value if had_soft_delete_value else ROLLBACK_RESTORE_UNSET,
        )
        self._sync_dirty_snapshot_fields(WriteFields.of(self.__class__).get_written_with({soft_delete_field}), db)
        return True

    async def _persist_soft_delete(
        self,
        db: DatabaseClient,
        apply_active_tenant_scope_guard: bool = True,
        deleted_at: datetime.datetime | None = None,
    ) -> None:
        """Marks this already-cascaded instance's own row deleted with an ``UPDATE``.

        Args:
            db: The connection the delete runs on.
            apply_active_tenant_scope_guard: False for a row a cascade found through a real FK match
                - its tenant may differ from the active one.
            deleted_at: The deletion time to write - now by default.

        Raises:
            IntegrityError: The row no longer exists.
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row was modified
                concurrently.
        """
        # The UPDATE matching no row raises - a concurrent version bump would otherwise leave the
        # row live while the cascade's mutations commit, its related rows cascaded as if the delete
        # had gone through.
        await self._write_soft_delete_field(
            db, deleted_at or Timezone.now(), "delete", apply_active_tenant_scope_guard=apply_active_tenant_scope_guard
        )

    async def _persist_hard_delete(
        self,
        db: DatabaseClient,
        apply_active_tenant_scope_guard: bool = True,
        allow_already_deleted: bool = False,
    ) -> None:
        """Issues this already-cascaded instance's own ``DELETE``.

        Args:
            db: The connection the delete runs on.
            apply_active_tenant_scope_guard: See ``_persist_soft_delete()``.
            allow_already_deleted: The same cascade may already have removed the row - a DELETE
                matching nothing is fine.

        Raises:
            IntegrityError: The row no longer exists and ``allow_already_deleted`` is not set.
        """
        writer = self._get_writer(db)
        rows = await writer.execute_delete(self, apply_active_tenant_scope_guard=apply_active_tenant_scope_guard)
        if rows == 0 and not allow_already_deleted:
            raise IntegrityError(f"Can't delete object that doesn't exist. PK: {self.pk}")

    async def restore(self, using: str | DatabaseClient | None = None, *, cascade: bool = False) -> None:
        """Reverses ``.delete()`` on a soft-deleted instance.

        Args:
            using: Specific DB connection to use instead of default bound.
            cascade: Also restore the rows the soft delete of this one removed along with it - those
                still carrying this row's deletion time.

        Raises:
            QueryError: The model has no ``Meta.soft_delete_field``, the tenant scope doesn't match,
                or the object has never been persisted.
            IncompleteInstanceError: The primary key or ``Meta.optimistic_lock_field`` was not
                loaded.
            IntegrityError: The row no longer exists.
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row was modified
                concurrently.
        """
        self._meta.raise_if_no_primary_key(f"{self.__class__.__name__}.restore() of one row")
        self._check_active_tenant_scope_for_write("restore")
        if not self._meta.soft_delete_field:
            raise QueryError(f"{type(self).__name__} has no Meta.soft_delete_field configured")
        db = Connections.get_client(using) or self._get_connection_for_instance(True)
        if not self._saved_in_db:
            raise QueryError("Can't restore unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.pk_attr_names):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model without primary key fetched. Can't restore it."
            )
        self._check_optimistic_lock_field_loaded("restore")
        if not cascade:
            await self._write_soft_delete_field(db, None, "restore")
        else:
            async with db._in_transaction() as transaction_db:
                soft_delete_values = await RelatedRows.lock_soft_delete_values(type(self), [self.pk], transaction_db)
                await self._write_soft_delete_field(transaction_db, None, "restore")
                if (deleted_at := soft_delete_values.get(self.pk)) is not None:
                    await CascadeRestore(transaction_db, deleted_at).run(type(self), [self.pk])
        await WriteSteps.report(
            db, self.__class__, RowOperation.UPDATE, instances=[self], fields=[self._meta.soft_delete_field]
        )

    def _adopt_soft_delete_value(self, db: DatabaseClient, value: datetime.datetime) -> None:
        """Takes the soft-delete value another write already stored in this instance's row.

        Args:
            db: Connection of the running transaction.
            value: The row's soft-delete value.
        """
        soft_delete_field = cast("str", self._meta.soft_delete_field)
        had_soft_delete_value = hasattr(self, soft_delete_field)
        old_soft_delete_value = getattr(self, soft_delete_field) if had_soft_delete_value else None
        self._set_soft_delete_field(value)
        self._register_rollback_restore(
            db,
            soft_delete_field,
            old_soft_delete_value if had_soft_delete_value else ROLLBACK_RESTORE_UNSET,
        )
        self._sync_dirty_snapshot_fields([soft_delete_field], db)

    def _set_soft_delete_field(self, value: datetime.datetime | None) -> None:
        _setattr = object.__setattr__
        _setattr(self, "_allow_soft_delete_write", True)
        try:
            setattr(self, cast("str", self._meta.soft_delete_field), value)
        finally:
            _setattr(self, "_allow_soft_delete_write", False)

    def _restore_soft_delete_field(self, had_value: bool, old_value: datetime.datetime | None) -> None:
        """Puts ``Meta.soft_delete_field`` back after a failed delete()/restore() - to its previous
        value, or to not loaded at all.
        """
        if had_value:
            self._set_soft_delete_field(old_value)
        else:
            object.__delattr__(self, cast("str", self._meta.soft_delete_field))

    def _restore_field_value(self, field_name: str, old_value: Any) -> None:
        """Puts ``field_name`` back to what it held before a write that didn't take effect.

        Args:
            field_name: The attribute to restore.
            old_value: The previous value, or ``ROLLBACK_RESTORE_UNSET`` if the attribute wasn't
                loaded at all (a ``.only()``/``.defer()`` instance) - it goes back to unloaded
                instead of gaining a made-up value.
        """
        if old_value is ROLLBACK_RESTORE_UNSET:
            if hasattr(self, field_name):
                object.__delattr__(self, field_name)
        else:
            setattr(self, field_name, old_value)

    def _pop_pending_rollback_restore_layer(self, layer: dict[str, Any]) -> None:
        """Removes ``layer`` from this instance's pending-rollback-restore stack - by identity: two
        layers can hold equal content.

        Args:
            layer: The layer to remove.
        """
        stack: list[dict[str, Any]] = getattr(self, "_pending_rollback_restore_stack", None) or []
        for index, entry in enumerate(stack):
            if entry is layer:
                del stack[index]
                return

    def _register_rollback_restore(self, using: DatabaseClient, field_name: str, old_value: Any) -> None:
        """Registers ``field_name`` to be set back to ``old_value`` if the transaction or savepoint
        this write runs in rolls back - a ROLLBACK reverts the row, never the instance.
        ``ROLLBACK_RESTORE_UNSET`` means the field wasn't loaded before the write. A no-op outside a
        transaction.

        The instance keeps a stack of layers, one per open savepoint span, each restored by the
        rollback of its own scope. Within a layer the first value registered for a field wins - it
        is the value from before any of the layer's writes.

        A layer whose connection is finalized but which is still on the stack was abandoned without
        its callbacks running (a ``Transactions.distributed()`` participant whose ``COMMIT
        PREPARED`` failed) - it is dropped here.
        """
        if not isinstance(using, TransactionClient):
            return
        registered_on = Atomic.get_connection(using.connection_name)
        current_span = current_savepoint_span.get()
        stack: list[dict[str, Any]] = getattr(self, "_pending_rollback_restore_stack", None) or []
        while stack and stack[-1]["client"]._finalized:
            stack.pop()
        if stack and stack[-1]["span"] is current_span:
            layer = stack[-1]
        else:
            layer = {"span": current_span, "client": registered_on, "pending": {}}
            stack.append(layer)
            object.__setattr__(self, "_pending_rollback_restore_stack", stack)

            def _on_rollback() -> None:
                self._pop_pending_rollback_restore_layer(layer)
                for name, value in layer["pending"].items():
                    if name == self._meta.soft_delete_field:
                        self._restore_soft_delete_field(
                            value is not ROLLBACK_RESTORE_UNSET,
                            None if value is ROLLBACK_RESTORE_UNSET else value,
                        )
                    else:
                        self._restore_field_value(name, value)

            def _on_commit() -> None:
                self._pop_pending_rollback_restore_layer(layer)

            Transactions.on_rollback(_on_rollback, using=using.connection_name)
            Transactions.on_commit(_on_commit, using=using.connection_name)
        if field_name not in layer["pending"]:
            layer["pending"][field_name] = old_value

    def snapshot(self, fields: Iterable[str] | None = None) -> FieldSnapshot:
        """Captures an immutable copy of the instance's direct field values.

        Args:
            fields: Direct field names to capture. ``None`` captures every loaded direct field.

        Returns:
            A ``FieldSnapshot`` to pass to ``diff_against()`` later.

        Raises:
            TypeError: If ``fields`` is a bare string instead of an iterable of names.
            FieldError: If a name isn't a direct field of this model.
            IncompleteInstanceError: If a named field wasn't loaded by ``.only()``/``.defer()``.
        """
        if fields is None:
            return FieldSnapshot(type(self), self._capture_field_values(self._meta.direct_fields))
        if isinstance(fields, str):
            raise TypeError(f"snapshot() fields must be an iterable of field names, not a string: {fields!r}")
        field_names = list(dict.fromkeys(fields))
        direct_fields = self._meta.direct_fields
        for field_name in field_names:
            if field_name not in direct_fields:
                raise FieldError(
                    f"{type(self).__name__} has no direct field {field_name!r} to snapshot "
                    f"(available: {', '.join(sorted(direct_fields))})"
                )
            if not hasattr(self, field_name):
                raise IncompleteInstanceError(
                    f"{type(self).__name__} is a partial model, field {field_name!r} is not loaded"
                )
        return FieldSnapshot(type(self), self._capture_field_values(field_names))

    def diff_against(self, snapshot: FieldSnapshot) -> dict[str, tuple[Any, Any]]:
        """Compares the instance's current values with a snapshot's.

        Args:
            snapshot: A ``FieldSnapshot`` taken from an instance of this model.

        Returns:
            ``{field: (snapshot_value, current_value)}`` for every snapshotted field that changed -
            ``sensitive=True`` fields included with their real values, so mask them before logging
            the result.

        Raises:
            TypeError: If ``snapshot`` isn't a ``FieldSnapshot`` of this model.
        """
        if not isinstance(snapshot, FieldSnapshot):
            raise TypeError(f"diff_against() expects a FieldSnapshot, got {type(snapshot).__name__}")
        if not isinstance(self, snapshot.model_class):
            raise TypeError(
                f"Can't diff a {type(self).__name__} instance against a snapshot of {snapshot.model_class.__name__}"
            )
        return self._diff_field_values(snapshot, snapshot)

    def _capture_field_values(self, field_names: Iterable[str]) -> dict[str, Any]:
        """Copies the loaded fields among ``field_names``, deep-copying only mutable values."""
        values = {}
        copy_value = FieldSnapshot.copy_value
        for field_name in field_names:
            if hasattr(self, field_name):
                values[field_name] = copy_value(getattr(self, field_name))
        return values

    def _diff_field_values(
        self,
        baseline_values: Mapping[str, Any],
        field_names: Iterable[str],
    ) -> dict[str, tuple[Any, Any]]:
        """Returns ``{field: (old, new)}`` for fields whose current value differs from the baseline
        (a field missing from the baseline counts as ``None``); a JSON value that only changed a
        nested type (``1`` to ``True``) counts as different too."""
        changes: dict[str, tuple[Any, Any]] = {}
        fields_map = self._meta.fields_map
        for field_name in field_names:
            old_value = baseline_values.get(field_name)
            current_value = getattr(self, field_name, None)
            if FieldSnapshot.values_differ(
                old_value, current_value, compare_types=isinstance(fields_map.get(field_name), JSONField)
            ):
                changes[field_name] = (old_value, current_value)
        return changes

    def _snapshot_dirty_fields(self) -> None:
        """Takes/refreshes the ``.get_dirty_fields()`` baseline - called after hydration from the
        DB and after a successful ``save()``, only when ``Meta.track_dirty_fields`` is set."""
        object.__setattr__(self, "_dirty_snapshot", self._capture_field_values(self._meta.direct_fields))

    def get_dirty_fields(self) -> dict[str, tuple[Any, Any]]:
        """
        Fields whose current value differs from the value this instance was last hydrated from
        the DB with (or from nonexistence, for an instance that's never been saved - every field
        is dirty relative to not existing at all).

        Raises:
            QueryError: If the model has no ``Meta.track_dirty_fields = True``.
        """
        if not self._meta.track_dirty_fields:
            raise QueryError(f"{type(self).__name__} has no Meta.track_dirty_fields configured")
        direct_fields = self._meta.direct_fields
        dirty_snapshot: dict[str, Any] | None = getattr(self, "_dirty_snapshot", None)
        if dirty_snapshot is None:
            return {field_name: (None, getattr(self, field_name, None)) for field_name in direct_fields}
        return self._diff_field_values(dirty_snapshot, direct_fields)

    def _sync_dirty_snapshot_fields(
        self,
        fields: Iterable[str],
        using: DatabaseClient | None = None,
    ) -> None:
        """Updates the ``get_dirty_fields()`` baseline of the written fields only - other unsaved
        changes stay dirty. A field left unloaded is skipped.

        Args:
            fields: The written field names.
            using: Connection of the write - the previous baseline comes back if its transaction
                rolls back.
        """
        if not self._meta.track_dirty_fields:
            return
        dirty_snapshot = getattr(self, "_dirty_snapshot", None)
        if using is not None:
            self._register_rollback_restore(
                using, "_dirty_snapshot", dict(dirty_snapshot) if dirty_snapshot is not None else None
            )
        if dirty_snapshot is not None:
            copy_value = FieldSnapshot.copy_value
            for field in fields:
                if not hasattr(self, field):
                    continue
                # Copied, not stored as-is - otherwise a later in-place mutation of a JSON dict/
                # list would mutate the baseline right along with the live value.
                dirty_snapshot[field] = copy_value(getattr(self, field))

    @staticmethod
    def _fk_setter(
        instance: Model,
        value: Model | None,
        _key: str,
        relation_fields: tuple[str, ...],
        to_fields: tuple[str, ...],
    ) -> None:
        to_field_values = (
            value._get_relation_key_values(to_fields, f"Assigning '{_key.removeprefix('_')}'")
            if value
            else [None] * len(to_fields)
        )
        for relation_field, to_field_value in zip(relation_fields, to_field_values, strict=True):
            setattr(instance, relation_field, to_field_value)
        setattr(instance, _key, value)

    def _get_relation_key_values(self, field_names: Iterable[str], usage: str) -> list[Any]:
        """The values of the fields a relation references this instance by.

        Args:
            field_names: The referenced (``to_field``) field names.
            usage: What needs the values, for the error message.

        Returns:
            The values, in ``field_names`` order.

        Raises:
            QueryError: One of the fields was left unloaded by ``.only()``/``.defer()``.
        """
        values = []
        for field_name in field_names:
            if not hasattr(self, field_name):
                model_name = type(self).__name__
                raise QueryError(
                    f"{usage} needs {model_name}.{field_name}, which this {model_name} instance didn't load "
                    f"(left out by .only()/.defer()) - load '{field_name}' too"
                )
            values.append(getattr(self, field_name))
        return values

    @staticmethod
    def _fk_getter(
        instance: Model, _key: str, ftype: type[Model], relation_fields: tuple[str, ...], to_fields: tuple[str, ...]
    ) -> Awaitable[Model | None]:
        try:
            return getattr(instance, _key)
        except AttributeError:
            values = [getattr(instance, relation_field) for relation_field in relation_fields]
            set_values = [value for value in values if value is not None]
            if not set_values:
                return NoneAwaitable
            if len(set_values) != len(values):
                # Some key columns of a composite relation set and some not: neither a reference nor
                # "no relation".
                raise IncompleteInstanceError(
                    f"{_key!r} on {type(instance).__name__} has only some of its composite key "
                    f"columns set ({dict(zip(relation_fields, values, strict=True))!r}) - set all "
                    "of them, or none."
                )
            return ftype._get_related_queryset(dict(zip(to_fields, values, strict=True)), instance).first()

    @classmethod
    def _get_related_queryset(cls: type[TModel], filters: dict[str, Any], instance: Model) -> QuerySet[TModel]:
        """A new queryset of this model's rows a relation of ``instance`` points at, read on the
        connection ``instance`` came from.

        Args:
            filters: The relation field names of this model to the values they must equal.
            instance: The instance the relation is read from.

        Returns:
            The queryset.
        """
        queryset = cast("QuerySet[TModel]", cls._meta.manager.get_queryset())
        queryset._append_filters(False, (), filters)
        queryset._set_instance_connection(instance)
        return queryset

    @staticmethod
    def _rfk_getter(
        instance: Model,
        _key: str,
        ftype: type[Model],
        frelfields: tuple[str, ...],
        from_fields: tuple[str, ...],
        bind: bool = True,
    ) -> ReverseRelation[Model]:
        relation = getattr(instance, _key, None)
        if relation is None:
            relation_class = cast("type[ReverseRelation[Model]]", ReverseRelation.get_class_for(ftype))
            relation = relation_class(ftype, frelfields, instance, from_fields)
            setattr(instance, _key, relation)
        if bind:
            relation._bind()
        return relation

    @staticmethod
    def _ro2o_getter(
        instance: Model, _key: str, ftype: type[Model], frelfields: tuple[str, ...], from_fields: tuple[str, ...]
    ) -> QuerySetSingle[Model | None]:
        # Not cached: the fields the relation is read by can still change (an unsaved instance, a
        # clone given a new pk). A value select_related()/prefetch_related() set is returned as is.
        if hasattr(instance, _key):
            return getattr(instance, _key)

        values = instance._get_relation_key_values(from_fields, f"Reading '{_key.removeprefix('_')}'")
        if None in values:
            # A NULL target value (a nullable to_field=, or an unsaved pk) is referenced by no row.
            return cast("QuerySetSingle[Model | None]", NoneAwaitable)
        return ftype._get_related_queryset(dict(zip(frelfields, values, strict=True)), instance).first()

    @staticmethod
    def _m2m_getter(
        instance: Model, _key: str, field_object: ManyToManyFieldInstance[Model], bind: bool = True
    ) -> ManyToManyRelation[Model]:
        relation = getattr(instance, _key, None)
        if relation is None:
            relation_class = cast(
                "type[ManyToManyRelation[Model]]", ManyToManyRelation.get_class_for(field_object.related_model)
            )
            relation = relation_class(instance, field_object)
            setattr(instance, _key, relation)
        if bind:
            relation._bind()
        return relation

    def _get_relation(self, field_name: str) -> RelatedQuerySet[Any]:
        """The to-many relation ``field_name`` of the instance as the holder of its fetched rows -
        not made ready to query, which an unsaved instance can't be.

        Args:
            field_name: A backward foreign key or many-to-many field name.

        Returns:
            The relation.
        """
        return cast("RelatedQuerySet[Any]", getattr(type(self), field_name).fget(self, bind=False))

    @classmethod
    def _validate_relation_type(cls, field_key: str, value: Model | None) -> None:
        if value is None:
            return

        field = cls._meta.fields_map[field_key]
        if not isinstance(field, (OneToOneFieldInstance, ForeignKeyFieldInstance)):
            raise FieldError(
                f"Field '{field_key}' must be a OneToOne or ForeignKey relation, got {type(field).__name__}"
            )

        expected_model = field.related_model
        received_model = type(value)
        if received_model is not expected_model:
            raise ValidationError(
                f"Invalid type for relationship field '{field_key}'. "
                f"Expected model type '{expected_model.__name__}', but got '{received_model.__name__}'. "
                "Make sure you're using the correct model class for this relationship."
            )
        if not value._saved_in_db:
            raise QueryError(f"You should first call .save() on {value!r} before referring to it")
        for to_field_instance in field.to_field_instances:
            to_field_name = to_field_instance.model_field_name
            # hasattr() first: a .only()/.defer() instance that never loaded the target column
            # has no attribute at all, which is not the same as a loaded, still-None one.
            if hasattr(value, to_field_name) and getattr(value, to_field_name) is None:
                raise QueryError(
                    f"{type(value).__name__} is marked saved but its '{to_field_name}' is None (saved by "
                    f"bulk_create() without returning=True?), so '{field_key}' would silently be stored as "
                    "NULL - use bulk_create(returning=True), or reload the object first"
                )

    def _get_lazy_joined_relation_names_needing_refresh(self, refresh_fields: set[str]) -> frozenset[str]:
        """Forward FK/O2O relation names declared ``lazy="joined"`` whose own shadow column(s)
        are among ``refresh_fields``.

        Args:
            refresh_fields: The field names a partial ``refresh_from_db(fields=...)`` is about
                to refresh.
        Returns:
            The matching relation names.
        """
        relation_names_needing_refresh = set()
        for relation_name in self._meta.fk_fields | self._meta.o2o_fields:
            relation_field = cast("RelationalField[Any]", self._meta.fields_map[relation_name])
            if getattr(relation_field, "lazy", None) != RelationLoadStrategy.JOINED:
                continue
            if set(relation_field.source_fields) & refresh_fields:
                relation_names_needing_refresh.add(relation_name)
        return frozenset(relation_names_needing_refresh)

    async def refresh_from_db(
        self,
        fields: Iterable[str] | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None:
        """
        Refresh latest data from db. When this method is called without arguments
        all db fields of the model are updated to the values currently present in the database.

        .. code-block:: python3

            user.refresh_from_db(fields=["name"])

        Args:
            fields: The special fields that to be refreshed - an empty collection is a no-op.
            using: Specific DB connection to use instead of default bound.

        Raises:
            FieldError: If ``fields`` names an unknown field - a forward relation refreshes its
                key column(s).
            IncompleteInstanceError: If a ``.only(...)``/``.defer(...)`` query left the
                primary key itself unloaded.
            QueryError: If object has never been persisted.
            QueryError: If ``fields`` is a single string instead of a collection.
        """
        self._meta.raise_if_no_primary_key(f"{self.__class__.__name__}.refresh_from_db()")
        if fields is not None:
            fields = self._get_validated_field_names(fields, "fields")
            if not fields:
                return
        if not self._saved_in_db:
            raise QueryError("Can't refresh unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.pk_attr_names):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model without primary key fetched. Can't refresh it."
            )
        db = Connections.get_client(using) or self._get_connection_for_instance()
        # Read through the base manager with the model's default scope. An instance soft-deleted in
        # memory is refreshed with the soft-delete filter lifted - its row can't be found otherwise.
        soft_delete_field = self._meta.soft_delete_field
        is_soft_deleted_in_memory = (
            soft_delete_field is not None and getattr(self, soft_delete_field, None) is not None
        )
        queryset = RowScopes.get_base_queryset(
            self.__class__, RowVisibility(include_deleted=is_soft_deleted_in_memory)
        ).using(db)
        if fields is not None:
            # This query's own .only() would skip the JOIN of a lazy="joined" relation -
            # select_related() forces it for the relations being refreshed.
            lazy_joined_relations_needing_refresh = self._get_lazy_joined_relation_names_needing_refresh(set(fields))
            if lazy_joined_relations_needing_refresh:
                queryset = queryset.select_related(*lazy_joined_relations_needing_refresh)
            queryset = queryset.only(*fields)
        obj = await queryset.get(pk=self.pk)

        # Field names, not column names - what .only() above takes.
        refresh_fields = fields if fields is not None else self._meta.fields_db_projection.keys()
        refresh_fields_set = set(refresh_fields)
        _setattr = object.__setattr__
        if self._meta.soft_delete_field is not None and self._meta.soft_delete_field in refresh_fields:
            _setattr(self, "_allow_soft_delete_write", True)
        try:
            for field in refresh_fields:
                setattr(self, field, getattr(obj, field, None))
        finally:
            _setattr(self, "_allow_soft_delete_write", False)

        # The refresh query loaded the lazy="joined"/"select" relations it touched - carried over,
        # since setting the key columns dropped this instance's cached objects.
        for relation_name in self._meta.fk_fields | self._meta.o2o_fields:
            relation_field = cast("RelationalField[Any]", self._meta.fields_map[relation_name])
            if getattr(relation_field, "lazy", None) not in (RelationLoadStrategy.JOINED, RelationLoadStrategy.SELECT):
                continue
            if not set(relation_field.source_fields) & refresh_fields_set:
                continue
            cache_key = f"_{relation_name}"
            if hasattr(obj, cache_key):
                _setattr(self, cache_key, getattr(obj, cache_key))
            elif hasattr(self, cache_key):
                object.__delattr__(self, cache_key)

        if not fields:
            # The reverse and many-to-many caches of a fully refreshed instance are dropped - read
            # again, they are fetched again.
            for relation_name in (
                self._meta.backward_fk_fields | self._meta.backward_o2o_fields | self._meta.m2m_fields
            ):
                cache_key = f"_{relation_name}"
                if hasattr(self, cache_key):
                    object.__delattr__(self, cache_key)
            # A lazy="select" M2M is auto-prefetched onto `obj` by the refresh query itself - its
            # fresh result is handed to this instance's own (just reset) relation container.
            for relation_name in self._meta.m2m_fields:
                relation_field = cast("RelationalField[Any]", self._meta.fields_map[relation_name])
                if getattr(relation_field, "lazy", None) != RelationLoadStrategy.SELECT:
                    continue
                refreshed_relation = obj._get_relation(relation_name)
                if refreshed_relation._fetched:
                    relation = self._get_relation(relation_name)
                    relation._set_result_for_query(list(refreshed_relation.related_objects))
        # A refresh of every field makes a partially loaded instance complete; a refresh of some
        # fields doesn't.
        if not fields:
            _setattr(self, "_partial", False)
        # The refreshed values are the new dirty-tracking baseline.
        if self._meta.track_dirty_fields:
            if fields:
                self._sync_dirty_snapshot_fields(refresh_fields)
            else:
                self._snapshot_dirty_fields()

    @classmethod
    def _check(cls) -> None:
        """
        Calls various checks to validate the model.

        Raises:
            ConfigurationError: If the model has not been configured correctly.
        """
        cls._check_together(ModelOption.INDEXES)

    @classmethod
    def _check_together(cls, together: str) -> None:
        """
        Check the value of a list-of-field-names option.

        Raises:
            ConfigurationError: If the model has not been configured correctly.
        """
        _together = getattr(cls._meta, together)
        if not isinstance(_together, (tuple, list)):
            raise ConfigurationError(f"'{cls.__name__}.{together}' must be a list or tuple.")

        if any(not isinstance(unique_fields, (tuple, list, Index)) for unique_fields in _together):
            raise ConfigurationError(f"All '{cls.__name__}.{together}' elements must be lists or tuples.")

        for fields_tuple in _together:
            if isinstance(fields_tuple, Index):
                fields_tuple = fields_tuple.fields
            for field_name in fields_tuple:
                field = cls._meta.fields_map.get(field_name)

                if not field:
                    raise ConfigurationError(f"'{cls.__name__}.{together}' has no '{field_name}' field.")

                if isinstance(field, ManyToManyFieldInstance):
                    raise ConfigurationError(
                        f"'{cls.__name__}.{together}' '{field_name}' field refers to ManyToMany field."
                    )

    @classmethod
    def _get_validated_field_names(cls, field_names: Iterable[str], argument_name: str) -> tuple[str, ...]:
        """
        Materializes a caller-supplied collection of field names once and validates every name.

        Args:
            field_names: Model field names, any iterable (a generator is consumed here, once).
            argument_name: The caller's parameter name, used in raised messages.

        Returns:
            The names without duplicates, in the given order, ``pk`` expanded to the
            primary key attribute(s) and a forward relation to its key column(s).

        Raises:
            QueryError: If ``field_names`` is a single string instead of a collection of names.
            FieldError: If a name is not a column-backed field or a forward relation of this model.
        """
        meta = cls._meta
        if isinstance(field_names, (str, bytes)):
            raise QueryError(
                f"'{argument_name}' must be a collection of field names, not a single string - "
                f"pass [{field_names!r}] instead of {field_names!r}"
            )
        validated_names: dict[str, None] = {}
        for field_name in field_names:
            if field_name == "pk":
                validated_names.update(dict.fromkeys(meta.pk_attr_names))
            elif field_name in meta.fields_db_projection:
                validated_names[field_name] = None
            elif field_name in meta.fk_fields or field_name in meta.o2o_fields:
                # A forward relation stands for its key column(s), like Django.
                source_fields = cast("RelationalField[Any]", meta.fields_map[field_name]).source_fields
                validated_names.update(dict.fromkeys(source_fields))
            elif (
                field_name in meta.backward_fk_fields
                or field_name in meta.backward_o2o_fields
                or (field_name in meta.m2m_fields)
            ):
                raise FieldError(
                    f"'{argument_name}' names the relation '{field_name}' of model {meta.full_name}, "
                    "which has no column on this model's table"
                )
            else:
                raise FieldError(f"Unknown field '{field_name}' in '{argument_name}' for model {meta.full_name}")
        return tuple(validated_names)

    @classmethod
    def get_table(cls) -> Table:
        """Return a hare.sql table for this model."""
        return Table(name=cls._meta.db_table, schema=cls._meta.schema)
