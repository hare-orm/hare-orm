from __future__ import annotations

import dataclasses
from typing import Any

from hare.instrumentation.enums import RowOperation
from tests.contracts.writes.enums import WriteRole


@dataclasses.dataclass(frozen=True)
class ExpectedChange:
    """The rows of one model a write path changes.

    Attributes:
        role: The model.
        operation: What is done to the rows.
        pks: The keys of the rows - what a captured change holds.
        unnamed: Whether a ``RowsChanged`` event of the rows names none (a cascade or a filter reaches rows it
            doesn't list).
    """

    role: WriteRole
    operation: RowOperation
    pks: tuple[Any, ...] = ()
    unnamed: bool = False

    @staticmethod
    def insert(role: WriteRole, *pks: Any, unnamed: bool = False) -> ExpectedChange:
        """Rows inserted.

        Args:
            role: The model.
            pks: The keys of the rows.
            unnamed: Whether an event names none.

        Returns:
            The change.
        """
        return ExpectedChange(role, RowOperation.INSERT, pks, unnamed)

    @staticmethod
    def update(role: WriteRole, *pks: Any, unnamed: bool = False) -> ExpectedChange:
        """Rows updated.

        Args:
            role: The model.
            pks: The keys of the rows.
            unnamed: Whether an event names none.

        Returns:
            The change.
        """
        return ExpectedChange(role, RowOperation.UPDATE, pks, unnamed)

    @staticmethod
    def delete(role: WriteRole, *pks: Any, unnamed: bool = False) -> ExpectedChange:
        """Rows deleted.

        Args:
            role: The model.
            pks: The keys of the rows.
            unnamed: Whether an event names none.

        Returns:
            The change.
        """
        return ExpectedChange(role, RowOperation.DELETE, pks, unnamed)

    def get_reported_pks(self) -> tuple[Any, ...] | None:
        """The keys a ``RowsChanged`` event names.

        Returns:
            The keys, None for none.
        """
        return None if self.unnamed else self.pks
