from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, ClassVar, Self

from hare.ddl.table_options import TableOptions
from hare.dialects.enums import DialectName
from hare.dialects.postgresql.constants import POSTGRESQL_STORAGE_PARAMETER_NAME_PATTERN
from hare.dialects.postgresql.partitioning.partitioning import Partitioning
from hare.exceptions import ConfigurationError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.features import Features
    from hare.models import Model


@dataclasses.dataclass(frozen=True)
class PostgresqlTableOptions(TableOptions):
    """How PostgreSQL stores a model's table.

    Attributes:
        tablespace: The tablespace the table is stored in, None for the database's default.
        unlogged: Skip the write-ahead log - much faster writes, but the table is emptied after a
            crash and isn't replicated.
        storage_parameters: The table's storage parameters (``WITH (...)``), such as
            ``{"fillfactor": 70}`` or ``{"autovacuum_vacuum_scale_factor": 0.05}`` - a name maps
            to a number, a boolean or a string. A partitioned table takes none itself - they are
            set on each of its partitions.
        partitioning: How the table is split into partitions (``HashPartitioning``,
            ``ListPartitioning``, ``RangePartitioning``), None for a plain table.
    """

    dialect_name: ClassVar[str] = DialectName.POSTGRESQL

    tablespace: str | None = None
    unlogged: bool = False
    storage_parameters: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    partitioning: Partitioning | None = None

    def __post_init__(self) -> None:
        """Checks the options and freezes the storage parameters in name order.

        Raises:
            ConfigurationError: A storage parameter's name isn't an identifier, its value isn't a
                number, a boolean or a string, or the partitioning isn't a ``Partitioning``.
        """
        if self.partitioning is not None and not isinstance(self.partitioning, Partitioning):
            raise ConfigurationError(
                f"PostgresqlTableOptions: partitioning must be a Partitioning, got {self.partitioning!r}"
            )
        for name, value in self.storage_parameters.items():
            if not isinstance(name, str) or not POSTGRESQL_STORAGE_PARAMETER_NAME_PATTERN.fullmatch(name):
                raise ConfigurationError(f"PostgresqlTableOptions: invalid storage parameter name {name!r}")
            if not isinstance(value, bool | int | float | str):
                raise ConfigurationError(
                    f"PostgresqlTableOptions: storage parameter {name!r} must be a number, a boolean or a "
                    f"string, got {value!r}"
                )
        object.__setattr__(self, "storage_parameters", dict(sorted(self.storage_parameters.items())))

    def __hash__(self) -> int:
        return hash((self.tablespace, self.unlogged, tuple(self.storage_parameters.items()), self.partitioning))

    def raise_if_unsupported(self, model: type[Model], features: Features) -> None:
        """Rejects a partitioned table that is unlogged - PostgreSQL has no unlogged partitioned
        tables - and a partitioning that doesn't fit the model.

        Args:
            model: The model.
            features: The features of the connection the table is created on.

        Raises:
            ConfigurationError: The options don't fit the model.
        """
        if self.partitioning is None:
            return
        if self.unlogged:
            raise ConfigurationError(
                f"{model.__name__}: PostgresqlTableOptions can't be both unlogged and partitioned - PostgreSQL "
                "has no unlogged partitioned tables"
            )
        self.partitioning.raise_if_unsupported(model)

    def get_partitions(self) -> dict[str, Any]:
        return self.partitioning.get_partitions() if self.partitioning is not None else {}

    def with_field_names(self, column_to_field_name: Mapping[str, str]) -> Self:
        if self.partitioning is None:
            return self
        field_names = tuple(dict.fromkeys(column_to_field_name.get(name, name) for name in self.partitioning.fields))
        return dataclasses.replace(self, partitioning=dataclasses.replace(self.partitioning, fields=field_names))

    def can_change_partitions_to(self, new_options: TableOptions) -> bool:
        return (
            isinstance(new_options, PostgresqlTableOptions)
            and self.partitioning is not None
            and new_options.partitioning is not None
            and bool(self.partitioning.get_partitions() or new_options.partitioning.get_partitions())
            and self.partitioning.with_partitions({}) == new_options.partitioning.with_partitions({})
        )

    def with_partitions(self, partitions: Mapping[str, Any]) -> Self:
        if self.partitioning is None:
            return self
        return dataclasses.replace(self, partitioning=self.partitioning.with_partitions(partitions))

    @staticmethod
    def get_storage_parameter_value_sql(value: bool | int | float | str) -> str:
        """Returns a storage parameter's value as written in ``WITH (...)``/``SET (...)``.

        Args:
            value: The value.

        Returns:
            The SQL.
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, str):
            return "'" + value.replace("'", "''") + "'"
        return repr(value)

    def get_storage_parameters_sql(self, storage_parameters: Mapping[str, Any]) -> str:
        """Returns storage parameters as the list inside ``WITH (...)``/``SET (...)``.

        Args:
            storage_parameters: The parameters.

        Returns:
            The comma-separated ``name = value`` list.
        """
        return ", ".join(
            f"{name} = {self.get_storage_parameter_value_sql(value)}" for name, value in storage_parameters.items()
        )

    def get_create_prefix_sql(self) -> str:
        return "UNLOGGED " if self.unlogged else ""

    def get_create_suffix_sql(self, model: type[Model], quote: Callable[[str], str]) -> str:
        if self.partitioning is not None:
            # A partitioned table holds no rows - its storage parameters go to its partitions.
            sql = f" {self.partitioning.get_partition_by_sql(model, quote)}"
            return sql + (f" TABLESPACE {quote(self.tablespace)}" if self.tablespace else "")
        return self.get_storage_sql(quote)

    def get_storage_sql(self, quote: Callable[[str], str]) -> str:
        """The storage parameters and tablespace of a table holding rows - a plain table, or each
        partition of a partitioned one.

        Args:
            quote: Quotes an identifier.

        Returns:
            ``WITH (...)`` and ``TABLESPACE ...`` with a leading space, or an empty string.
        """
        sql = ""
        if self.storage_parameters:
            sql += f" WITH ({self.get_storage_parameters_sql(self.storage_parameters)})"
        if self.tablespace:
            sql += f" TABLESPACE {quote(self.tablespace)}"
        return sql
