"""The deletion graph's answers for a model on a cycle of CASCADE relations - each model's answer is its
own, whichever model of the cycle was asked about first."""

from typing import Any

import pytest

from hare import Hare, fields
from hare.contrib.outbox import ChangeCapture
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.models import Model
from hare.models.deletion.cascade.deletion_graph import DeletionGraph
from tests.contrib.outbox.capture_models import CaptureOutboxEvent


def register_cycle_models(end_meta: type | None, end_delete: Any = None) -> dict[str, type[Model]]:
    """X cascades to Y, Y and Z cascade to each other, and Y cascades to W - the end of the walk.

    Args:
        end_meta: The ``Meta`` of W, None for none.
        end_delete: W's ``delete()``, None to keep ``Model.delete()``.

    Returns:
        The models by name.
    """

    class CycleTop(Model):
        id = fields.IntField(primary_key=True)

    class CycleMiddle(Model):
        id = fields.IntField(primary_key=True)
        top = fields.ForeignKeyField("cycle.CycleTop", related_name="middles", on_delete=fields.CASCADE)
        partner = fields.ForeignKeyField(
            "cycle.CyclePartner", related_name="middles", on_delete=fields.CASCADE, null=True
        )

    # The relation from Y to Z is walked before the one to W: their names sort so.
    class CyclePartner(Model):
        id = fields.IntField(primary_key=True)
        middle = fields.ForeignKeyField("cycle.CycleMiddle", related_name="a_partners", on_delete=fields.CASCADE)

    end_attributes: dict[str, Any] = {
        "__module__": __name__,
        "id": fields.IntField(primary_key=True),
        "middle": fields.ForeignKeyField("cycle.CycleMiddle", related_name="b_ends", on_delete=fields.CASCADE),
    }
    if end_meta is not None:
        end_attributes["Meta"] = end_meta
    if end_delete is not None:
        end_attributes["delete"] = end_delete
    cycle_end = type("CycleEnd", (Model,), end_attributes)
    models = [CycleTop, CycleMiddle, CyclePartner, cycle_end]
    Hare.register_live_models(models, app_label="cycle")
    return {model.__name__: model for model in models}


async def init_hare() -> None:
    await Hare.init(
        HareConfig.from_db_url(
            "sqlite+aiosqlite://:memory:",
            {"models": ["tests.shared_models.testmodels", "tests.contrib.outbox.capture_models"]},
        )
    )


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_model_on_a_cycle_reaches_an_overridden_delete_asked_after_another():
    async def delete(self: Model, *args: Any, **kwargs: Any) -> None:
        await Model.delete(self, *args, **kwargs)

    async with HareContext():
        await init_hare()
        models = register_cycle_models(None, delete)

        assert DeletionGraph.reaches_overridden_deletes(models["CycleTop"]) is True
        assert DeletionGraph.reaches_overridden_deletes(models["CyclePartner"]) is True
        assert DeletionGraph.needs_python_cascade(models["CyclePartner"]) is True


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_model_on_a_cycle_reaches_a_captured_model_asked_after_another():
    class Meta:
        change_capture = ChangeCapture(CaptureOutboxEvent)

    async with HareContext():
        await init_hare()
        models = register_cycle_models(Meta)

        assert DeletionGraph.reaches_captured_models(models["CycleTop"]) is True
        assert DeletionGraph.reaches_captured_models(models["CyclePartner"]) is True


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_cycle_reaching_nothing_answers_false_for_every_model():
    async with HareContext():
        await init_hare()
        models = register_cycle_models(None)

        for model in models.values():
            assert DeletionGraph.reaches_overridden_deletes(model) is False
            assert DeletionGraph.reaches_captured_models(model) is False
