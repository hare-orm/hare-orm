from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NoReturn, cast

from hare.exceptions import FieldError, IncompleteInstanceError, IntegrityError, QueryError, StaleObjectError
from hare.fields.relations.fields.relational_field import RelationalField
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.constants import PARTIAL_INSTANCE_WITHOUT_KEY_MESSAGE
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.rules.constants import SAVE_METHOD_NAME
from hare.models.write.rules.enums import WriteType
from hare.models.write.rules.row_write_rules import RowWriteRules
from hare.models.write.rules.written_instances import WrittenInstances
from hare.models.write.write_steps import WriteSteps
from hare.query.queryset.arguments.collection_arguments import CollectionArguments
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.models.model import Model


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
        meta = obj._meta
        if not isinstance(meta.primary_key_attribute, str) or meta.generated_pk_field_name is not None:
            return
        if getattr(obj, meta.primary_key_attribute, None) is not None:
            return
        pk_field = meta.pk
        default = pk_field.default
        if pk_field._default_is_coroutine:
            setattr(obj, meta.primary_key_attribute, await default())
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
        pending_defaults = obj.__dict__.pop("_await_when_save", None)
        if pending_defaults:
            for field_name, awaitable_default in pending_defaults.items():
                setattr(obj, field_name, await awaitable_default())

    @staticmethod
    async def execute_forced_insert(obj: Model, writer: InstanceWriter) -> None:
        """INSERTs this obj for ``save(force_create=True)``, sending an already-set
        DB-generated pk as it is instead of letting the database generate a new one.

        Args:
            obj: The model obj.
            writer: The writer of this obj's model on the target connection.
        """
        custom_generated_pk = obj._custom_generated_pk
        if obj._meta.generated_pk_field_name is not None and obj.pk is not None:
            object.__setattr__(obj, "_custom_generated_pk", True)
        try:
            await writer.execute_insert(obj)
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
            obj.__class__,
            RowVisibility(include_deleted=True, all_tenants=Tenancy.get_scope(obj.__class__) is None),
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
        optimistic_lock_field = obj._meta.optimistic_lock_field
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
        if not all(hasattr(obj, pk_name) for pk_name in obj._meta.primary_key_attribute_names):
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
        connection: DatabaseClient,
        update_fields: Iterable[str] | None,
        *,
        force_create: bool,
        force_update: bool,
    ) -> None:
        """Writes the row of a save() - an INSERT or an UPDATE - and brings the obj in step with it.

        Args:
            obj: The model obj.
            writer: The model's writer on ``connection``.
            connection: The connection the row is written on.
            update_fields: The fields a partial UPDATE writes, None for a whole write.
            force_create: Whether an INSERT is forced.
            force_update: Whether an UPDATE is forced.
        """
        # Only set when an actual partial UPDATE (update_fields is not None) is the operation
        # that ran - used below to snapshot just the fields that were actually written, instead
        # of laundering every OTHER unsaved in-memory change on this instance as clean too.
        partial_update_fields: list[str] | None = None

        # Captured before the write: a rollback reverts the row, not _saved_in_db, the pk or the
        # dirty baseline of the instance.
        was_insert = False
        pre_write_pk = obj.pk
        pre_write_saved_in_db = obj._saved_in_db
        pre_write_custom_generated_pk = obj._custom_generated_pk
        pre_write_dirty_snapshot = (
            dict(obj._dirty_snapshot) if obj._meta.track_dirty_fields and obj._dirty_snapshot is not None else None
        )

        # Decided before a pk default below fills the pk in - an unset pk means an INSERT.
        pk_is_unset = pre_write_pk is None
        # A pk the database generates has no default to fill in.
        if not force_update and pk_is_unset and obj._meta.generated_pk_field_name is None:
            await InstanceSaving.set_unset_pk_default(obj)

        # An INSERT unless an UPDATE is forced or the instance has a row to update: loaded from or
        # saved to the database, or given update_fields, and with a primary key.
        if force_create:
            await InstanceSaving.execute_forced_insert(obj, writer)
            was_insert = True
        elif not force_update and (pk_is_unset or not (obj._saved_in_db or update_fields)):
            await writer.execute_insert(obj)
            was_insert = True
        else:
            optimistic_lock_field = obj._meta.optimistic_lock_field
            old_version = getattr(obj, optimistic_lock_field) if optimistic_lock_field else None
            rows = await writer.execute_update(obj, update_fields)
            # A database reporting no count of the rows a write changed tells no missing row.
            if rows == 0 or (rows is None and connection.features.reports_written_row_count):
                await InstanceSaving.check_update_matched(
                    obj,
                    connection,
                    rows,
                    old_version,
                    never_inserts=not force_update and not pre_write_saved_in_db,
                )
            if update_fields is not None:
                partial_update_fields = list(update_fields)

        await InstanceSaving.record_written(
            obj,
            connection,
            was_insert,
            partial_update_fields,
            None
            if partial_update_fields is None
            else writer.get_update_rules(partial_update_fields, WriteType.UPDATE).get_updated_names(),
            pk_before=pre_write_pk,
            saved_in_db_before=pre_write_saved_in_db,
            custom_generated_pk_before=pre_write_custom_generated_pk,
            dirty_snapshot_before=pre_write_dirty_snapshot,
        )

    @staticmethod
    async def record_written(
        obj: Model,
        connection: DatabaseClient,
        was_insert: bool,
        partial_update_fields: list[str] | None,
        updated_field_names: list[str] | None,
        *,
        pk_before: Any,
        saved_in_db_before: bool,
        custom_generated_pk_before: bool,
        dirty_snapshot_before: dict[str, Any] | None,
    ) -> None:
        """Brings an obj in step with the row a save() just wrote and reports the write. A rollback
        reverts the row, so what the obj held before is registered to be put back with it.

        Args:
            obj: The model obj.
            connection: The connection the row was written on.
            was_insert: Whether the row was inserted.
            partial_update_fields: The fields a partial UPDATE was asked to write, None for a whole write.
            updated_field_names: Every field the partial UPDATE wrote - the stamps, blind indexes and
                version too; None for a whole write.
            pk_before: The primary key before the write.
            saved_in_db_before: Whether the obj counted as saved before the write.
            custom_generated_pk_before: Whether the obj carried a caller's value for a generated
                primary key before the write.
            dirty_snapshot_before: The dirty baseline before the write.
        """
        meta = obj._meta
        # Outside a transaction a write can't be rolled back - nothing to register.
        if connection.is_transaction_client:
            if was_insert:
                RollbackRestores.register_rollback_restore(obj, connection, "_saved_in_db", saved_in_db_before)
                if meta.has_primary_key:
                    RollbackRestores.register_rollback_restore(obj, connection, "pk", pk_before)
            if meta.track_dirty_fields:
                RollbackRestores.register_rollback_restore(obj, connection, "_dirty_snapshot", dirty_snapshot_before)

        WrittenInstances.mark_saved((obj,), connection)
        if was_insert and not custom_generated_pk_before:
            # The pk the database just generated was assigned through Model.__setattr__ while the
            # obj still counted as unsaved - it isn't a caller-supplied value.
            object.__setattr__(obj, "_custom_generated_pk", False)
        if meta.track_dirty_fields:
            # After a partial UPDATE only the written fields are clean - the other unsaved changes
            # of the obj stay dirty.
            if updated_field_names is not None:
                DirtyFields.sync_dirty_snapshot_fields(obj, updated_field_names)
            else:
                DirtyFields.snapshot_dirty_fields(obj)
        # Asked first - the report is a coroutine, made only for a listener.
        if ChangeEvents.is_observed(obj.__class__):
            await WriteSteps.report(
                connection,
                obj.__class__,
                RowOperation.INSERT if was_insert else RowOperation.UPDATE,
                instances=[obj],
                fields=None if was_insert else partial_update_fields,
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
        optimistic_lock_field = obj._meta.optimistic_lock_field
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
        meta = obj._meta
        if (obj._saved_in_db or update_fields is not None or force_update) and not force_create:
            meta.raise_if_no_primary_key(f"{obj.__class__.__name__}.save() of a saved row (an UPDATE)")
        if update_fields is None:
            return None
        names = CollectionArguments.get_list(update_fields, "'update_fields'")
        # The version is the one the row is matched by - bumped as on every save().
        version_field = meta.optimistic_lock_field
        rules = RowWriteRules.get(
            type(obj), WriteType.UPDATE, [name for name in names if name != version_field], SAVE_METHOD_NAME
        )
        return cast("tuple[str, ...]", rules.named_field_names)

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
        if not obj._meta.track_dirty_fields:
            raise QueryError(
                f"save(changed_only=True) needs {type(obj).__name__}.Meta.track_dirty_fields - the "
                "changes are read from it"
            )
        if not (obj._saved_in_db or force_update):
            return None
        # The primary key names the updated row; a generated field the database computes.
        meta = obj._meta
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

    @staticmethod
    def get_validated_field_names(
        model: type[Model], field_names: Iterable[str], argument_name: str
    ) -> tuple[str, ...]:
        """Materializes a caller-supplied collection of field names once and validates every name.

        Args:
            model: The model.
            field_names: Model field names, any iterable (a generator is consumed here, once).
            argument_name: The caller's parameter name, used in raised messages.

        Returns:
            The names without duplicates, in the given order, ``pk`` expanded to the
            primary key attribute(s) and a forward relation to its key column(s).

        Raises:
            FieldError: If a name is not a column-backed field or a forward relation of this model.
        """
        meta = model._meta
        field_names = CollectionArguments.get_list(field_names, f"'{argument_name}'")
        validated_names: dict[str, None] = {}
        for field_name in field_names:
            if field_name == "pk":
                validated_names.update(dict.fromkeys(meta.primary_key_attribute_names))
            elif field_name in meta.fields_db_projection:
                validated_names[field_name] = None
            elif field_name in meta.foreign_key_fields or field_name in meta.one_to_one_fields:
                # A forward relation stands for its key column(s), like Django.
                source_fields = cast("RelationalField[Any]", meta.fields_map[field_name]).source_fields
                validated_names.update(dict.fromkeys(source_fields))
            elif field_name in meta.generic_foreign_key_fields:
                # A generic foreign key stands for the key columns of all its branches.
                for branch_name in meta.generic_foreign_key_fields[field_name].branch_names:
                    source_fields = cast("RelationalField[Any]", meta.fields_map[branch_name]).source_fields
                    validated_names.update(dict.fromkeys(source_fields))
            elif (
                field_name in meta.backward_foreign_key_fields
                or field_name in meta.backward_one_to_one_fields
                or (field_name in meta.many_to_many_fields)
            ):
                raise FieldError(
                    f"{argument_name!r} names the relation {field_name!r} of model {meta.full_name}, "
                    "which has no column on this model's table"
                )
            else:
                raise FieldError(f"Unknown field {field_name!r} in {argument_name!r} for model {meta.full_name}")
        return tuple(meta.get_with_blind_indexes(validated_names))
