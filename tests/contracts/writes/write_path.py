from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import pytest

from hare.instrumentation.enums import RowOperation
from tests.contracts.writes.enums import WriteRole
from tests.contracts.writes.expected_change import ExpectedChange

if TYPE_CHECKING:
    from tests.contracts.writes.write_scenario import WriteScenario


@dataclasses.dataclass(frozen=True)
class WritePath:
    """One way the ORM writes rows - a method of a model, a queryset or a relation, with the arguments that
    make it do one thing.

    Attributes:
        name: The path's name - the test's id.
        run: Runs the path on a scenario, giving the value it writes to the ``amount`` of its rows.
        reported: What a ``RowsChanged`` observer gets of it, in sorted order.
        captured: The rows a change capture holds of it - an event of each row, ``unnamed`` unused.
        writes_amount: Whether the given amount reaches the field of a row the path writes - a validator of
            the field checks it.
        prepare: Runs before the observers watch - the state the path starts from.
        required_features: The features of the dialect the path needs.
    """

    name: str
    run: Callable[[WriteScenario, int], Awaitable[None]]
    reported: tuple[ExpectedChange, ...]
    captured: tuple[ExpectedChange, ...]
    writes_amount: bool = False
    prepare: Callable[[WriteScenario], Awaitable[None]] | None = None
    required_features: tuple[str, ...] = ()

    def get_changed_rows(self, *roles: WriteRole) -> list[tuple[WriteRole, RowOperation, Any]]:
        """The rows the path changes, one entry for each.

        Args:
            roles: The models whose rows are wanted; every model's when none.

        Returns:
            The model, the operation and the key of each row.
        """
        return [
            (change.role, change.operation, pk)
            for change in self.captured
            if not roles or change.role in roles
            for pk in change.pks
        ]

    def get_parameter(self) -> Any:
        """The path as a test's parameter.

        Returns:
            The parameter, named after the path.
        """
        return pytest.param(self, id=self.name)
