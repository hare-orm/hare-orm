from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Generator, TypeVar, cast

from hare.core.connections.connections import Connections
from hare.core.hare_context import HareContext
from hare.exceptions import (
    ConfigurationError,
    FieldError,
    IncompleteInstanceError,
    NoValuesFetched,
    QueryError,
)
from hare.fields.field import Field
from hare.fields.relations.fields.relational_field import RelationalField
from hare.fields.relations.relation_values import RelationValues
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.class_building.generic_foreign_keys import GenericForeignKeys
from hare.models.constants import EMPTY, EMPTY_PENDING_DEFAULTS
from hare.models.deletion.cascade.cascade_restore import CascadeRestore
from hare.models.deletion.cascade.deletion_collector import DeletionCollector
from hare.models.deletion.cascade.related_rows import RelatedRows
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
from hare.models.write.write_steps import WriteSteps
from hare.query.managers.base_manager import BaseManager
from hare.query.queryset.relations.related_queryset.relation_rows import RelationRows
from hare.sql import Table

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterable

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.deletion.preview.delete_preview import DeletePreview

TModel = TypeVar("TModel", bound="Model")


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
        # self._meta is a very common attribute lookup, lets cache it.
        meta = self._meta
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
        for key in meta.fields.difference(
            InstanceInitialization.set_kwargs(self, kwargs, assigns_directly=sets_columns_directly)
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
        # set field value override async default function
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

    def _get_pk_value(self) -> Any:
        # primary_key_attribute is the field name of a single-column key, a tuple of names for a composite one
        # and () without a key - read straight off it, every `.pk` access goes through here.
        primary_key_attribute = self._meta.primary_key_attribute
        if type(primary_key_attribute) is str:
            return getattr(self, primary_key_attribute, None)
        if not primary_key_attribute:
            return None
        return tuple(getattr(self, name, None) for name in primary_key_attribute)

    def _set_pk_value(self, value: Any) -> None:
        self._meta.raise_if_no_primary_key("setting pk")
        if self._meta.has_composite_primary_key:
            if not isinstance(value, tuple) or len(value) != len(self._meta.primary_key_attribute):
                raise QueryError(
                    f"Composite pk must be set to a {len(self._meta.primary_key_attribute)}-tuple matching "
                    f"{self._meta.primary_key_attribute}, got {value!r}"
                )
            for name, part in zip(self._meta.primary_key_attribute, value, strict=True):
                setattr(self, name, part)
            return
        setattr(self, cast("str", self._meta.primary_key_attribute), value)

    pk = property(_get_pk_value, _set_pk_value)
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
            self, meta.fields.difference(kwargs, meta.fetch_fields, populated_source_fields)
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
            ConfigurationError: No Hare context is active, or the model's tenancy needs a setting of
                the connection it lacks (``MetaInfo.check_tenant_client()``).
            QueryError: The model is swapped out or has no default connection, or its tenancy needs a
                single active tenant or a transaction that set its tenants.
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
                raise ConfigurationError(f"default_connection for the model {meta._model} cannot be None")
            connection = context.connections.get(default_connection)
        if meta.checks_tenant_client:
            meta.check_tenant_client(connection)
        return connection

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
        """
        Creates/Updates the current model object.

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
            QueryError: If update_fields include pk field; ``changed_only`` isn't a bool, is given with
                ``update_fields`` or ``force_create``, or the model has no ``Meta.track_dirty_fields``.
            StaleObjectError: If ``Meta.optimistic_lock_field`` is set and the row was concurrently
                modified since this instance was read.
        """
        update_fields = InstanceSaving.get_checked_update_fields(
            self, update_fields, changed_only, force_create=force_create, force_update=force_update
        )
        if update_fields is not None and not update_fields:
            return
        # A tenant-scoped model is written only into the active scope's tenant; with no scope, the
        # instance's own value is trusted. A partial update not touching the tenant field has
        # nothing to check.
        tenant_field = self._meta.tenant_field
        if tenant_field and (update_fields is None or tenant_field in update_fields):
            WriteSteps.scope_to_active_tenant(
                self.__class__, (self,), "save", fills_missing=True, requires_active=False
            )

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
        pk_is_unset = pre_write_pk is None
        # A pk the database generates has no default to fill in.
        if not force_update and pk_is_unset and self._meta.generated_pk_field_name is None:
            await InstanceSaving.set_unset_pk_default(self)

        # An INSERT unless an UPDATE is forced or the instance has a row to update: loaded from or
        # saved to the database, or given update_fields, and with a primary key.
        if force_create:
            await InstanceSaving.execute_forced_insert(self, writer)
            was_insert = True
        elif not force_update and (pk_is_unset or not (self._saved_in_db or update_fields)):
            await writer.execute_insert(self)
            was_insert = True
        else:
            optimistic_lock_field = self._meta.optimistic_lock_field
            old_version = getattr(self, optimistic_lock_field) if optimistic_lock_field else None
            rows = await writer.execute_update(self, update_fields)
            if not rows:
                await InstanceSaving.check_update_matched(
                    self,
                    connection,
                    rows,
                    old_version,
                    never_inserts=not force_update and not pre_write_saved_in_db,
                )
            if update_fields is not None:
                partial_update_fields = list(update_fields)

        await InstanceSaving.record_written(
            self,
            connection,
            was_insert,
            partial_update_fields,
            pk_before=pre_write_pk,
            saved_in_db_before=pre_write_saved_in_db,
            custom_generated_pk_before=pre_write_custom_generated_pk,
            dirty_snapshot_before=pre_write_dirty_snapshot,
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
        InstanceDeletion.check_active_tenant_scope_for_write(self, "delete")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        with ChangeEvents.reporting_as_a_whole():
            await InstanceDeletion.delete_with_cascade(self, connection, apply_active_tenant_scope_guard=True)
        if ChangeEvents.is_observed():
            await ChangeEvents.report_deletion(
                connection, self.__class__, pks=[self.pk], soft=self._meta.soft_delete_field is not None
            )

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
        InstanceDeletion.check_active_tenant_scope_for_write(self, "delete")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        InstanceDeletion.check_deletable(self)
        await DeletionCollector.check_protected(type(self), [self.pk], connection)
        await DeletionCollector.check_protected_transitively(type(self), [self.pk], connection)
        with ChangeEvents.reporting_as_a_whole():
            await InstanceDeletion.delete_row_permanently(self, connection, apply_active_tenant_scope_guard=True)
        if ChangeEvents.is_observed():
            await ChangeEvents.report_deletion(connection, self.__class__, pks=[self.pk])

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
        InstanceDeletion.check_active_tenant_scope_for_write(self, "delete")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        if not self._saved_in_db:
            raise QueryError("Can't preview deleting unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.primary_key_attribute_names):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model without primary key fetched. Can't preview deleting it."
            )
        return await DeletePreviewBuilder.build(self, connection)

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
        InstanceDeletion.check_active_tenant_scope_for_write(self, "restore")
        if not self._meta.soft_delete_field:
            raise QueryError(f"{type(self).__name__} has no Meta.soft_delete_field configured")
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self, True)
        if not self._saved_in_db:
            raise QueryError("Can't restore unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.primary_key_attribute_names):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model without primary key fetched. Can't restore it."
            )
        InstanceSaving.check_optimistic_lock_field_loaded(self, "restore")
        if not cascade:
            await SoftDeletion.write_soft_delete_field(self, connection, None, "restore")
        else:
            async with connection._in_transaction() as transaction_connection:
                soft_delete_values = await RelatedRows.lock_soft_delete_values(
                    type(self), [self.pk], transaction_connection
                )
                await SoftDeletion.write_soft_delete_field(self, transaction_connection, None, "restore")
                if (deleted_at := soft_delete_values.get(self.pk)) is not None:
                    await CascadeRestore(transaction_connection, deleted_at).run(type(self), [self.pk])
        await WriteSteps.report(
            connection, self.__class__, RowOperation.UPDATE, instances=[self], fields=[self._meta.soft_delete_field]
        )

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
            return FieldSnapshot(type(self), DirtyFields.capture_field_values(self, self._meta.direct_fields))
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
            TypeError: If ``snapshot`` isn't a ``FieldSnapshot`` of this model.
        """
        if not isinstance(snapshot, FieldSnapshot):
            raise TypeError(f"diff_against() expects a FieldSnapshot, got {type(snapshot).__name__}")
        if not isinstance(self, snapshot.model_class):
            raise TypeError(
                f"Can't diff a {type(self).__name__} instance against a snapshot of {snapshot.model_class.__name__}"
            )
        return DirtyFields.diff_field_values(self, snapshot, snapshot)

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
        return DirtyFields.diff_field_values(self, dirty_snapshot, direct_fields)

    async def refresh_from_db(
        self,
        fields: Iterable[str] | None = None,
        using: str | DatabaseClient | None = None,
    ) -> None:
        """
        Refresh latest data from connection. When this method is called without arguments
        all connection fields of the model are updated to the values currently present in the database.

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
            fields = InstanceSaving.get_validated_field_names(type(self), fields, "fields")
            if not fields:
                return
        if not self._saved_in_db:
            raise QueryError("Can't refresh unpersisted record")
        if not all(hasattr(self, pk_name) for pk_name in self._meta.primary_key_attribute_names):
            raise IncompleteInstanceError(
                f"{self.__class__.__name__} is a partial model without primary key fetched. Can't refresh it."
            )
        connection = Connections.get_client(using) or InstanceConnections.get_connection_for_instance(self)
        refreshed_obj = await InstanceRefresh.get_queryset(self, connection, fields).get(pk=self.pk)
        InstanceRefresh.apply(self, refreshed_obj, fields)

    @classmethod
    def get_table(cls) -> Table:
        """Return a hare.sql table for this model."""
        return Table(name=cls._meta.db_table, schema=cls._meta.schema)
