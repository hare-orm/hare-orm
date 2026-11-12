from __future__ import annotations

import collections
import math
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

from hare.dialects.base.types.type_registry import TypeRegistry
from hare.fields.constants import ROLLBACK_RESTORE_UNSET
from hare.models.constants import MIN_STORED_KEY_CODEC_BATCH_SIZE
from hare.models.write.instance_snapshot import InstanceSnapshot
from hare.models.write.rollback_restores import RollbackRestores

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.fields.field import Field
    from hare.models import Model

TInstance = TypeVar("TInstance", bound="Model")


class ReturnedValues:
    """The values a write's ``RETURNING`` rows hold - the columns the database computed - set on
    the written instances."""

    #: A NaN as a key holds it.
    NAN_KEY: ClassVar[object] = object()

    @staticmethod
    def apply_row(
        model: type[Model],
        types: TypeRegistry,
        obj: Model,
        row: Any,
        columns: Iterable[str] | None = None,
        *,
        connection: DatabaseClient,
        readers: Sequence[tuple[str, str, Callable[[Any], Any]]] | None = None,
    ) -> None:
        """Sets on an obj every field a ``RETURNING`` row holds a column of - put back as they were when the
        write's transaction rolls back.

        Args:
            model: The written model.
            types: The connection's type registry.
            obj: The obj whose row was written.
            row: The row, by column name.
            columns: Only these columns of the row - every one by default.
            connection: The connection the write ran on.
        """
        if readers is None:
            readers = ReturnedValues.get_returning_readers(model, types, row.keys() if columns is None else columns)
        for column, field_name, read in readers:
            value = read(row[column])
            RollbackRestores.register_rollback_restore(
                obj, connection, field_name, obj.__dict__.get(field_name, ROLLBACK_RESTORE_UNSET)
            )
            setattr(obj, field_name, value)

    @staticmethod
    def get_returning_readers(
        model: type[Model],
        types: TypeRegistry,
        columns: Iterable[str],
    ) -> tuple[tuple[str, str, Callable[[Any], Any]], ...]:
        """Prepare readers of a RETURNING projection; assignments and rollback stay row-local."""
        meta = model._meta
        return tuple(
            (column, name, types.get_python_reader(meta.fields_map[name]))
            for column in columns
            if (name := meta.fields_db_projection_reverse.get(column)) is not None
        )

    @staticmethod
    def match_rows(
        model: type[Model],
        types: TypeRegistry,
        instances: Iterable[TInstance],
        rows: Sequence[Any],
        field_names: Sequence[str],
    ) -> Iterator[tuple[TInstance, dict[str, Any]]]:
        """Pairs each ``RETURNING`` row with the instance holding the row's values of the given
        fields - the first such instance; a row no instance holds is skipped. An instance's value is
        compared as given, and, for a row none holds so, as the database stores it - a field may write
        a value otherwise (an email's domain in lower case).

        Args:
            model: The written model.
            types: The connection's type registry.
            instances: The written instances.
            rows: The rows.
            field_names: The fields telling the rows apart - the primary key, or a conflict target.

        Yields:
            Each instance with its row, by column name.
        """
        meta = model._meta
        fields = [meta.fields_map[field_name] for field_name in field_names]
        columns = [meta.fields_db_projection[field_name] for field_name in field_names]
        readers = ReturnedValues.get_key_readers(types, fields, columns)
        instances = list(instances)
        instances_by_key: dict[tuple[Any, ...], TInstance] = {}
        for instance in instances:
            key = tuple(ReturnedValues.get_hashable(getattr(instance, field_name)) for field_name in field_names)
            instances_by_key.setdefault(key, instance)
        # Built at the first row no instance holds as given.
        instances_by_stored_key: dict[tuple[Any, ...], TInstance] | None = None
        matched_instance_ids: set[int] = set()
        for raw_row in rows:
            # A driver's row may have no .items()/.get() - read as a dict.
            row = dict(raw_row)
            key = ReturnedValues.get_row_key(readers, row)
            matched_instance = instances_by_key.get(key)
            if matched_instance is None or id(matched_instance) in matched_instance_ids:
                if instances_by_stored_key is None:
                    instances_by_stored_key = ReturnedValues.get_instances_by_stored_key(
                        types, fields, field_names, instances
                    )
                matched_instance = instances_by_stored_key.get(key)
                if matched_instance is None or id(matched_instance) in matched_instance_ids:
                    continue
            matched_instance_ids.add(id(matched_instance))
            yield matched_instance, row

    @staticmethod
    def get_instances_by_stored_key(
        types: TypeRegistry, fields: Sequence[Field[Any]], field_names: Sequence[str], instances: list[TInstance]
    ) -> dict[tuple[Any, ...], TInstance]:
        """The instances by their values of fields as the database stores them - each value written and
        read back through its field; the first instance of a key.

        Args:
            types: The connection's type registry.
            fields: The fields.
            field_names: Their names.
            instances: The instances.

        Returns:
            The first instance of each key.
        """
        instances_by_stored_key: dict[tuple[Any, ...], TInstance] = {}
        for stored_key, instance in zip(
            ReturnedValues.get_stored_keys(types, fields, field_names, instances), instances, strict=True
        ):
            instances_by_stored_key.setdefault(stored_key, instance)
        return instances_by_stored_key

    @staticmethod
    def get_stored_key_reader(
        types: TypeRegistry,
        fields: Sequence[Field[Any]],
        field_names: Sequence[str],
        count: int,
    ) -> Callable[[Model], tuple[Any, ...]]:
        """Prepare stored-key matching for one operation, always returning a tuple.

        NULL and unchanged native keys bypass conversion. Other values are written,
        read and made hashable in field order with the actual instance as context.
        Small batches retain direct dispatch; larger batches bind the same codecs.
        No values or execution connections are retained by the prepared callable.
        """
        prepares_columns = count >= MIN_STORED_KEY_CODEC_BATCH_SIZE
        codecs = tuple(
            (
                field_name,
                types.get_native_key_type(field),
                TypeRegistry.get_db_writer(types, field) if prepares_columns else partial(types.get_db_value, field),
                TypeRegistry.get_python_reader(types, field)
                if prepares_columns
                else partial(types.get_python_value, field),
            )
            for field, field_name in zip(fields, field_names, strict=True)
        )

        def read_key(instance: Model) -> tuple[Any, ...]:
            stored_values = []
            for field_name, native_type, write, read in codecs:
                value = getattr(instance, field_name)
                if value is not None and type(value) is not native_type:
                    value = ReturnedValues.get_hashable(read(write(value, instance)))
                stored_values.append(value)
            return tuple(stored_values)

        return read_key

    @staticmethod
    def get_stored_keys(
        types: TypeRegistry, fields: Sequence[Field[Any]], field_names: Sequence[str], instances: Sequence[Model]
    ) -> list[tuple[Any, ...]]:
        """Read stored matching keys with one preparation, preserving instance order."""
        read_key = ReturnedValues.get_stored_key_reader(types, fields, field_names, len(instances))
        return [read_key(instance) for instance in instances]

    @staticmethod
    def get_hashable(value: Any) -> Any:
        """A value as a key can hold it - a list or tuple (an array, a JSON array) as the tuple of its items,
        a dict or set (a JSON object, a key-value mapping) as the frozenset of its items: a database may give a
        mapping's keys back in an order of its own. Every value inside them too.

        Ordinary scalar values remain unchanged. All floating-point NaNs share one marker so keys
        compare equal even when Python's original NaN values do not.

        Args:
            value: The value.

        Returns:
            The value, or its tuples.
        """
        value_type = type(value)
        if value is None or value_type is int or value_type is str or value_type is bool or value_type is bytes:
            return value
        if value_type is float:
            return ReturnedValues.NAN_KEY if math.isnan(value) else value
        if isinstance(value, Mapping):
            return frozenset((key, ReturnedValues.get_hashable(item)) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return tuple(ReturnedValues.get_hashable(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return frozenset(ReturnedValues.get_hashable(item) for item in value)
        if isinstance(value, float) and math.isnan(value):
            # Equal to itself, as a unique index holds it - NaN is equal to nothing in Python.
            return ReturnedValues.NAN_KEY
        return value

    @staticmethod
    def get_key_readers(
        types: TypeRegistry, fields: Sequence[Field[Any]], columns: Sequence[str]
    ) -> list[tuple[Callable[[Any], Any], str]]:
        """The reader and the column of each field telling returned rows apart - taken once per batch.

        Args:
            types: The connection's type registry.
            fields: The fields.
            columns: Their columns.

        Returns:
            The readers, in the fields' order.
        """
        return [(types.get_python_reader(field), column) for field, column in zip(fields, columns, strict=True)]

    @staticmethod
    def get_row_key(readers: Sequence[tuple[Callable[[Any], Any], str]], row: Mapping[str, Any]) -> tuple[Any, ...]:
        """A returned row's values of the fields, as the fields read them.

        Args:
            readers: ``get_key_readers()``.
            row: The row, by column name.

        Returns:
            The values, in the fields' order.
        """
        return tuple(ReturnedValues.get_hashable(read(row.get(column))) for read, column in readers)

    @staticmethod
    def match_rows_in_order(
        model: type[Model],
        types: TypeRegistry,
        instances: Iterable[TInstance],
        rows: Sequence[Any],
        field_names: Sequence[str],
    ) -> Iterator[tuple[TInstance, dict[str, Any]]]:
        """Pairs the rows a database returns in the order it wrote them with their instances - each row
        with the next instance after the previous row's whose stored values of the fields are the row's: an
        instance a conflict skipped holds the values of an earlier one, and has no row.

        Args:
            model: The written model.
            types: The connection's type registry.
            instances: The written instances, in the order written.
            rows: The rows, in that order.
            field_names: The fields telling the rows apart - the conflict target, or the primary key.

        Yields:
            Each instance with its row, by column name.
        """
        meta = model._meta
        fields = [meta.fields_map[field_name] for field_name in field_names]
        columns = [meta.fields_db_projection[field_name] for field_name in field_names]
        instances = list(instances)
        if len(rows) == len(instances):
            # Nothing was skipped - each row is its instance's, whatever the instance gave the target.
            yield from ((instance, dict(raw_row)) for instance, raw_row in zip(instances, rows, strict=True))
            return
        stored_keys = ReturnedValues.get_stored_keys(types, fields, field_names, instances)
        readers = ReturnedValues.get_key_readers(types, fields, columns)
        next_index = 0
        for raw_row in rows:
            # A driver's row may have no .items()/.get() - read as a dict.
            row = dict(raw_row)
            key = ReturnedValues.get_row_key(readers, row)
            for index in range(next_index, len(instances)):
                if stored_keys[index] == key:
                    yield instances[index], row
                    next_index = index + 1
                    break

    @staticmethod
    def match_rows_by_stored_values(
        model: type[Model],
        types: TypeRegistry,
        instances: Iterable[TInstance],
        rows: Sequence[Any],
        field_names: Sequence[str],
    ) -> Iterator[tuple[TInstance, dict[str, Any]]]:
        """Pairs rows a database returns in no order with their instances by every field written - each row
        with the first instance not yet paired whose stored values are the row's; instances alike in all of
        them are interchangeable.

        Args:
            model: The written model.
            types: The connection's type registry.
            instances: The written instances, in the order written.
            rows: The rows.
            field_names: The fields written.

        Yields:
            Each instance with its row, by column name.
        """
        meta = model._meta
        fields = [meta.fields_map[field_name] for field_name in field_names]
        columns = [meta.fields_db_projection[field_name] for field_name in field_names]
        readers = ReturnedValues.get_key_readers(types, fields, columns)
        instances = list(instances)
        instances_by_stored_key: collections.defaultdict[tuple[Any, ...], collections.deque[TInstance]] = (
            collections.defaultdict(collections.deque)
        )
        for stored_key, instance in zip(
            ReturnedValues.get_stored_keys(types, fields, field_names, instances), instances, strict=True
        ):
            instances_by_stored_key[stored_key].append(instance)
        for raw_row in rows:
            row = dict(raw_row)
            key = ReturnedValues.get_row_key(readers, row)
            matching_instances = instances_by_stored_key.get(key)
            if matching_instances:
                yield matching_instances.popleft(), row

    @staticmethod
    def leave_unloaded(
        model: type[Model], objs: Iterable[Model], columns: Iterable[str], *, connection: DatabaseClient
    ) -> None:
        """Leaves the fields of columns the database gave values to unloaded on the written objects - a
        write returning no row tells none of them; ``refresh_from_db()`` reads them. A field left to its
        ``db_default`` keeps ``DatabaseDefault``, as before the write; a field the database computes
        is read again by nothing, and reading it raises ``NoValuesFetched``. Put back as they were when
        the write's transaction rolls back.

        Args:
            model: The written model.
            objs: The written objects.
            columns: The columns.
            connection: The connection the write ran on.
        """
        # Local import: the fields' package imports the models' package.
        from hare.fields.database_default import DatabaseDefault

        field_names_by_column = model._meta.fields_db_projection_reverse
        field_names = [field_names_by_column.get(column, column) for column in columns]
        snapshots: list[InstanceSnapshot] | None = [] if connection.is_transaction_client else None
        for obj in objs:
            old_values: dict[str, Any] | None = {} if snapshots is not None else None
            for field_name in field_names:
                if not isinstance(obj.__dict__.get(field_name), DatabaseDefault) and field_name in obj.__dict__:
                    if old_values is not None:
                        old_values[field_name] = obj.__dict__[field_name]
                    del obj.__dict__[field_name]
            if snapshots is not None and old_values:
                snapshots.append(InstanceSnapshot(obj, old_values))
        if snapshots:
            RollbackRestores.register_snapshots(connection, snapshots)
