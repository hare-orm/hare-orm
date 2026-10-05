from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generator, TypeVar, cast

from hare.classes.value_descriptions import ValueDescriptions
from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.exceptions import (
    ConfigurationError,
    FieldError,
    IncompleteInstanceError,
    NoValuesFetched,
    QueryError,
)
from hare.fields.constants import SENSITIVE_VALUE_PLACEHOLDER
from hare.fields.field import Field
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.constants import (
    CLONE_PK_NOT_GIVEN,
    EMPTY_PENDING_DEFAULTS,
    MISSING_DEFAULT_CONNECTION_MESSAGE,
    PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE,
)
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.deletion.instance_deletion import InstanceDeletion
from hare.models.deletion.preview.delete_preview_builder import DeletePreviewBuilder
from hare.models.deletion.soft_deletion import SoftDeletion
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.instances.field_snapshot import FieldSnapshot
from hare.models.instances.instance_connections import InstanceConnections
from hare.models.instances.instance_copies import InstanceCopies
from hare.models.instances.instance_initialization import InstanceInitialization
from hare.models.instances.instance_refresh import InstanceRefresh
from hare.models.instances.instance_saving import InstanceSaving
from hare.models.meta_info import MetaInfo
from hare.models.model_meta import ModelMeta
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.rules.tenant_write_rules import TenantWriteRules
from hare.models.write.write_steps import WriteSteps
from hare.query.managers.base_manager import BaseManager
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.sql import Table

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.deletion.preview.delete_preview import DeletePreview

TModel = TypeVar("TModel", bound="Model")


