from __future__ import annotations

from typing import Any

from hare import fields
from hare.contrib.outbox import ChangeCapture, OutboxEvent
from hare.models import Model
from tests.contracts.constants import CONTRACT_APP
from tests.contracts.field_sample import FieldSample


class ContractModels:
    """The models of a field sample, made anew for each test: the row holding the sampled field, the model a
    relation targets and the outbox a captured row writes its changes to."""

    @staticmethod
    def make_target() -> type[Model]:
        """The target of the relation samples.

        Returns:
            The model ``contracts.ContractTarget``.
        """
        meta = type("Meta", (), {"app": CONTRACT_APP, "table": "contract_target"})
        return type(
            "ContractTarget",
            (Model,),
            {"__module__": __name__, "id": fields.IntField(primary_key=True, generated=False), "Meta": meta},
        )

    @staticmethod
    def make_outbox() -> type[OutboxEvent]:
        """The outbox of the captured rows.

        Returns:
            The model ``contracts.ContractOutboxEvent``.
        """
        meta = type("Meta", (OutboxEvent.Meta,), {"app": CONTRACT_APP, "table": "contract_outbox_event"})
        return type("ContractOutboxEvent", (OutboxEvent,), {"__module__": __name__, "Meta": meta})

    @staticmethod
    def make_row(sample: FieldSample, outbox: type[OutboxEvent] | None = None) -> type[Model]:
        """The model holding a sample's fields.

        Args:
            sample: The sample.
            outbox: The outbox its changes are captured into; None captures none.

        Returns:
            The model ``contracts.ContractRow``.
        """
        meta_attributes: dict[str, Any] = {"app": CONTRACT_APP, "table": "contract_row"}
        if outbox is not None:
            meta_attributes["change_capture"] = ChangeCapture(outbox)
        attributes: dict[str, Any] = {
            "__module__": __name__,
            "id": fields.IntField(primary_key=True, generated=False),
            **sample.make_fields(),
            "Meta": type("Meta", (), meta_attributes),
        }
        return type("ContractRow", (Model,), attributes)

    @staticmethod
    def make_models(sample: FieldSample, outbox: type[OutboxEvent] | None = None) -> list[type[Model]]:
        """Every model of a sample.

        Args:
            sample: The sample.
            outbox: The outbox its row's changes are captured into, among the models; None captures none.

        Returns:
            The outbox, the target of a relation and the row's model.
        """
        return [
            *([outbox] if outbox is not None else []),
            *([ContractModels.make_target()] if sample.related else []),
            ContractModels.make_row(sample, outbox),
        ]
