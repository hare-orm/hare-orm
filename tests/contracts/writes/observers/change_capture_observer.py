from __future__ import annotations

from typing import Any, ClassVar

from hare.contrib.outbox import ChangeCapture, ChangeExtension, ChangePayload, OutboxEvent
from hare.instrumentation.enums import RowOperation
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP, WRITE_EXTENSION_HEADERS, WRITE_EXTENSION_PAYLOAD
from tests.contracts.writes.enums import WriteOutcome, WriteRole
from tests.contracts.writes.write_observer import WriteObserver
from tests.contracts.writes.write_path import WritePath
from tests.contracts.writes.write_paths import WRITE_PATHS
from tests.contracts.writes.write_scenario import WriteScenario


class ChangeCaptureObserver(WriteObserver):
    """A change capture of the models: every path writes an event for each row it changes, in the write's
    transaction - the operation, the key, the row before and after, the fields changed, and what ``extend``
    adds; a path that changes no row writes none."""

    name: ClassVar[str] = "change_capture"
    required_features: ClassVar[tuple[str, ...]] = ("supports_unique_constraints",)
    # A merge's events read its rows back, as they were too.
    path_required_features: ClassVar[dict[str, tuple[str, ...]]] = {
        "QuerySet.merge": ("supports_merge_returning", "supports_returning_old_new")
    }
    outcomes: ClassVar[dict[str, WriteOutcome]] = {path.name: WriteOutcome.OBSERVED for path in WRITE_PATHS}

    def __init__(self) -> None:
        meta = type("Meta", (OutboxEvent.Meta,), {"app": CONTRACT_APP, "table": "write_outbox_event"})
        self.outbox: type[OutboxEvent] = type(
            "WriteOutboxEvent", (OutboxEvent,), {"__module__": __name__, "Meta": meta}
        )

    def get_meta_options(self, role: WriteRole) -> dict[str, Any]:
        if role is WriteRole.TAG:
            return {}
        payload = ChangePayload.KEYS if role is WriteRole.LINK else ChangePayload.BEFORE_AND_AFTER
        return {
            "change_capture": ChangeCapture(
                self.outbox,
                payload=payload,
                extend=lambda change: ChangeExtension(
                    payload=WRITE_EXTENSION_PAYLOAD, headers=WRITE_EXTENSION_HEADERS
                ),
            ),
            "track_dirty_fields": role is not WriteRole.LINK,
        }

    def get_extra_models(self) -> tuple[type[Model], ...]:
        return (self.outbox,)

    async def check(self, scenario: WriteScenario, path: WritePath, outcome: WriteOutcome) -> None:
        await self.outbox.objects.all().delete()
        await path.run(scenario, 5)

        events = await self.outbox.objects.order_by("sequence")
        captured = sorted(self.get_captured_rows(events))
        expected = sorted(
            (change.role.value, change.operation.value, pk) for change in path.captured for pk in change.pks
        )
        assert captured == expected
        for event in events:
            self.check_event(event)

    @staticmethod
    def get_captured_rows(events: list[OutboxEvent]) -> list[tuple[str, str, Any]]:
        """The rows the events are of.

        Args:
            events: The outbox's events.

        Returns:
            Each event's model role, operation and key.
        """
        operations = {"inserted": RowOperation.INSERT, "updated": RowOperation.UPDATE, "deleted": RowOperation.DELETE}
        return [
            (
                event.payload["model"].removeprefix(f"{CONTRACT_APP}.Write").lower(),
                operations[event.payload["operation"]].value,
                event.payload["pk"],
            )
            for event in events
        ]

    @staticmethod
    def check_event(event: OutboxEvent) -> None:
        """Checks what an event holds: the row before and after as the operation says, the fields changed
        as the two differ, and what ``extend`` added.

        Args:
            event: The event.
        """
        payload = event.payload
        assert payload["source"] == WRITE_EXTENSION_PAYLOAD["source"]
        assert event.headers == WRITE_EXTENSION_HEADERS
        if payload["model"].endswith("Link"):
            return
        before, after = payload["before"], payload["after"]
        if payload["operation"] == "inserted":
            assert before is None and payload["changed"] is None
            assert after["id"] == payload["pk"]
        elif payload["operation"] == "deleted":
            assert after is None and before["id"] == payload["pk"]
        else:
            differing = sorted(key for key in after if after[key] != before[key])
            assert sorted(payload["changed"]) == differing
            assert differing
