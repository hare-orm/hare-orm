from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from hare.ddl.indexes.index import Index
from hare.dialects.clickhouse.indexes.constants import CLICKHOUSE_INDEX_GRANULARITY_RANGE
from hare.exceptions import ConfigurationError, UnSupportedError

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.dialects.base.client.database_client import DatabaseClient
    from hare.dialects.base.dialect import Dialect
    from hare.dialects.base.features import Features
    from hare.models import Model


class ClickhouseIndex(Index):
    """A data skipping index of ClickHouse - ``INDEX name (keys) TYPE type(arguments) GRANULARITY n``:
    for each ``granularity`` granules of the table it keeps what tells a query the granules hold no row
    it looks for. Created and dropped on a ClickHouse database only.

    Args:
        granularity: The granules of the table an entry of the index covers.

    Raises:
        ConfigurationError: ``granularity`` isn't an int in its range.
    """

    SUPPORTS_INCLUDE: ClassVar[bool] = False
    INTEGER_STORAGE_PARAMETERS: tuple[str, ...] = ("granularity",)

    def __init__(self, *args: Any, granularity: int = 1, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.granularity = self.get_validated_integer("granularity", granularity, CLICKHOUSE_INDEX_GRANULARITY_RANGE)

    def get_validated_integer(self, name: str, value: Any, value_range: tuple[int, int]) -> int:
        """Checks an integer argument before it's written into the DDL.

        Args:
            name: The argument's name.
            value: The given value.
            value_range: The inclusive minimum and maximum.

        Returns:
            The value.

        Raises:
            ConfigurationError: The value isn't an int in the range.
        """
        if type(value) is not int or not value_range[0] <= value <= value_range[1]:
            raise ConfigurationError(
                f"{type(self).__name__}({name}=...) takes an int from {value_range[0]} to {value_range[1]}, "
                f"got {value!r}"
            )
        return value

    def get_type_arguments(self) -> tuple[Any, ...]:
        """The arguments of the index's type - numbers, written into the DDL.

        Returns:
            The arguments, none by default.
        """
        return ()

    def get_index_type_sql(self) -> str:
        """The index's type with its arguments - ``set(100)``.

        Returns:
            The type.
        """
        arguments = self.get_type_arguments()
        if not arguments:
            return self.INDEX_TYPE
        return f"{self.INDEX_TYPE}({', '.join(repr(argument) for argument in arguments)})"

    def get_expression_dialect(self) -> Dialect:
        # The index exists on ClickHouse only - its expressions are ClickHouse's.
        # Local import: the dialect's modules import the indexes.
        from hare.dialects.clickhouse.constants import CLICKHOUSE_DIALECT

        return CLICKHOUSE_DIALECT

    def raise_if_unsupported(self, dialect: Dialect) -> None:
        """Rejects the index on another database.

        Raises:
            UnSupportedError: The dialect isn't ClickHouse's.
        """
        self.raise_if_other_dialect(dialect)
        super().raise_if_unsupported(dialect)

    def raise_if_not_droppable(self, features: Features, dialect: Dialect) -> None:
        """Rejects dropping the index on another database - it can't exist there.

        Raises:
            UnSupportedError: The dialect isn't ClickHouse's.
        """
        self.raise_if_other_dialect(dialect)

    def raise_if_other_dialect(self, dialect: Dialect) -> None:
        """Rejects the index on a database of another dialect, before any SQL.

        Args:
            dialect: The dialect of the database the DDL runs on.

        Raises:
            UnSupportedError: The dialect isn't ClickHouse's.
        """
        # Local import: the dialect's module imports its introspection, which imports the indexes.
        from hare.dialects.clickhouse.clickhouse_dialect import ClickhouseDialect

        if not isinstance(dialect, ClickhouseDialect):
            raise UnSupportedError(
                f"{type(self).__name__}(fields={list(self.get_declared_fields())!r}) is an index of ClickHouse - the "
                f"{dialect} dialect has none: declare an index of that database in the model, or change the migration"
            )

    def get_extra(self, model: type[Model], client: DatabaseClient) -> str:
        return f" GRANULARITY {self.granularity}{super().get_extra(model, client)}"

    def deconstruct(self) -> tuple[str, list[Any], dict[str, Any]]:
        path, args, kwargs = super().deconstruct()
        if self.granularity != 1:
            kwargs["granularity"] = self.granularity
        return path, args, kwargs
