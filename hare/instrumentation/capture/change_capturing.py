from __future__ import annotations

from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from hare.core.constants import CACHE_MISS
from hare.exceptions import ConfigurationError
from hare.instrumentation.capture.capture_needs import CaptureNeeds
from hare.instrumentation.capture.captured_change import CapturedChange
from hare.instrumentation.capture.change_sink import ChangeSink
from hare.instrumentation.enums import ChangePayload, RowOperation
from hare.query.constants import RETURNING_OLD_COLUMN_ALIAS_PREFIX
from hare.sql.terms.values.old_row_value import OldRowValue
from hare.time import Timezone

if TYPE_CHECKING:  # pragma: nocoverage
    import datetime

    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.types.type_registry import TypeRegistry
    from hare.models import Model
    from hare.models.meta_info import MetaInfo


class ChangeCapturing:
    """The steps every write of a model with ``Meta.change_capture`` runs: what it returns of its rows,
    how they become ``CapturedChange``s and their delivery to the sink, in the write's transaction.
    A model without it pays one attribute read per write.
    """

    @staticmethod
    def build_needs(meta: MetaInfo) -> CaptureNeeds | None:
        """Works out what the writes of a model read for its ``Meta.change_capture``.

        Args:
            meta: The model's meta, its fields finalised.

        Returns:
            The needs, None for a model without ``Meta.change_capture`` or an abstract one.

        Raises:
            ConfigurationError: ``Meta.change_capture`` isn't a ``ChangeSink``, or doesn't fit the
                model.
        """
        sink = meta.change_capture
        if sink is None or meta.abstract:
            return None
        model = meta._model
        if not isinstance(sink, ChangeSink):
            raise ConfigurationError(
                f"{model.__name__}: Meta.change_capture must be a ChangeSink (hare.contrib.outbox.ChangeCapture), "
                f"got {sink!r}"
            )
        if not meta.has_primary_key:
            raise ConfigurationError(
                f"{model.__name__}: Meta.change_capture needs a primary key - a change names its row by it"
            )
        field_names = sink.get_field_names(model) if sink.payload is not ChangePayload.KEYS else ()
        projection = meta.fields_db_projection
        tenant_field_name = meta.tenant_field if meta.tenant_field in projection else None
        if tenant_field_name is None and meta.tenant_field is not None:
            tenant_field_name = next(iter(getattr(meta.fields_map[meta.tenant_field], "source_fields", ())), None)
        column_names = (
            *field_names,
            *meta.primary_key_attribute_names,
            *((tenant_field_name,) if tenant_field_name else ()),
        )
        columns = tuple(dict.fromkeys(projection[name] for name in column_names))
        return CaptureNeeds(
            sink=sink,
            payload=sink.payload,
            operations=frozenset(sink.operations),
            field_names=field_names,
            columns=columns,
            tenant_field_name=tenant_field_name,
        )

    @staticmethod
    def is_in_transaction(connection: DatabaseClient) -> bool:
        """Whether a connection is an open transaction.

        Args:
            connection: The connection.

        Returns:
            True inside a transaction.
        """
        # Local import: the client module imports the instrumentation package.
        from hare.dialects.base.client.transaction_client import TransactionClient

        return isinstance(connection, TransactionClient) and not connection._is_transaction_finished()

    @staticmethod
    def needs_transaction(connection: DatabaseClient) -> bool:
        """Whether a write of a captured model on a connection opens a transaction for itself and its
        changes - none is open, and the database has transactions.

        Args:
            connection: The connection of the write.

        Returns:
            True when it does.
        """
        return connection.features.supports_transactions and not ChangeCapturing.is_in_transaction(connection)

    @staticmethod
    @asynccontextmanager
    async def transaction(connection: DatabaseClient) -> AsyncIterator[DatabaseClient]:
        """The transaction a write of a captured model and its changes share - the open one, else a
        new one around both; on a database without transactions, none.

        Args:
            connection: The connection of the write.

        Yields:
            The connection to write on.
        """
        if not ChangeCapturing.needs_transaction(connection):
            yield connection
            return
        async with connection._in_transaction() as transaction_connection:
            yield transaction_connection

    @staticmethod
    def get_pk(model: type[Model], values: Mapping[str, Any]) -> Any:
        """A row's primary key from its field values.

        Args:
            model: The model.
            values: The row's values by field name, its key fields among them.

        Returns:
            The key - a tuple for a composite one.
        """
        primary_key_attribute_names = model._meta.primary_key_attribute_names
        if len(primary_key_attribute_names) == 1:
            return values[primary_key_attribute_names[0]]
        return tuple(values[name] for name in primary_key_attribute_names)

    @staticmethod
    def get_instance_values(obj: Model, needs: CaptureNeeds) -> dict[str, Any] | None:
        """The captured fields of an obj as it holds them.

        Args:
            obj: The obj.
            needs: The model's needs.

        Returns:
            The values by field name, None when a captured field isn't loaded on the obj or still
            waits for its database default.
        """
        # Local import: the defaults module imports the fields package, which imports this one.
        from hare.fields.database_default import DatabaseDefault

        values: dict[str, Any] = {}
        instance_values = obj.__dict__
        for name in needs.field_names:
            value = instance_values.get(name, CACHE_MISS)
            if value is CACHE_MISS or isinstance(value, DatabaseDefault):
                return None
            values[name] = value
        return values

    @staticmethod
    def get_tenant(obj: Model, needs: CaptureNeeds) -> Any:
        """The tenant of an obj's row.

        Args:
            obj: The obj.
            needs: The model's needs.

        Returns:
            The tenant, None for a model without tenants.
        """
        return getattr(obj, needs.tenant_field_name, None) if needs.tenant_field_name else None

    @staticmethod
    async def read_values(
        model: type[Model], connection: DatabaseClient, pks: Sequence[Any], needs: CaptureNeeds, *, lock: bool = False
    ) -> dict[Any, tuple[dict[str, Any], Any]]:
        """Reads the captured fields of rows - every tenant's, soft-deleted ones included.

        Args:
            model: The model.
            connection: The connection - the write's transaction.
            pks: The rows' primary keys.
            needs: The model's needs.
            lock: Lock the rows until the transaction ends (``SELECT ... FOR UPDATE``), where the
                database locks rows.

        Returns:
            The values and the tenant of each row found, by primary key.
        """
        # Local import: the deletion helpers import the queryset package, which imports this module.
        from hare.models.deletion.cascade.related_rows import RelatedRows

        rows: dict[Any, tuple[dict[str, Any], Any]] = {}
        for batch in RelatedRows.split_into_batches(
            list(pks), connection, len(model._meta.primary_key_attribute_names)
        ):
            rows.update(await ChangeCapturing.read_values_of(model, connection, batch, needs, lock=lock))
        return rows

    @staticmethod
    async def read_values_of(
        model: type[Model], connection: DatabaseClient, keys: Any, needs: CaptureNeeds, *, lock: bool = False
    ) -> dict[Any, tuple[dict[str, Any], Any]]:
        """Reads the captured fields of the rows of some primary keys in one query - every tenant's,
        soft-deleted ones included.

        Args:
            model: The model.
            connection: The connection - the write's transaction.
            keys: The primary keys - a list, or a query of them.
            needs: The model's needs.
            lock: Lock the rows until the transaction ends (``SELECT ... FOR UPDATE``), where the
                database locks rows.

        Returns:
            The values and the tenant of each row found, by primary key.
        """
        # Local import: the deletion helpers import the queryset package, which imports this module.
        from hare.models.deletion.cascade.related_rows import RelatedRows

        tenant_names = (needs.tenant_field_name,) if needs.tenant_field_name else ()
        names = list(dict.fromkeys((*model._meta.primary_key_attribute_names, *needs.field_names, *tenant_names)))
        queryset = RelatedRows.include_soft_deleted(
            RelatedRows.get_base_queryset(model).filter(pk__in=keys).using(connection)
        )
        if lock and connection.features.supports_select_for_update:
            queryset = queryset.select_for_update()
        rows: dict[Any, tuple[dict[str, Any], Any]] = {}
        for row in await queryset.values_list(*names):
            values = dict(zip(names, row, strict=True))
            rows[ChangeCapturing.get_pk(model, values)] = (
                {name: values[name] for name in needs.field_names},
                values[needs.tenant_field_name] if needs.tenant_field_name else None,
            )
        return rows

    @staticmethod
    def get_old_value_terms(model: type[Model], needs: CaptureNeeds) -> list[OldRowValue]:
        """The captured columns as they were before an ``UPDATE`` - ``RETURNING OLD``.

        Args:
            model: The model.
            needs: The model's needs.

        Returns:
            A term per captured column.
        """
        projection = model._meta.fields_db_projection
        return [
            OldRowValue(column, alias=f"{RETURNING_OLD_COLUMN_ALIAS_PREFIX}{column}")
            for column in dict.fromkeys(projection[name] for name in needs.field_names)
        ]

    @staticmethod
    def get_row_values(
        model: type[Model], needs: CaptureNeeds, types: TypeRegistry, row: Mapping[str, Any], *, old: bool = False
    ) -> dict[str, Any]:
        """The captured fields of a row a write returned.

        Args:
            model: The model.
            needs: The model's needs.
            types: The connection's type registry.
            row: The returned row, by column.
            old: Read the values the row held before the write (``RETURNING OLD``).

        Returns:
            The values by field name.
        """
        meta = model._meta
        projection = meta.fields_db_projection
        fields_map = meta.fields_map
        prefix = RETURNING_OLD_COLUMN_ALIAS_PREFIX if old else ""
        return {
            name: types.get_python_value(fields_map[name], row[f"{prefix}{projection[name]}"])
            for name in needs.field_names
        }

    @staticmethod
    def get_row_key(
        model: type[Model], needs: CaptureNeeds, types: TypeRegistry, row: Mapping[str, Any]
    ) -> tuple[Any, Any]:
        """The primary key and the tenant of a row a write returned.

        Args:
            model: The model.
            needs: The model's needs.
            types: The connection's type registry.
            row: The returned row, by column.

        Returns:
            The key - a tuple for a composite one - and the tenant, None without tenants.
        """
        meta = model._meta
        projection = meta.fields_db_projection
        fields_map = meta.fields_map
        key_values = [
            types.get_python_value(fields_map[name], row[projection[name]])
            for name in meta.primary_key_attribute_names
        ]
        tenant = (
            types.get_python_value(fields_map[needs.tenant_field_name], row[projection[needs.tenant_field_name]])
            if needs.tenant_field_name
            else None
        )
        return key_values[0] if len(key_values) == 1 else tuple(key_values), tenant

    @staticmethod
    def build_change(
        model: type[Model],
        needs: CaptureNeeds,
        operation: RowOperation,
        pk: Any,
        *,
        changed: Iterable[str] | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        tenant: Any = None,
        occurred_at: datetime.datetime | None = None,
    ) -> CapturedChange:
        """A change, holding of its row what the payload asks for: nothing but the key for
        ``KEYS``; the row as the write left it for ``AFTER`` - as it was for a delete; both for
        ``BEFORE_AND_AFTER``.

        Args:
            model: The model.
            needs: The model's needs.
            operation: What the write did.
            pk: The row's primary key.
            changed: The fields an update set, None when not known.
            before: The captured fields before the write, None when not read.
            after: The captured fields after it, None when not read.
            tenant: The row's tenant.
            occurred_at: When the write ran - now by default.

        Returns:
            The change.
        """
        payload = needs.payload
        if payload is ChangePayload.KEYS:
            before = after = None
        elif operation is RowOperation.INSERT:
            before = None
        elif operation is RowOperation.DELETE:
            after = None
        elif payload is ChangePayload.AFTER:
            before = None
        return CapturedChange(
            model=model,
            operation=operation,
            pk=pk,
            changed=None if changed is None or operation is not RowOperation.UPDATE else tuple(changed),
            before=before,
            after=after,
            occurred_at=occurred_at or Timezone.now(),
            tenant=tenant,
        )

    @staticmethod
    def get_returned_changes(
        model: type[Model],
        needs: CaptureNeeds,
        types: TypeRegistry,
        raw_rows: Iterable[Any],
        operation: RowOperation,
        *,
        changed: Iterable[str] | None = None,
        returns_old: bool = False,
        before_by_pk: Mapping[Any, Mapping[str, Any]] | None = None,
    ) -> list[CapturedChange]:
        """The changes of the rows a write returned - ``RETURNING`` of the captured columns.

        Args:
            model: The model.
            needs: The model's needs.
            types: The connection's type registry.
            raw_rows: The returned rows, by column.
            operation: What the write did.
            changed: The fields an update set.
            returns_old: The rows hold the values before the write too (``RETURNING OLD``).
            before_by_pk: The values before the write, read before it, by primary key.

        Returns:
            A change per row.
        """
        changed_names = None if changed is None else tuple(changed)
        occurred_at = Timezone.now()
        changes: list[CapturedChange] = []
        for raw_row in raw_rows:
            # A sqlite3.Row has no .get() - read as a dict like a PostgreSQL row.
            row = dict(raw_row)
            pk, tenant = ChangeCapturing.get_row_key(model, needs, types, row)
            before: Mapping[str, Any] | None
            after: Mapping[str, Any] | None
            values = ChangeCapturing.get_row_values(model, needs, types, row) if needs.reads_values else None
            if operation is RowOperation.DELETE:
                before, after = values, None
            else:
                after = values
                if returns_old:
                    before = ChangeCapturing.get_row_values(model, needs, types, row, old=True)
                elif before_by_pk is not None:
                    before = before_by_pk.get(pk)
                else:
                    before = None
            changes.append(
                ChangeCapturing.build_change(
                    model,
                    needs,
                    operation,
                    pk,
                    changed=changed_names,
                    before=before,
                    after=after,
                    tenant=tenant,
                    occurred_at=occurred_at,
                )
            )
        return changes

    @staticmethod
    async def capture(connection: DatabaseClient, model: type[Model], changes: list[CapturedChange]) -> None:
        """Gives the changes of a write to the model's sink - those of a captured operation.

        Args:
            connection: The connection of the write - its transaction.
            model: The model.
            changes: The changes.
        """
        needs = model._meta.change_capture_needs
        if needs is None:
            return
        captured = [change for change in changes if change.operation in needs.operations]
        if captured:
            await needs.sink.write(connection, model, captured)
