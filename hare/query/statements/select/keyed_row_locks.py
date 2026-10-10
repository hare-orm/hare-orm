from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from hare.exceptions import OperationalError, TransactionRetryError
from hare.query.queryset.specification_copying import SpecificationCopying
from hare.query.statements.building.row_locks import RowLocks

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.client.transaction_client import TransactionClient
    from hare.models import Model
    from hare.query.queryset.queryset import QuerySet
    from hare.query.statements.select.rows_query import RowsQuery


class KeyedRowLocks:
    """``select_for_update()`` on a database locking rows by their keys (``Features.locks_rows_by_key``):
    the keys of the rows - and of the relations ``of=`` names - are read in the transaction, locked
    through it in the order of their names, then the rows are read by their keys. A transaction reads the
    rows as they were when it began: a row whose lock was waited for is read again outside it, and a row
    another transaction changed meanwhile ends the read with ``TransactionRetryError``."""

    @staticmethod
    async def read_locked_rows(query: RowsQuery[Any]) -> Any:
        """Locks the rows of a query, then reads them.

        Args:
            query: The query, not built yet.

        Returns:
            What the query returns.
        """
        locked_queryset = await KeyedRowLocks.get_locked_queryset(query)
        return await locked_queryset

    @staticmethod
    async def get_locked_queryset(query: RowsQuery[Any]) -> QuerySet[Any, Any]:
        """Locks the rows of a query.

        Args:
            query: The query, not built yet.

        Returns:
            The queryset reading the locked rows, ordered as the query orders them.

        Raises:
            QueryError: The query can't lock its rows, or runs outside a transaction.
            OperationalError: ``nowait=True`` and another transaction holds a lock.
            TransactionRetryError: A row waited for was changed by the transaction holding its lock.
        """
        query._raise_if_row_lock_outside_transaction()
        source = KeyedRowLocks.get_source_queryset(query)
        # Built for its checks alone - the rows are read by the queryset of the locked keys.
        query._make_query_to_run()
        connection = cast("TransactionClient", query._connection)
        locked_paths = KeyedRowLocks.get_locked_paths(query)
        if query._select_for_update_skip_locked:
            keys = await KeyedRowLocks.take_free_rows(source, locked_paths, connection)
        else:
            keys = await KeyedRowLocks.take_rows(
                source, locked_paths, connection, wait=not query._select_for_update_nowait
            )
        source._limit = None
        source._offset = None
        source._is_single_row_of_slice = False
        return source.filter(pk__in=keys)

    @staticmethod
    def get_source_queryset(query: RowsQuery[Any]) -> QuerySet[Any, Any]:
        """The queryset of the query's rows, with no lock.

        Args:
            query: The query.

        Returns:
            The queryset.
        """
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.queryset import QuerySet

        source: QuerySet[Any, Any] = QuerySet(query.model)
        SpecificationCopying.copy_specification(query, source)
        source._select_for_update = False
        return source

    @staticmethod
    def get_locked_paths(query: RowsQuery[Any]) -> list[tuple[str, type[Model]]]:
        """The relation paths whose rows are locked, with their models - ``""`` for the query's own
        model.

        Args:
            query: The query.

        Returns:
            The paths, the query's own model alone with no ``of=``.
        """
        names = query._select_for_update_of
        if not names:
            return [("", query.model)]
        locked_paths: list[tuple[str, type[Model]]] = []
        for name in sorted(names):
            if name in ("self", query.model._meta.db_table):
                locked_paths.append(("", query.model))
            else:
                locked_paths.append((name, RowLocks.get_relation_path_fields(query, name)[-1].related_model))
        return locked_paths

    @staticmethod
    async def read_key_rows(
        source: QuerySet[Any, Any], locked_paths: list[tuple[str, type[Model]]], *, sliced: bool
    ) -> list[tuple[Any, ...]]:
        """Reads the keys of the rows: the model's key, then the key of each locked relation.

        Args:
            source: The queryset of the rows.
            locked_paths: The locked relation paths.
            sliced: Whether the slice of the queryset applies.

        Returns:
            The rows of keys, each once, in the order of the rows.
        """
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.selection.statement_selection import StatementSelection

        keys_queryset = StatementSelection.get_values_rows_queryset(source)
        if not sliced:
            keys_queryset._limit = None
            keys_queryset._offset = None
        columns = list(source.model._meta.primary_key_attribute_names)
        for path, model in locked_paths:
            if path:
                columns.extend(
                    f"{path}__{attribute_name}" for attribute_name in model._meta.primary_key_attribute_names
                )
        return list(dict.fromkeys(await keys_queryset.values_list(*columns)))

    @staticmethod
    def get_row_lock_names(
        key_row: tuple[Any, ...], locked_paths: list[tuple[str, type[Model]]], model: type[Model]
    ) -> dict[str, tuple[type[Model], Any]]:
        """The lock names of a row of keys, each with its model and key - none for an empty relation.

        Args:
            key_row: The row of keys.
            locked_paths: The locked relation paths.
            model: The query's model.

        Returns:
            The model and key by lock name.
        """
        position = len(model._meta.primary_key_attribute_names)
        lock_names: dict[str, tuple[type[Model], Any]] = {}
        for path, locked_model in locked_paths:
            if not path:
                key_parts = key_row[:position]
            else:
                key_width = len(locked_model._meta.primary_key_attribute_names)
                key_parts = key_row[position : position + key_width]
                position += key_width
                if any(part is None for part in key_parts):
                    continue
            key = key_parts[0] if len(key_parts) == 1 else key_parts
            encoded_key = ",".join(quote(str(part), safe="") for part in key_parts)
            lock_names[f"{locked_model._meta.db_table}/{encoded_key}"] = (locked_model, key)
        return lock_names

    @staticmethod
    def get_row_key(key_row: tuple[Any, ...], model: type[Model]) -> Any:
        """The model's key of a row of keys.

        Args:
            key_row: The row of keys.
            model: The query's model.

        Returns:
            The key - a tuple for a key of several columns.
        """
        key_width = len(model._meta.primary_key_attribute_names)
        return key_row[0] if key_width == 1 else key_row[:key_width]

    @staticmethod
    async def take_rows(
        source: QuerySet[Any, Any],
        locked_paths: list[tuple[str, type[Model]]],
        connection: TransactionClient,
        *,
        wait: bool,
    ) -> list[Any]:
        """Locks every row of the queryset's slice.

        Args:
            source: The queryset of the rows.
            locked_paths: The locked relation paths.
            connection: The transaction.
            wait: Whether to wait for the locks other transactions hold.

        Returns:
            The keys of the rows.

        Raises:
            OperationalError: Not waiting, and another transaction holds a lock.
            TransactionRetryError: A row waited for was changed by the transaction holding its lock.
        """
        model = source.model
        key_rows = await KeyedRowLocks.read_key_rows(source, locked_paths, sliced=True)
        keys_by_name: dict[str, tuple[type[Model], Any]] = {}
        for key_row in key_rows:
            keys_by_name.update(KeyedRowLocks.get_row_lock_names(key_row, locked_paths, model))
        outcome = await connection.take_row_locks(sorted(keys_by_name), wait=wait)
        if outcome.busy:
            raise OperationalError(
                f"select_for_update(nowait=True) found {len(outcome.busy)} row(s) of {model.__name__} locked by "
                "another transaction"
            )
        if outcome.waited:
            await KeyedRowLocks.check_waited_rows(connection, [keys_by_name[name] for name in outcome.waited])
        return [KeyedRowLocks.get_row_key(key_row, model) for key_row in key_rows]

    @staticmethod
    async def take_free_rows(
        source: QuerySet[Any, Any], locked_paths: list[tuple[str, type[Model]]], connection: TransactionClient
    ) -> list[Any]:
        """Locks the rows no other transaction holds - ``skip_locked=True`` - in the order of the rows,
        until the slice of the queryset is filled.

        Args:
            source: The queryset of the rows.
            locked_paths: The locked relation paths.
            connection: The transaction.

        Returns:
            The keys of the rows of the slice.
        """
        model = source.model
        offset = source._offset or 0
        wanted = None if source._limit is None else offset + source._limit
        key_rows = await KeyedRowLocks.read_key_rows(source, locked_paths, sliced=False)
        taken_rows: list[tuple[Any, ...]] = []
        position = 0
        while position < len(key_rows) and (wanted is None or len(taken_rows) < wanted):
            chunk_size = len(key_rows) if wanted is None else wanted - len(taken_rows)
            chunk = key_rows[position : position + chunk_size]
            position += len(chunk)
            chunk_names = [KeyedRowLocks.get_row_lock_names(key_row, locked_paths, model) for key_row in chunk]
            outcome = await connection.take_row_locks(
                sorted({name for lock_names in chunk_names for name in lock_names}), wait=False
            )
            busy_names = set(outcome.busy)
            taken_rows.extend(
                key_row
                for key_row, lock_names in zip(chunk, chunk_names, strict=True)
                if busy_names.isdisjoint(lock_names)
            )
        return [KeyedRowLocks.get_row_key(key_row, model) for key_row in taken_rows[offset:]]

    @staticmethod
    async def check_waited_rows(connection: TransactionClient, model_keys: list[tuple[type[Model], Any]]) -> None:
        """Compares the rows whose locks were waited for as the transaction reads them with the rows as
        they are now.

        Args:
            connection: The transaction.
            model_keys: The model and key of each row.

        Raises:
            TransactionRetryError: A row differs - the transaction holding its lock changed it.
        """
        keys_by_model: dict[type[Model], list[Any]] = {}
        for model, key in model_keys:
            keys_by_model.setdefault(model, []).append(key)
        outside_connection = connection._get_top_level_transaction()._parent
        for model, keys in keys_by_model.items():
            rows_read = await KeyedRowLocks.read_rows(model, keys, connection)
            if rows_read != await KeyedRowLocks.read_rows(model, keys, outside_connection):
                raise TransactionRetryError(
                    f"A row of {model.__name__} select_for_update() waited for was changed by another transaction "
                    "after this one began - the transaction reads the rows as they were when it began. Run the "
                    "transaction again."
                )

    @staticmethod
    async def read_rows(model: type[Model], keys: list[Any], connection: DatabaseClient) -> list[str]:
        """Reads rows by their keys, comparable whatever their values are.

        Args:
            model: The model.
            keys: The keys.
            connection: The connection read on.

        Returns:
            The text of each row, sorted.
        """
        # Local import: the queryset package imports the query statements.
        from hare.query.queryset.queryset import QuerySet

        queryset: QuerySet[Any, Any] = QuerySet(model).filter(pk__in=keys)
        queryset._apply_connection(connection)
        return sorted(repr(row) for row in await queryset.values_list())
