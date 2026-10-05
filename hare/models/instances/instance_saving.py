from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, NoReturn, cast

from hare.exceptions import FieldError, IncompleteInstanceError, IntegrityError, QueryError, StaleObjectError
from hare.fields.relations.fields.relational_field import RelationalField
from hare.instrumentation.change_events import ChangeEvents
from hare.instrumentation.enums import RowOperation
from hare.models.instances.dirty_fields import DirtyFields
from hare.models.instances.instance_connections import InstanceConnections
from hare.models.tenancy.tenancy import Tenancy
from hare.models.write.instance_writer import InstanceWriter
from hare.models.write.rollback_restores import RollbackRestores
from hare.models.write.write_fields import WriteFields
from hare.models.write.write_steps import WriteSteps
from hare.query.scopes.row_scopes import RowScopes
from hare.query.scopes.row_visibility import RowVisibility

if TYPE_CHECKING:  # pragma: nocoverage
    from collections.abc import Iterable

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
        """retrieve value from field's async default value

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
                f"'{optimistic_lock_field}' is not available - every {operation}() needs to read and "
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
        if not update_fields:
            raise IncompleteInstanceError(
                f"{obj.__class__.__name__} is a partial model, can only be saved with the "
                "relevant update_field provided"
            )
        for field in update_fields:
            if not all(hasattr(obj, pk_name) for pk_name in obj._meta.primary_key_attribute_names):
                raise IncompleteInstanceError(
                    f"{obj.__class__.__name__} is a partial model without primary key "
                    "fetched. Partial update not available"
                )
            if not hasattr(obj, field):
                raise IncompleteInstanceError(
                    f"{obj.__class__.__name__} is a partial model, field '{field}' is not available"
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
            StaleObjectError: ``Meta.optimistic_lock_field`` is set - the row was modified
                concurrently.
            IntegrityError: Otherwise.
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
        InstanceSaving.raise_for_unmatched_update(obj, old_version, message)

    @staticmethod
    async def record_written(
        obj: Model,
        connection: DatabaseClient,
        was_insert: bool,
        partial_update_fields: list[str] | None,
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
            partial_update_fields: The fields a partial UPDATE wrote, None for a whole write.
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
                RollbackRestores.register_rollback_restore(obj, connection, "pk", pk_before)
            if meta.track_dirty_fields:
                RollbackRestores.register_rollback_restore(obj, connection, "_dirty_snapshot", dirty_snapshot_before)

        # Not a field name - set past the overridden __setattr__.
        object.__setattr__(obj, "_saved_in_db", True)
        InstanceConnections.remember_connection(obj, connection)
        if was_insert and not custom_generated_pk_before:
            # The pk the database just generated was assigned through Model.__setattr__ while the
            # obj still counted as unsaved - it isn't a caller-supplied value.
            object.__setattr__(obj, "_custom_generated_pk", False)
        if meta.track_dirty_fields:
            # After a partial UPDATE only the written fields are clean - the other unsaved changes
            # of the obj stay dirty.
            if partial_update_fields is not None:
                DirtyFields.sync_dirty_snapshot_fields(
                    obj, WriteFields.of(obj.__class__).get_written_with(partial_update_fields)
                )
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
    def raise_for_unmatched_update(obj: Model, old_version: Any, message: str) -> NoReturn:
        """Raises for an UPDATE of this obj's row that matched no row, after putting the bumped
        in-memory version back.

        Args:
            obj: The model obj.
            old_version: The version before the UPDATE.
            message: The ``IntegrityError`` message.

        Raises:
            StaleObjectError: ``Meta.optimistic_lock_field`` is set - the row was modified
                concurrently.
            IntegrityError: Otherwise.
        """
        optimistic_lock_field = obj._meta.optimistic_lock_field
        if optimistic_lock_field:
            setattr(obj, optimistic_lock_field, old_version)
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
            QueryError: The arguments contradict each other, ``update_fields`` is a single string or
                names ``Meta.soft_delete_field``, or an UPDATE is asked of a model without a
                primary key.
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
        validated_fields = InstanceSaving.get_validated_field_names(type(obj), update_fields, "update_fields")
        if meta.soft_delete_field in validated_fields:
            raise QueryError(
                f"Cannot set '{meta.soft_delete_field}' via save(update_fields=...) - use .delete()/.restore() instead"
            )
        return validated_fields

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
            QueryError: ``changed_only`` isn't a bool, is given with ``update_fields`` or
                ``force_create``, or the model has no ``Meta.track_dirty_fields``.
        """
        if not isinstance(changed_only, bool):
            raise QueryError(f"save(changed_only=...) takes a bool, got {changed_only!r}")
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
        return [
            field_name
            for field_name in obj.get_dirty_fields()
            if field_name not in meta.primary_key_attribute_names and not meta.fields_map[field_name].generated
        ]

    @staticmethod
    def get_validated_field_names(
        model: type[Model], field_names: Iterable[str], argument_name: str
    ) -> tuple[str, ...]:
        """
        Materializes a caller-supplied collection of field names once and validates every name.

        Args:
            model: The model.
            field_names: Model field names, any iterable (a generator is consumed here, once).
            argument_name: The caller's parameter name, used in raised messages.

        Returns:
            The names without duplicates, in the given order, ``pk`` expanded to the
            primary key attribute(s) and a forward relation to its key column(s).

        Raises:
            QueryError: If ``field_names`` is a single string instead of a collection of names.
            FieldError: If a name is not a column-backed field or a forward relation of this model.
        """
        meta = model._meta
        if isinstance(field_names, (str, bytes)):
            raise QueryError(
                f"'{argument_name}' must be a collection of field names, not a single string - "
                f"pass [{field_names!r}] instead of {field_names!r}"
            )
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
                    f"'{argument_name}' names the relation '{field_name}' of model {meta.full_name}, "
                    "which has no column on this model's table"
                )
            else:
                raise FieldError(f"Unknown field '{field_name}' in '{argument_name}' for model {meta.full_name}")
        return tuple(meta.get_with_blind_indexes(validated_names))
