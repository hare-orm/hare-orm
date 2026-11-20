from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NoReturn

from hare.exceptions import IncompleteInstanceError, IntegrityError, QueryError, StaleObjectError
from hare.models.constants import PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.declarations import PendingRelationDefault
from hare.models.write.instance_update_guard import InstanceUpdateGuard
from hare.models.write.instance_update_guard_mismatch import InstanceUpdateGuardMismatch
from hare.models.write.rules.constants import SAVE_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.write_finalizer import WriteFinalizer
from hare.query.queryset.arguments.collection_arguments import CollectionArguments
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model
    from hare.models.write.declarations import WriteOutcome
    from hare.models.write.instance_values import InstanceValues
    from hare.models.write.instance_writer import InstanceWriter


class InstanceSaving:
    """The steps of save(): the primary key default and the async defaults set, a forced insert run,
    the rows an update targets counted, the optimistic lock checked, the fields an update writes
    worked out and validated."""

    @staticmethod
    async def set_unset_pk_default(obj: Model) -> None:
        """Assigns the primary key field's ``default``/``db_default`` when the pk is ``None`` and
        the database doesn't generate it.

        Args:
            obj: The model obj.
        """
        meta = obj._binding
        if not isinstance(meta.primary_key_attribute, str) or meta.generated_pk_field_name is not None:
            return
        if getattr(obj, meta.primary_key_attribute, None) is not None:
            return
        pk_field = meta.pk
        default = pk_field.default
        if pk_field._default_is_coroutine:
            setattr(obj, meta.primary_key_attribute, pk_field.get_default_value_on_assign(await default()))
        elif callable(default):
            setattr(obj, meta.primary_key_attribute, pk_field.get_default_value_on_assign(default()))
        elif default is not None:
            setattr(obj, meta.primary_key_attribute, pk_field.get_static_default_value())
        elif pk_field.has_db_default():
            setattr(obj, meta.primary_key_attribute, pk_field.get_db_default_value())

    @staticmethod
    async def set_async_default_field(obj: Model) -> None:
        """Awaits the coroutine defaults still pending on the object and assigns their values.

        Args:
            obj: The model obj.
        """
        # Initialization also imports the deletion paths, which use this executor.
        from hare.models.instances.instance_initialization import InstanceInitialization

        pending_defaults = obj.__dict__.pop("_await_when_save", None)
        if pending_defaults:
            completed_relations = set()
            for field_name, awaitable_default in pending_defaults.items():
                if isinstance(awaitable_default, PendingRelationDefault):
                    if id(awaitable_default) in completed_relations:
                        continue
                    completed_relations.add(id(awaitable_default))
                    value = await awaitable_default.factory()
                    fields = tuple(name for name, pending in pending_defaults.items() if pending is awaitable_default)
                    InstanceInitialization.assign_relation_default_value(
                        obj, awaitable_default.relation_name, value, fields
                    )
                else:
                    value = await awaitable_default()
                    field = obj._binding.fields_map[field_name]
                    setattr(obj, field_name, field.get_default_value_on_assign(value))

    @staticmethod
    async def execute_forced_insert(obj: Model, writer: InstanceWriter) -> WriteOutcome:
        """INSERTs this obj for ``save(force_create=True)``, sending an already-set
        DB-generated pk as it is instead of letting the database generate a new one.

        Args:
            obj: The model obj.
            writer: The writer of this obj's model on the target connection.
        """
        custom_generated_pk = obj._custom_generated_pk
        if obj._binding.generated_pk_field_name is not None and obj.pk is not None:
            object.__setattr__(obj, "_custom_generated_pk", True)
        try:
            return await writer.execute_insert(obj)
        finally:
            object.__setattr__(obj, "_custom_generated_pk", custom_generated_pk)

    @staticmethod
    async def count_update_target_rows(obj: Model, connection: DatabaseClient) -> int:
        """Counts the rows an UPDATE by this obj's pk would match, for a save() with no
        column left to write.

        Args:
            obj: The model obj.
            connection: The connection the UPDATE would run on.

        Returns:
            ``1`` if the row exists (in the active tenant, when one is active), else ``0``.
        """
        queryset = RowScopes.get_base_queryset(
            obj._binding,
            RowVisibility(include_deleted=True, all_tenants=Tenancy.get_scope(obj._binding) is None),
        )
        return int(await queryset.filter(pk=obj.pk).using(connection).exists())

    @staticmethod
    def check_optimistic_lock_field_loaded(obj: Model, operation: str) -> None:
        """Raises if ``Meta.optimistic_lock_field`` is set but a ``.only()``/``.defer()`` query left it
        unloaded - every write reads and bumps it, whichever other fields it touches.

        Args:
            obj: The model obj.
            operation: Names the calling method ("save"/"delete"/"restore") in the raised message.

        Raises:
            IncompleteInstanceError: ``Meta.optimistic_lock_field`` is set and not loaded on this obj.
        """
        optimistic_lock_field = obj._binding.optimistic_lock_field
        if optimistic_lock_field and not hasattr(obj, optimistic_lock_field):
            raise IncompleteInstanceError(
                f"{obj.__class__.__name__} is a partial model, Meta.optimistic_lock_field "
                f"{optimistic_lock_field!r} is not available - every {operation}() needs to read and "
                "bump it, whichever other fields it touches"
            )

    @staticmethod
    def check_partial_instance_saving(obj: Model, update_fields: Iterable[str] | None) -> None:
        """Raises when a partial obj - one a ``.only()``/``.defer()`` query loaded - can't be saved.

        Args:
            obj: The model obj.
            update_fields: The fields the save writes.

        Raises:
            IncompleteInstanceError: No fields are named, or the primary key, a named field or
                ``Meta.optimistic_lock_field`` wasn't loaded.
        """
        model_name = obj.__class__.__name__
        if not update_fields:
            raise IncompleteInstanceError(
                f"{model_name} was loaded by .only()/.defer() - save it with update_fields naming the loaded "
                "fields to write"
            )
        if not all(hasattr(obj, pk_name) for pk_name in obj._binding.primary_key_attribute_names):
            raise IncompleteInstanceError(
                PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE.format(model_name=model_name, action="save")
            )
        for field in update_fields:
            if not hasattr(obj, field):
                raise IncompleteInstanceError(
                    f"{model_name} was loaded by .only()/.defer() without {field!r} - it can't be saved"
                )
        # The optimistic lock field is bumped on every save(), whatever update_fields names - it has
        # to be loaded.
        InstanceSaving.check_optimistic_lock_field_loaded(obj, "save")

    @staticmethod
    async def check_update_matched(
        obj: Model, connection: DatabaseClient, rows: int | None, old_version: Any, *, never_inserts: bool
    ) -> None:
        """Raises when the UPDATE of a save() matched no row.

        Args:
            obj: The model obj.
            connection: The connection the UPDATE ran on.
            rows: The rows the UPDATE reported, None when it had no column left to write.
            old_version: The version before the UPDATE.
            never_inserts: Whether the error explains that this save() never creates the row.

        Raises:
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row exists - it was
                modified concurrently.
            IntegrityError: The row doesn't exist.
        """
        if rows is None:
            rows = await InstanceSaving.count_update_target_rows(obj, connection)
        if rows != 0:
            return
        guard = InstanceUpdateGuard.get_for(obj, connection)
        if guard is not None and not await guard.matching_rows.filter(pk=obj.pk).exists():
            raise InstanceUpdateGuardMismatch(f"The row with PK {obj.pk} no longer matches the update")
        message = f"Can't update object that doesn't exist. PK: {obj.pk}"
        if never_inserts:
            message += (
                " - save(update_fields=...) on an instance that wasn't loaded from or saved to "
                "the database always issues an UPDATE by primary key and never creates the row; "
                "use create() or save(force_create=True) to insert it"
            )
        await InstanceSaving.raise_for_unmatched_update(obj, connection, old_version, message)

    @staticmethod
    async def write_row(
        obj: Model,
        writer: InstanceWriter,
        update_fields: Iterable[str] | None,
        *,
        force_create: bool,
        force_update: bool,
        prepared_values: InstanceValues | None = None,
    ) -> None:
        """Writes the row of a save() - an INSERT or an UPDATE - and brings the obj in step with it.

        Args:
            obj: The model obj.
            writer: The model's writer and its selected connection.
            update_fields: The fields a partial UPDATE writes, None for a whole write.
            force_create: Whether an INSERT is forced.
            force_update: Whether an UPDATE is forced.
            prepared_values: Default and tenant assignments to undo if writing fails.
        """
        # Only set when an actual partial UPDATE (update_fields is not None) is the operation
        # that ran - used below to snapshot just the fields that were actually written, instead
        # of laundering every OTHER unsaved in-memory change on this instance as clean too.
        partial_update_fields: list[str] | None = None
        connection = writer.connection

        try:
            before = WriteFinalizer.remember_persistence(obj, connection)
            pre_write_saved_in_db = obj._saved_in_db
            # Decided before a default fills the key: an unset key means an INSERT.
            pk_is_unset = obj.pk is None
            # A pk the database generates has no default to fill in.
            if not force_update and pk_is_unset and obj._binding.generated_pk_field_name is None:
                await InstanceSaving.set_unset_pk_default(obj)

            # An INSERT unless an UPDATE is forced or the instance has a row to update: loaded from or
            # saved to the database, or given update_fields, and with a primary key.
            if force_create:
                outcome = await InstanceSaving.execute_forced_insert(obj, writer)
            elif not force_update and (pk_is_unset or not (obj._saved_in_db or update_fields)):
                outcome = await writer.execute_insert(obj)
            else:
                optimistic_lock_field = obj._binding.optimistic_lock_field
                old_version = getattr(obj, optimistic_lock_field) if optimistic_lock_field else None
                outcome = await writer.execute_update(obj, update_fields)
                # A database reporting no count of the rows a write changed tells no missing row.
                if not outcome.may_have_written:
                    await InstanceSaving.check_update_matched(
                        obj,
                        connection,
                        outcome.row_count,
                        old_version,
                        never_inserts=not force_update and not pre_write_saved_in_db,
                    )
                if update_fields is not None:
                    partial_update_fields = list(update_fields)
        except BaseException as error:
            if prepared_values is not None:
                WriteFinalizer.restore_failed_values(prepared_values, error)
            raise

        # Reporting happens after the write succeeded. A listener failure must not undo
        # preparation of a row that remains stored outside a transaction.
        await WriteFinalizer.saved(
            before,
            connection,
            outcome,
            updated_fields=None
            if partial_update_fields is None or not obj._binding.track_dirty_fields
            else writer.get_update_rules(partial_update_fields, WriteType.UPDATE).get_updated_names(),
            reported_fields=partial_update_fields,
        )

    @staticmethod
    async def raise_for_unmatched_update(
        obj: Model, connection: DatabaseClient, old_version: Any, message: str
    ) -> NoReturn:
        """Raises for a write of this obj's row that matched no row, after putting the bumped
        in-memory version back.

        Args:
            obj: The model obj.
            connection: The connection the write ran on.
            old_version: The version before the write.
            message: The ``IntegrityError`` message.

        Raises:
            StaleObjectError: ``Meta.optimistic_lock_field`` is set and the row exists - it was
                modified concurrently.
            IntegrityError: The row doesn't exist.
        """
        optimistic_lock_field = obj._binding.optimistic_lock_field
        if optimistic_lock_field:
            setattr(obj, optimistic_lock_field, old_version)
        # Asked only when the write failed: a version mismatch and a missing row both match nothing.
        if optimistic_lock_field and await InstanceSaving.count_update_target_rows(obj, connection):
            raise StaleObjectError(
                f"{obj.__class__.__name__} (pk={obj.pk}) was modified concurrently - expected version {old_version}",
                obj.__class__,
                obj.pk,
                old_version,
            )
        raise IntegrityError(message)

    @staticmethod
    def get_checked_update_fields(
        obj: Model,
        update_fields: Iterable[str] | None,
        changed_only: Any,
        *,
        force_create: bool,
        force_update: bool,
    ) -> tuple[str, ...] | None:
        """The fields a save() updates, its arguments checked.

        Args:
            obj: The model obj.
            update_fields: The ``update_fields`` argument.
            changed_only: The ``changed_only`` argument.
            force_create: The ``force_create`` argument.
            force_update: The ``force_update`` argument.

        Returns:
            The validated field names - none when there is nothing to write - or None for a save
            of the whole obj.

        Raises:
            QueryError: The arguments contradict each other, ``update_fields`` names
                ``Meta.soft_delete_field``, or an UPDATE is asked of a model without a primary key.
            FieldError: ``update_fields`` names an unknown field or a relation without a column.
        """
        if force_create and force_update:
            raise QueryError("save() can't force both an insert (force_create) and an update (force_update)")
        if changed_only is not False:
            update_fields = InstanceSaving.get_changed_update_fields(
                obj, changed_only, update_fields, force_create, force_update
            )
            if update_fields is not None and not update_fields:
                return ()
        meta = obj._binding
        if (obj._saved_in_db or update_fields is not None or force_update) and not force_create:
            meta.raise_if_no_primary_key(f"{obj.__class__.__name__}.save() of a saved row (an UPDATE)")
        if update_fields is None:
            return None
        names = CollectionArguments.get_list(update_fields, "'update_fields'")
        # The version is the one the row is matched by - bumped as on every save().
        version_field = meta.optimistic_lock_field
        rules = RowWriteRules.get(
            obj._binding,
            WriteType.UPDATE,
            names if version_field is None else [name for name in names if name != version_field],
            SAVE_METHOD_NAME,
        )
        return rules.named_field_names

    @staticmethod
    def get_changed_update_fields(
        obj: Model,
        changed_only: Any,
        update_fields: Iterable[str] | None,
        force_create: bool,
        force_update: bool,
    ) -> list[str] | None:
        """The ``update_fields`` of ``save(changed_only=True)`` - the changed fields of a saved obj.

        Args:
            obj: The model obj.
            changed_only: The argument.
            update_fields: The ``update_fields`` given with it.
            force_create: The ``force_create`` given with it.
            force_update: The ``force_update`` given with it.

        Returns:
            The changed fields, None for an obj inserted whole.

        Raises:
            QueryError: ``changed_only`` is given with ``update_fields`` or ``force_create``, the
                model has no ``Meta.track_dirty_fields``, or a primary key
                field changed - the row is found by it.
        """
        if not changed_only:
            return None if update_fields is None else list(update_fields)
        if update_fields is not None or force_create:
            raise QueryError("save(changed_only=True) takes neither update_fields nor force_create")
        if not obj._binding.track_dirty_fields:
            raise QueryError(
                f"save(changed_only=True) needs {type(obj).__name__}.Meta.track_dirty_fields - the "
                "changes are read from it"
            )
        if not (obj._saved_in_db or force_update):
            return None
        # The primary key names the updated row; a generated field the database computes.
        meta = obj._binding
        dirty_field_names = obj.get_dirty_fields()
        changed_key_names = [name for name in meta.primary_key_attribute_names if name in dirty_field_names]
        # An unsaved obj (force_update) names its row by the key it was given.
        if obj._saved_in_db and changed_key_names:
            raise QueryError(
                f"save(changed_only=True) of {type(obj).__name__} can't save a changed primary key field "
                f"({', '.join(changed_key_names)}) - the row is found by it. Update it through "
                "Model.objects.filter(pk=...).update(...)"
            )
        return [field_name for field_name in dirty_field_names if not meta.fields_map[field_name].generated]
