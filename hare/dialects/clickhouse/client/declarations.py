from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True, slots=True)
class ClickhouseDriverErrors:
    """The exception classes of a ClickHouse driver, one class or several of each sort - read by
    the shared client only once a statement failed.

    Attributes:
        server: An error the server reported, its message naming the ClickHouse error code.
        connection: The server couldn't be reached or the connection broke.
        driver: Any other error of the driver.
    """

    server: type[Exception] | tuple[type[Exception], ...]
    connection: type[Exception] | tuple[type[Exception], ...]
    driver: type[Exception] | tuple[type[Exception], ...]


@dataclasses.dataclass(frozen=True, slots=True)
class ClickhouseValueSet:
    """The values of a long ``IN`` list bound as one parameter - sent as an external table by a read,
    written as a list of literals by any other statement.

    Attributes:
        column_types: The type of each value of a row - one for a list of single values.
        rows: The values - single values, or a tuple per row of several.
    """

    column_types: tuple[str, ...]
    rows: list[Any]


@dataclasses.dataclass(frozen=True, slots=True)
class ClickhouseExternalSet:
    """An external table a read sends beside its statement, named by the statement in place of a list.

    Attributes:
        name: The table's name.
        value_set: Its values.
    """

    name: str
    value_set: ClickhouseValueSet
