from __future__ import annotations

from typing import Any, ClassVar

from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.observers.observers import Observers
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario


class RowsChangedObserver(WriteObserver):
    """An observer of ``RowsChanged``: every path reports the rows it changed once - the model, the operation
    and the keys it names - and a path that changes none reports nothing."""

    name: ClassVar[str] = "rows_changed"
    outcomes: ClassVar[dict[str, WriteOutcome]] = {path.name: WriteOutcome.OBSERVED for path in WRITE_PATHS}

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        events: list[RowsChanged] = []
        with Observers.observing(RowsChanged, events.append):
            await path.run(scenario, 5)
        roles = {scenario.models.get_model(role): role for role in WriteRole}
        reported = list(
            (roles[event.model].value, event.operation.value, tuple(event.pks) if event.pks is not None else None)
            for event in events
        )
        expected = list(
            (change.role.value, change.operation.value, change.get_reported_pks()) for change in path.reported
        )
        assert self.sort_key_list(reported) == self.sort_key_list(expected)

    @staticmethod
    def sort_key_list(items: list[tuple[Any, ...]]) -> list[tuple[Any, ...]]:
        """The items in an order that compares keys and the absence of keys.

        Args:
            items: The ``(role, operation, keys)`` items.

        Returns:
            The items, sorted.
        """
        return sorted(items, key=lambda item: (item[0], item[1], item[2] is None, item[2] or ()))
