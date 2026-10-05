from __future__ import annotations

import abc
import dataclasses
import re
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.classes.class_path import ClassPath
from hare.ddl.constraints.exclusion_constraint import ExclusionConstraint
from hare.ddl.constraints.unique_constraint import UniqueConstraint
from hare.ddl.indexes.index import Index
from hare.dialects.postgresql.enums import PartitionStrategy
from hare.dialects.postgresql.partitioning.constants import PARTITION_NAME_PATTERN
from hare.exceptions import ConfigurationError
from hare.fields.relations.fields.foreign_key_field_instance import ForeignKeyFieldInstance
from hare.sql.identifiers import Identifiers

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.fields.field import Field
    from hare.models import Model

#: Renders the value of a partition bound for the key column at a position: ``(position, value) -> SQL``.
BoundValueRenderer = Callable[[int, Any], str]


@dataclasses.dataclass(frozen=True)
class Partitioning(abc.ABC):
    """How a PostgreSQL table is split into partitions - ``PostgresqlTableOptions(partitioning=...)``:
    the table is created ``PARTITION BY <strategy> (<key columns>)``, and each partition as a table
    of its own (``<table>_<partition name>``) holding the rows of its key values.

    Attributes:
        fields: The fields of the partition key, in order - a field with a column, or a foreign
            key, which stands for its key column(s).
    """

    strategy: ClassVar[PartitionStrategy]

    fields: tuple[str, ...]

    def __post_init__(self) -> None:
        """Freezes the key fields as a tuple and checks them.

        Raises:
            ConfigurationError: No key field is given, a name isn't a string, or a name repeats.
        """
        given_fields: Any = self.fields
        fields = (given_fields,) if isinstance(given_fields, str) else tuple(given_fields)
        object.__setattr__(self, "fields", fields)
        if not fields:
            raise ConfigurationError(f"{type(self).__name__} needs at least one key field")
        if not all(isinstance(name, str) and name for name in fields):
            raise ConfigurationError(f"{type(self).__name__}: the key fields must be field names, got {fields!r}")
        if len(set(fields)) != len(fields):
            raise ConfigurationError(f"{type(self).__name__}: a key field is named twice in {fields!r}")

    @staticmethod
    def check_partition_name(owner: str, name: Any) -> None:
        """Checks a partition's name - it ends the name of the partition's table.

        Args:
            owner: The class the name is given to, for the message.
            name: The name.

        Raises:
            ConfigurationError: The name isn't a non-empty string of letters, digits and underscores.
        """
        if not isinstance(name, str) or not re.fullmatch(PARTITION_NAME_PATTERN, name):
            raise ConfigurationError(f"{owner}: a partition name is letters, digits and underscores, got {name!r}")

    @staticmethod
    def get_partition_table_name(table_name: str, partition_name: str) -> str:
        """The table of a partition: ``<table>_<partition name>``, within the identifier length limit.

        Args:
            table_name: The partitioned table.
            partition_name: The partition's name.

        Returns:
            The partition's table name.
        """
        return Identifiers.get_within_limit(f"{table_name}_{partition_name}")

    def get_partitions(self) -> dict[str, Any]:
        """The partitions the migrations add and remove one at a time, by name - none for a
        partitioning whose partitions follow from its own settings.

        Returns:
            The partitions.
        """
        return {}

    def with_partitions(self, partitions: Mapping[str, Any]) -> Self:
        """The partitioning with other partitions added or removed one at a time.

        Args:
            partitions: The partitions, by name (``get_partitions()``).

        Returns:
            The partitioning.
        """
        return self

    @abc.abstractmethod
    def get_partition_names(self) -> list[str]:
        """The names of every partition of the table.

        Returns:
            The names.
        """

    @abc.abstractmethod
    def get_partition_bound_sqls(self, render_value: BoundValueRenderer) -> dict[str, str]:
        """Each partition's bound as ``CREATE TABLE ... PARTITION OF`` takes it.

        Args:
            render_value: Renders a bound value of the key column at a position.

        Returns:
            ``FOR VALUES ...`` or ``DEFAULT``, by partition name.
        """

    def get_key_fields(self, model: type[Model]) -> list[Field[Any]]:
        """The fields holding the key columns, in order - a foreign key stands for the fields of
        its key columns.

        Args:
            model: The partitioned model.

        Returns:
            The fields.

        Raises:
            ConfigurationError: A key field doesn't exist, or has no column of its own.
        """
        meta = model._meta
        key_fields: list[Field[Any]] = []
        for name in self.fields:
            field_object = meta.fields_map.get(name)
            if field_object is None:
                raise ConfigurationError(f"{model.__name__}: the partition key field {name!r} doesn't exist")
            if isinstance(field_object, ForeignKeyFieldInstance):
                key_field_names = field_object.source_fields
                key_fields.extend(meta.fields_map[key_field_name] for key_field_name in key_field_names)
            elif name in meta.fields_db_projection:
                key_fields.append(field_object)
            else:
                raise ConfigurationError(
                    f"{model.__name__}: the partition key field {name!r} has no column of its own - name a field "
                    "with a column or a foreign key"
                )
        return key_fields

    def get_column_names(self, model: type[Model]) -> list[str]:
        """The key columns, in order.

        Args:
            model: The partitioned model.

        Returns:
            The column names.
        """
        return [field.source_field or field.model_field_name for field in self.get_key_fields(model)]

    def get_partition_by_sql(self, model: type[Model], quote: Callable[[str], str]) -> str:
        """The ``PARTITION BY`` clause of the partitioned table.

        Args:
            model: The partitioned model.
            quote: Quotes an identifier.

        Returns:
            The clause.
        """
        columns = ", ".join(quote(column) for column in self.get_column_names(model))
        return f"PARTITION BY {self.strategy} ({columns})"

    def raise_if_unsupported(self, model: type[Model]) -> None:
        """Rejects a partitioning the model's table can't have: an unknown key field, a primary
        key, unique constraint, unique index or exclusion constraint without every key column -
        PostgreSQL enforces them per partition - and partitions that don't fit the key.

        Args:
            model: The partitioned model.

        Raises:
            ConfigurationError: The partitioning doesn't fit the model.
        """
        key_columns = set(self.get_column_names(model))
        meta = model._meta
        unique_column_sets: list[tuple[str, set[str]]] = []
        if meta.has_primary_key:
            unique_column_sets.append(
                ("the primary key", set(meta.get_column_names(meta.primary_key_attribute_names)))
            )
        for field_name, field_object in meta.fields_map.items():
            if field_object.unique and not field_object.pk and field_name in meta.fields_db_projection:
                unique_column_sets.append(
                    (f"the unique field {field_name!r}", {meta.fields_db_projection[field_name]})
                )
        for constraint in meta.constraints:
            if isinstance(constraint, UniqueConstraint):
                described = f"the unique constraint {constraint.name or constraint.fields!r}"
                unique_column_sets.append((described, set(meta.get_column_names(constraint.fields))))
            elif isinstance(constraint, ExclusionConstraint):
                described = f"the exclusion constraint {constraint.name!r}"
                unique_column_sets.append((described, Partitioning.get_exclusion_columns(model, constraint)))
        for entry in meta.indexes:
            if isinstance(entry, Index) and entry.unique:
                described = f"the unique index {entry.name or entry.fields!r}"
                unique_column_sets.append((described, set(meta.get_column_names(entry.field_names))))
        for described, column_set in unique_column_sets:
            if not key_columns <= column_set:
                missing = ", ".join(sorted(key_columns - column_set))
                raise ConfigurationError(
                    f"{model.__name__}: {described} doesn't include the partition key column(s) {missing} - "
                    "PostgreSQL enforces it in each partition, so it must include every key column"
                )
        self.raise_if_partitions_unsupported(model, len(key_columns))

    @staticmethod
    def get_exclusion_columns(model: type[Model], constraint: ExclusionConstraint) -> set[str]:
        """The plain columns an exclusion constraint compares with ``=`` - PostgreSQL enforces one
        per partition only when it compares every key column that way.

        Args:
            model: The model.
            constraint: The constraint.

        Returns:
            The column names - an expression element names no column.
        """
        field_names = [
            element for element, operator in constraint.expressions if isinstance(element, str) and operator == "="
        ]
        return set(model._meta.get_column_names(field_names))

    def raise_if_partitions_unsupported(self, model: type[Model], key_column_count: int) -> None:
        """Rejects partitions that don't fit the key - checked by each strategy.

        Args:
            model: The partitioned model.
            key_column_count: The number of key columns.

        Raises:
            ConfigurationError: A partition doesn't fit.
        """

    @staticmethod
    def raise_if_names_repeat(owner: str, names: Iterable[str]) -> None:
        """Rejects partitions sharing a name - and so a table.

        Args:
            owner: The partitioning, for the message.
            names: The partition names.

        Raises:
            ConfigurationError: A name repeats.
        """
        seen: set[str] = set()
        for name in names:
            if name in seen:
                raise ConfigurationError(f"{owner}: the partition name {name!r} is used twice")
            seen.add(name)

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        """How a migration file rebuilds the partitioning: its class path and arguments.

        Returns:
            The path, positional arguments and keyword arguments.
        """
        kwargs: dict[str, Any] = {}
        for option in dataclasses.fields(self):
            value = getattr(self, option.name)
            default = option.default if option.default is not dataclasses.MISSING else dataclasses.MISSING
            if value == default:
                continue
            kwargs[option.name] = list(value) if isinstance(value, tuple) else value
        return ClassPath.get(type(self)), [], kwargs