class Model(metaclass=ModelMeta):
    """
    Base class for all Hare ORM Models: construction, relation attributes, change tracking and
    the instance's own writes (``save``/``delete``/``hard_delete``/``restore``/
    ``refresh_from_db``). Queries start at ``Model.objects``, not on the class itself.
    """

    #: The model's Meta - the base model's empty one here, every model class gets its own.
    _meta = MetaInfo(None)

    #: Where the model's queries start: ``Model.objects`` is a queryset of its rows. Set per
    #: model class by the metaclass - the ``objects`` the class declares, ``Meta.manager``, or
    #: a plain ``Manager``.
    objects: ClassVar[BaseManager]

    # Type-only (no value assigned) - set through object.__setattr__, past the model's own __setattr__,
    # which mypy can't infer an attribute's type from.
    _saved_in_db: bool
    # Class defaults - an instance gets one of its own only where it differs, so reading a row or
    # making an instance sets none of them.
    _partial: bool = False
    _custom_generated_pk: bool = False
    _await_when_save: Mapping[str, Any] = EMPTY_PENDING_DEFAULTS
    #: None - a never-persisted instance has no prior state to diff against, so get_dirty_fields()
    #: treats every field as dirty relative to nonexistence.
    _dirty_snapshot: dict[str, Any] | None = None
    #: The alias of the connection this instance was loaded from or last saved to - None for an
    #: instance that never touched the database. Set per instance via object.__setattr__.
    _connection_alias: str | None = None
    #: Whether ``Meta.soft_delete_field`` may be set directly - only while a soft delete, a restore
    #: or a refresh writes it.
    _allow_soft_delete_write: bool = False
    #: The values to put back on the instance should the transactions of its writes roll back - one
    #: layer per open savepoint span; None until a write in a transaction registers one.
    _pending_rollback_restore_stack: list[dict[str, Any]] | None = None

    def __init__(self, **kwargs: Any) -> None:
        meta = self._meta
        if meta.computed_field_names and not meta.computed_field_names.isdisjoint(kwargs):
            InstanceInitialization.raise_for_computed_fields(type(self), kwargs)
        if meta.generic_foreign_key_fields:
            kwargs = GenericForeignKeys.expand_kwargs(meta, kwargs, apply_defaults=True)
        constructor = meta.instance_constructor
        if constructor is None:
            constructor = meta.get_instance_constructor()
        if constructor and constructor.construct(self, kwargs):
            return
        # Not field names - set past the overridden __setattr__.
        _setattr = object.__setattr__
        _setattr(self, "_saved_in_db", False)

        # Assign defaults for missing fields. A plain column of the new instance is set directly,
        # as in InstanceInitialization.set_kwargs() - Model.__setattr__ has nothing to refresh on it
        # yet; a relation, an automatic primary key and a model overriding __setattr__ still go
        # through it.
        sets_columns_directly = type(self).__setattr__ is Model.__setattr__
        fields_db_projection = meta.fields_db_projection
        generated_pk_field_name = meta.generated_pk_field_name
        # A field the database computes holds no value until a row of the instance is read.
        for key in meta.fields.difference(
            InstanceInitialization.set_kwargs(self, kwargs, assigns_directly=sets_columns_directly),
            meta.computed_field_names,
        ):
            InstanceInitialization.assign_default(
                self,
                key,
                object.__setattr__
                if sets_columns_directly and key in fields_db_projection and key != generated_pk_field_name
                else setattr,
            )

    def __setattr__(self, key: str, value: Any) -> None:
        meta = self._meta
        hooks = meta.setattr_hooks
        if hooks is None:
            hooks = meta.get_setattr_hooks()
        # The instance's pending async defaults, else the class's empty ones - read as an attribute,
        # not off __dict__, which an instance keeping its attributes inline would have to make.
        pending_defaults: Any = self._await_when_save
        if key not in hooks:
            # A plain attribute: stored, overriding a pending async default.
            if pending_defaults:
                pending_defaults.pop(key, None)
            meta.next_setattr(self, key, value)
            return
        # A field's value replaces its pending async default.
        if pending_defaults:
            pending_defaults.pop(key, None)
        if meta.soft_delete_field is not None and key == meta.soft_delete_field:
            SoftDeletion.check_soft_delete_write_allowed(self, key)
        if key in meta.foreign_key_fields or key in meta.one_to_one_fields:
            RelationValues.validate_relation_type(type(self), key, value)
        elif (cache_key := meta.foreign_key_shadow_columns.get(key)) is not None:
            # A key column assigned directly (author_id = 2) drops the cached related object of its
            # relation.
            if hasattr(self, cache_key):
                object.__delattr__(self, cache_key)
        elif key == meta.generated_pk_field_name:
            # A value assigned to an auto-generated pk of an instance not saved yet reaches the
            # INSERT; None hands the pk back to the database.
            if value is not None and not getattr(self, "_saved_in_db", False):
                object.__setattr__(self, "_custom_generated_pk", True)
            else:
                # The class default, False.
                self.__dict__.pop("_custom_generated_pk", None)
        super().__setattr__(key, value)

    def __str__(self) -> str:
        return f"{self.__class__.__name__} object ({self._get_shown_pk_if_known()})"

    def _get_shown_pk_if_known(self) -> str | None:
        """The primary key as ``str()`` and ``repr()`` show it, None when it is unset or a
        ``.only()``/``.defer()`` query left it out.

        Returns:
            The key's text, or None.
        """
        try:
            if self._pk_is_unset():
                return None
        except NoValuesFetched:
            return None
        return self._get_shown_pk()

    def _get_shown_pk(self) -> str:
        """The primary key as ``str()`` and ``repr()`` show it - a sensitive field's part hidden.

        Returns:
            The key's text.
        """
        meta = self._meta
        fields_map = meta.fields_map
        names = meta.primary_key_attribute_names
        if not any(fields_map[name].sensitive for name in names):
            return str(self.pk)
        shown_parts = [
            SENSITIVE_VALUE_PLACEHOLDER if fields_map[name].sensitive else repr(getattr(self, name, None))
            for name in names
        ]
        return shown_parts[0] if len(names) == 1 else f"({', '.join(shown_parts)})"

    def _pk_is_unset(self) -> bool:
        # `is None`, not truthiness: a composite pk is a non-empty tuple even when unset, and 0 is a
        # valid pk.
        pk = self.pk
        return any(component is None for component in pk) if isinstance(pk, tuple) else pk is None

    def __getattr__(self, name: str) -> Any:
        # Reached only for an attribute the instance doesn't hold - a field a .only()/.defer()
        # query left out, or one a write returning no row left unloaded, is named as such; an
        # internal cache attribute misses at no extra cost.
        if name[:1] != "_" and (self._partial or name in self._meta.fields_map):
            meta = self._meta
            if name == "pk" or name in meta.primary_key_attribute_names:
                # The pk property raised too - refresh_from_db() needs the key itself.
                raise NoValuesFetched(
                    f"{type(self).__name__}.{name} wasn't fetched - a .only()/.defer() query left the primary key "
                    "out; name it in .only() to read, save or refresh the object"
                )
            if not self._saved_in_db and name in meta.computed_field_names:
                raise NoValuesFetched(
                    f"{type(self).__name__}.{name} is computed by the database - it holds a value once the object's "
                    "row is written and read"
                )
            # A generic foreign key's branch keys left out raise from its descriptor, read here again.
            if name in meta.fields_map or name in meta.generic_foreign_key_fields:
                raise NoValuesFetched(
                    f"{type(self).__name__}.{name} wasn't fetched - a .only()/.defer() query, or a write returning "
                    f"no row, left it out; load it with refresh_from_db(fields=[{name!r}])"
                )
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __repr__(self) -> str:
        shown_pk = self._get_shown_pk_if_known()
        if shown_pk is not None:
            return f"<{self.__class__.__name__}: {shown_pk}>"
        return f"<{self.__class__.__name__}>"

    def __hash__(self) -> int:
        if not self._meta.has_primary_key:
            # A row of a model without a primary key is only ever equal to itself.
            return object.__hash__(self)
        try:
            pk = self.pk
        except NoValuesFetched:
            raise TypeError("Model instances without a fetched primary key are unhashable") from None
        if (isinstance(pk, tuple) and any(component is None for component in pk)) or pk is None:
            raise TypeError("Model instances without id are unhashable")
        return hash(pk)

    def __iter__(self) -> Any:
        # The field names, not the column names - a field whose source_field differs from its name is
        # read by its name.
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
        try:
            pk = self.pk
            other_pk = other.pk  # type: ignore[attr-defined]
        except NoValuesFetched:
            # A key a .only()/.defer() query left out is unknown - the instance equals only itself.
            return self is other
        # An unsaved instance equals only itself - two of them both have an unset pk.
        if (isinstance(pk, tuple) and any(component is None for component in pk)) or pk is None:
            return self is other
        return pk == other_pk

    def _get_pk_value(self) -> Any:
        # primary_key_attribute is the field name of a single-column key, a tuple of names for a composite one
        # and () without a key - read straight off it, every `.pk` access goes through here.
        # The key of an instance a .only()/.defer() query left it out of raises - None would read as
        # an unsaved instance.
        primary_key_attribute = self._meta.primary_key_attribute
        if type(primary_key_attribute) is str:
            try:
                return getattr(self, primary_key_attribute)
            except NoValuesFetched:
                raise
            except AttributeError:
                return None
        if not primary_key_attribute:
            return None
        if self._partial:
            return tuple(getattr(self, name) for name in primary_key_attribute)
        return tuple(getattr(self, name, None) for name in primary_key_attribute)

    def _set_pk_value(self, value: Any) -> None:
        self._meta.raise_if_no_primary_key("setting pk")
        if self._meta.has_composite_primary_key:
            if not isinstance(value, tuple) or len(value) != len(self._meta.primary_key_attribute):
                raise QueryError(
                    f"Composite pk must be set to a {len(self._meta.primary_key_attribute)}-tuple matching "
                    f"{self._meta.primary_key_attribute}, got {ValueDescriptions.get_description(value)}"
                )
            for name, part in zip(self._meta.primary_key_attribute, value, strict=True):
                setattr(self, name, part)
            return
        setattr(self, cast("str", self._meta.primary_key_attribute), value)

    pk = property(_get_pk_value, _set_pk_value)
    """The value of the model's primary key - a tuple of a composite one. ``pk`` names the key in
    filters and orderings too (``.filter(pk=...)``)."""

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
        pending_defaults = self.__dict__.get("_await_when_save")
        if pending_defaults is not None:
            _setattr(obj, "_await_when_save", dict(pending_defaults))

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
        # The copy's to-many relations unfetched - not the original's rows.
        for key in (*meta.backward_foreign_key_fields, *meta.many_to_many_fields):
            cache_key = f"_{key}"
            if cache_key in obj.__dict__:
                _setattr(obj, cache_key, RelationRows())
        for key in meta.backward_one_to_one_fields:
            cache_key = f"_{key}"
            if cache_key in obj.__dict__:
                object.__delattr__(obj, cache_key)

        # Rollback bookkeeping never transfers to a copy - RollbackRestores.register_rollback_restore()'s pending
        # stack (and the live DB clients it references) is scoped to the ORIGINAL instance's own
        # writes inside a still-open transaction, meaningless once duplicated onto a second one.
        if "_pending_rollback_restore_stack" in obj.__dict__:
            object.__delattr__(obj, "_pending_rollback_restore_stack")

        return obj

    def clone(self: TModel, pk: Any = CLONE_PK_NOT_GIVEN) -> TModel:
        """An unsaved copy of the object - saving it inserts a new row.

        Args:
            pk: The copy's primary key; without it the copy gets the key's default (a key the database
                generates is left to it). Needed when the key has no default.

        Returns:
            A copy of the current object without primary key information, with every
            ``auto_now_add`` field reset so the new row gets its own first-save value, without the
            values the database computed for the original's row, and live - its
            ``Meta.soft_delete_field`` cleared.

        Raises:
            QueryError: If pk is required but not provided.
            ConfigurationError: ``pk`` is given for a model without a primary key.
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
        # The values the database computed belong to the original's row - the copy's are read with its own.
        for field_name in self._meta.computed_field_names:
            obj.__dict__.pop(field_name, None)
        # A copy of a soft-deleted row isn't born deleted.
        soft_delete_field = self._meta.soft_delete_field
        if soft_delete_field is not None:
            setattr(obj, soft_delete_field, None)
        if not self._meta.has_primary_key:
            # No key to reset - the copy is a new row as it is.
            if pk is not CLONE_PK_NOT_GIVEN:
                self._meta.raise_if_no_primary_key(f"{type(self).__name__}.clone(pk=...)")
            return obj
        if pk is CLONE_PK_NOT_GIVEN:
            if self._meta.has_composite_primary_key:
                # Every component of a composite PK is resolved independently, the same way a
                # single-column PK is below - a component with its own default=/db_default=
                # (e.g. a version field) gets a fresh value; a component with neither raises.
                for field_name, field in zip(self._meta.primary_key_attribute, self._meta.pk_fields, strict=True):
                    InstanceCopies.apply_clone_pk_component_default(obj, field_name, field)
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
                    InstanceInitialization.add_pending_default(
                        obj, cast("str", self._meta.primary_key_attribute), pk_default
                    )
                elif callable(pk_default):
                    obj.pk = pk_default()
                else:
                    obj.pk = pk_default
            else:
                obj.pk = None
        else:
            obj.pk = pk
        return obj

    @classmethod
    def construct(cls: type[TModel], *, _saved_in_db: bool = False, **kwargs: Any) -> TModel:
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

        _setattr(self, "_saved_in_db", _saved_in_db)

        # Track source fields that are auto-populated from FK/O2O objects
        # so that the default-setting loop doesn't overwrite them with None.
        populated_source_fields: set[str] = set()

        for key, value in kwargs.items():
            if key in meta.backward_foreign_key_fields or key in meta.many_to_many_fields:
                # A to-many relation given its rows - fetched; its relation object is made when read.
                relation_rows = RelationRows()
                relation_rows._fetched = True
                relation_rows.related_objects = list(value)
                _setattr(self, f"_{key}", relation_rows)
            elif key in meta.backward_one_to_one_fields:
                # Backward O2O: store at _{key} for property getter
                _setattr(self, f"_{key}", value)
            elif key in meta.foreign_key_fields or key in meta.one_to_one_fields:
                # FK/O2O: store at _{key} for property getter, also set source field(s) - one
                # per composite-PK-target component when the target's PK isn't a single column.
                _setattr(self, f"_{key}", value)
                foreign_key_field = cast("RelationalField[Any]", meta.fields_map[key])
                if foreign_key_field.to_field_instances:
                    for source_field, to_field_instance in zip(
                        foreign_key_field.source_fields, foreign_key_field.to_field_instances, strict=True
                    ):
                        if value is not None:
                            _setattr(self, source_field, getattr(value, to_field_instance.model_field_name, None))
                        else:
                            _setattr(self, source_field, None)
                        populated_source_fields.add(source_field)
            else:
                # Data fields, source fields, or unknown fields: store directly
                _setattr(self, key, value)

        # The key columns a relation's object filled in keep its values.
        InstanceInitialization.set_constructed_defaults(
            self, meta.fields.difference(kwargs, meta.fetch_fields, populated_source_fields, meta.computed_field_names)
        )

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
        # Unknown keys are dropped here - InstanceInitialization.set_kwargs() raises for them.
        known_data = {
            key: value
            for key, value in data.items()
            if key == "pk"
            or key in meta.foreign_key_fields
            or key in meta.one_to_one_fields
            or key in meta.fields_db_projection
            or key in meta.backward_foreign_key_fields
            or key in meta.backward_one_to_one_fields
            or key in meta.many_to_many_fields
            or key in meta.generic_foreign_key_fields
        }
        InstanceInitialization.set_kwargs(self, known_data)
        return self

    @classmethod
    def get_connection(cls, *, for_write: bool = False) -> DatabaseClient:
        """The connection a query on this model would run on now: the router's choice, else the model's
        ``default_connection`` - inside an open transaction on it, the transaction's client.

        Args:
            for_write: Whether the connection is chosen for a write or for a read.

        Returns:
            The chosen connection.

        Raises:
            ConfigurationError: No Hare context is active, the model is swapped out or has no default
                connection, or its tenancy needs a setting of the connection it lacks
                (``MetaInfo.check_tenant_client()``).
            QueryError: The model's tenancy needs a single active tenant or a transaction that set its
                tenants.
        """
        cls._meta.check_not_swapped()
        context = HareContext.require_current()
        router = context.router
        connection = router.db_for_write(cls) if for_write else router.db_for_read(cls)
        meta = cls._meta
        if connection is None:
            # MetaInfo.connection inlined - the context is already at hand, and this is the hottest path of
            # the ORM.
            default_connection = meta.default_connection
            if default_connection is None:
                raise ConfigurationError(MISSING_DEFAULT_CONNECTION_MESSAGE.format(model_name=cls.__name__))
            connection = context.connections.get(default_connection)
        if meta.checks_tenant_client:
            meta.check_tenant_client(connection)
        return connection

    @classmethod
    def get_routed_connection(cls, *, for_write: bool = False) -> DatabaseClient:
        """The connection a query on this model is routed to, its tenancy unchecked - what its alias
        and its features are read off, where no query of the model runs on it (a ``Meta.tenant_schema``
        model outside a tenant scope).

        Args:
            for_write: Whether the connection is chosen for a write or for a read.

        Returns:
            The connection.

        Raises:
            ConfigurationError: No Hare context is active, the model is swapped out or has no default
                connection.
        """
        cls._meta.check_not_swapped()
        context = HareContext.require_current()
        router = context.router
        connection = router.db_for_write(cls) if for_write else router.db_for_read(cls)
        if connection is not None:
            return connection
        return cls._meta.connection

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

    async def save(
        self,
        using: str | DatabaseClient | None = None,
        update_fields: Iterable[str] | None = None,
        *,
        force_create: bool = False,
        force_update: bool = False,
        changed_only: bool = False,
    ) -> None:
        """Inserts the object's row, or updates it when the object has one.

        Args:
            changed_only: Update only the fields changed since the instance was loaded or last
                saved (``get_dirty_fields()``, ``Meta.track_dirty_fields``) - as ``update_fields``
                naming them; nothing changed sends no statement. An unsaved instance is inserted
                whole.
            update_fields: If provided, it should be a tuple/list of fields by name. This is
                the subset of fields that should be updated. On an instance that has a pk it
                always issues an UPDATE by primary key - even for an instance never loaded from
                or saved to the database (``Model(id=known_id, name="x")``) - and raises
                ``IntegrityError`` if no such row exists instead of creating it (use ``create()``
                or ``force_create=True``). It is only ignored when there is no pk yet (a
                DB-generated one), where the row is inserted. An empty collection is a no-op.
            using: The connection - an alias or a client - to write on instead of the model's routed one.
            force_create: Insert the row, with the current pk when the object has one.
            force_update: Update the row - ``IntegrityError`` when there is none.

        Raises:
            FieldError: ``update_fields`` names an unknown field or a relation (name the relation's key
                field, e.g. ``author_id``, instead).
            QueryError: ``update_fields`` is a single string or names the primary key or
                ``Meta.soft_delete_field`` (use ``delete()``/``restore()``); both ``force_create`` and
                ``force_update`` are set; ``changed_only`` is given with ``update_fields`` or
                ``force_create``, or the model has no ``Meta.track_dirty_fields``; the object's
                ``Meta.tenant_field`` value isn't the active tenant scope's, or is unset with no scope.
            IncompleteInstanceError: The object was loaded by ``.only()``/``.defer()`` without a field the
                save writes.
            IntegrityError: The database refuses the row, or an update - ``force_update`` or
                ``update_fields`` - finds no row.
            StaleObjectError: If ``Meta.optimistic_lock_field`` is set and the row was concurrently
                modified since this instance was read.
        """
        update_fields = InstanceSaving.get_checked_update_fields(
            self, update_fields, changed_only, force_create=force_create, force_update=force_update
        )
        if update_fields is not None and not update_fields:
            return
        # A tenant-scoped model is written only into the active scope's tenant; with no scope, the
        # instance's own value is trusted. A partial update not touching the tenant field checks the
        # tenant the instance holds, without filling it - the row's own is checked by the UPDATE.
        tenant_field = self._meta.tenant_field
        if tenant_field and (update_fields is None or tenant_field in update_fields):
            TenantWriteRules.scope_objects(self.__class__, (self,), "save", fills_missing=True, requires_active=False)
        elif tenant_field and self.__dict__.get(tenant_field) is not None:
            TenantWriteRules.scope_objects(self.__class__, (self,), "save", fills_missing=False, requires_active=False)

        if self._await_when_save:
            await InstanceSaving.set_async_default_field(self)
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        if Tenancy.has_tenant_scoped_relations(self.__class__):
            await Tenancy.check_relation_targets(
                self.__class__, [Tenancy.get_relation_row(self)], connection, update_fields
            )
        writer = InstanceWriter(self.__class__, connection)
        if self._partial:
            InstanceSaving.check_partial_instance_saving(self, update_fields)

        # On a database without transactions the row and its report are one step a cancel can't split.
        await WriteSteps.run_whole(
            connection,
            InstanceSaving.write_row(
                self, writer, connection, update_fields, force_create=force_create, force_update=force_update
            ),
        )

    async def delete(self, using: str | DatabaseClient | None = None) -> None:
        """Deletes the object. With ``Meta.soft_delete_field`` the row is marked deleted instead, and
        related rows follow their ``on_delete`` as for a real ``DELETE``.

        Args:
            using: The connection - an alias or a client - to run on instead of the model's routed one.

        Raises:
            QueryError: The tenant scope doesn't match, or the object has never been persisted.
            IncompleteInstanceError: The primary key or ``Meta.optimistic_lock_field`` was not
                loaded.
            ProtectedError: A PROTECT relation guards this row or one ``on_delete=CASCADE`` would
                remove with it.
            IntegrityError: A ``RESTRICT``/``NO_ACTION`` relation still points at the row, or the
                row no longer exists.
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row was modified
                concurrently.
        """
        self._meta.raise_if_no_primary_key(f"{self.__class__.__name__}.delete() of one row")
        TenantWriteRules.check_existing_object(self, "delete")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        # On a database without transactions the cascade, the row and their report are one step a cancel can't split.
        await WriteSteps.get_whole_run(connection, InstanceDeletion.delete_and_report(self, connection))

    async def hard_delete(self, using: str | DatabaseClient | None = None) -> None:
        """Deletes the row for real, even with ``Meta.soft_delete_field`` and even when it is
        soft-deleted already. Related rows follow their ``on_delete``.

        Args:
            using: The connection - an alias or a client - to run on instead of the model's routed one.

        Raises:
            QueryError: The tenant scope doesn't match, or the object has never been persisted.
            IncompleteInstanceError: The primary key or ``Meta.optimistic_lock_field`` was not
                loaded.
            ProtectedError: A PROTECT relation guards this row or one ``on_delete=CASCADE`` would
                remove with it.
            IntegrityError: A ``RESTRICT``/``NO_ACTION`` relation still points at the row, or the
                row no longer exists.
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row was modified
                concurrently.
        """
        TenantWriteRules.check_existing_object(self, "delete")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        InstanceDeletion.check_deletable(self)
        await DeletionCollector.check_protected(type(self), [self.pk], connection)
        await DeletionCollector.check_protected_transitively(type(self), [self.pk], connection)
        await WriteSteps.get_whole_run(connection, InstanceDeletion.hard_delete_and_report(self, connection))

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
        TenantWriteRules.check_existing_object(self, "delete")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        if not self._saved_in_db:
            raise QueryError("Can't preview deleting unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.primary_key_attribute_names):
            raise IncompleteInstanceError(
                PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE.format(
                    model_name=self.__class__.__name__, action="preview deleting"
                )
            )
        return await DeletePreviewBuilder.build(self, connection)

    async def restore(self, using: str | DatabaseClient | None = None, *, cascade: bool = False) -> None:
        """Reverses ``.delete()`` on a soft-deleted instance.

        Args:
            using: The connection - an alias or a client - to run on instead of the model's routed one.
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
        TenantWriteRules.check_existing_object(self, "restore")
        if not self._meta.soft_delete_field:
            raise QueryError(f"{type(self).__name__} has no Meta.soft_delete_field configured")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        if not self._saved_in_db:
            raise QueryError("Can't restore unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.primary_key_attribute_names):
            raise IncompleteInstanceError(
                PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE.format(model_name=self.__class__.__name__, action="restore")
            )
        InstanceSaving.check_optimistic_lock_field_loaded(self, "restore")
        await WriteSteps.get_whole_run(connection, InstanceDeletion.restore_and_report(self, connection, cascade))

    def snapshot(self, fields: Iterable[str] | None = None) -> FieldSnapshot:
        """Captures an immutable copy of the instance's direct field values.

        Args:
            fields: Direct field names to capture. ``None`` captures every loaded direct field.

        Returns:
            A ``FieldSnapshot`` to pass to ``diff_against()`` later.

        Raises:
            FieldError: If a name isn't a direct field of this model.
            IncompleteInstanceError: If a named field wasn't loaded by ``.only()``/``.defer()``.
        """
        if fields is None:
            return FieldSnapshot(type(self), DirtyFields.capture_field_values(self, self._meta.direct_fields))
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
        return FieldSnapshot(type(self), DirtyFields.capture_field_values(self, field_names))

    def diff_against(self, snapshot: FieldSnapshot) -> dict[str, tuple[Any, Any]]:
        """Compares the instance's current values with a snapshot's.

        Args:
            snapshot: A ``FieldSnapshot`` taken from an instance of this model.

        Returns:
            ``{field: (snapshot_value, current_value)}`` for every snapshotted field that changed -
            ``sensitive=True`` fields included with their real values, so mask them before logging
            the result.

        Raises:
            TypeError: If ``snapshot`` is a snapshot of another model - its fields aren't this one's.
        """
        if not isinstance(self, snapshot.model_class):
            raise TypeError(
                f"Can't diff a {type(self).__name__} instance against a snapshot of {snapshot.model_class.__name__}"
            )
        return DirtyFields.diff_field_values(self, snapshot, snapshot)

    def get_dirty_fields(self) -> dict[str, tuple[Any, Any]]:
        """
        Fields whose current value differs from the value this instance was last hydrated from
        the DB with (or from nonexistence, for an instance that's never been saved - every field
        is dirty relative to not existing at all, one without a value as ``(None, None)``).

        Raises:
            QueryError: If the model has no ``Meta.track_dirty_fields = True``.
        """
        if not self._meta.track_dirty_fields:
            raise QueryError(f"{type(self).__name__} has no Meta.track_dirty_fields configured")
        direct_fields = self._meta.direct_fields
        dirty_snapshot: dict[str, Any] | None = getattr(self, "_dirty_snapshot", None)
        if dirty_snapshot is None:
            return {field_name: (None, getattr(self, field_name, None)) for field_name in direct_fields}
        return DirtyFields.diff_field_values(self, dirty_snapshot, direct_fields)

    async def refresh_from_db(
        self,
        fields: Iterable[str] | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None:
        """Reads the object's fields again from its row - every field, or the ones named.

        Example: ::

            await user.refresh_from_db(fields=["name"])

        Args:
            fields: The fields to read - an empty collection reads none.
            using: The connection - an alias or a client - to read on instead of the model's routed one.

        Raises:
            FieldError: If ``fields`` names an unknown field - a forward relation refreshes its
                key column(s), one key column of a relation keyed by several every one of them.
            IncompleteInstanceError: If a ``.only(...)``/``.defer(...)`` query left the
                primary key itself unloaded.
            QueryError: If object has never been persisted.
            QueryError: If ``fields`` is a single string instead of a collection.
        """
        self._meta.raise_if_no_primary_key(f"{self.__class__.__name__}.refresh_from_db()")
        if fields is not None:
            fields = InstanceSaving.get_validated_field_names(type(self), fields, "fields")
            if not fields:
                return
            fields = InstanceRefresh.get_refreshed_field_names(type(self), list(fields))
        if not self._saved_in_db:
            raise QueryError("Can't refresh unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.primary_key_attribute_names):
            raise IncompleteInstanceError(
                PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE.format(model_name=self.__class__.__name__, action="refresh")
            )
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self)
        refreshed_obj = await InstanceRefresh.get_queryset(self, connection, fields).get(pk=self.pk)
        InstanceRefresh.apply(self, refreshed_obj, fields)

    @classmethod
    def get_table(cls) -> Table:
        """The model's table in the SQL builder, qualified by ``Meta.schema``."""
        return Table(name=cls._meta.db_table, schema=cls._meta.schema)
