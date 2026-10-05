from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from hare.sql.constants import HIDDEN_PARAMETER_TEXT


class QueryParameters(list[Any]):
    """The parameters of a statement some of which hold a ``sensitive=True`` field's value - a
    plain list to the driver, shown with ``<hidden>`` in their places wherever the list is written
    out (``repr()``): the DEBUG log of a statement, the slow query log, ``QueryExecuted``. A
    statement without such a value binds a plain list.

    Attributes:
        hidden_indexes: The positions of the values never shown.
    """

    __slots__ = ("hidden_indexes",)

    def __init__(self, values: Iterable[Any] = (), hidden_indexes: Iterable[int] = ()) -> None:
        super().__init__(values)
        self.hidden_indexes: set[int] = set(hidden_indexes)

    def __repr__(self) -> str:
        return repr(self.get_shown())

    def get_shown(self) -> list[Any]:
        """The parameters as they may be shown - each hidden one replaced by ``<hidden>``.

        Returns:
            A plain list.
        """
        shown = list(self)
        for index in self.hidden_indexes:
            if index < len(shown):
                shown[index] = HIDDEN_PARAMETER_TEXT
        return shown

    def get_hidden_values(self) -> list[Any]:
        """The values never shown.

        Returns:
            The values, in no particular order.
        """
        return [self[index] for index in self.hidden_indexes if index < len(self)]

    def copy(self) -> QueryParameters:
        return QueryParameters(self, self.hidden_indexes)
