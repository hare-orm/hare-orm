"""Whether a model's filter keys can reach a ``sensitive=True`` field - each model's answer is its own,
whichever model of a cycle of relations was asked about first."""

import pytest

from hare import Hare, fields
from hare.core.config import HareConfig
from hare.core.hare_context import HareContext
from hare.models import Model
from hare.query.filters.resolution.sensitive_filter_keys import SensitiveFilterKeys


@pytest.mark.database_independent
@pytest.mark.asyncio
async def test_a_model_reaches_a_sensitive_field_asked_after_another_model_of_its_cycle():
    async with HareContext():
        await Hare.init(
            HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.shared_models.testmodels"]})
        )

        class SensitiveTop(Model):
            id = fields.IntField(primary_key=True)

        class SensitiveMiddle(Model):
            id = fields.IntField(primary_key=True)
            top = fields.ForeignKeyField("sensitive_cycle.SensitiveTop", related_name="middles")

        class SensitivePartner(Model):
            id = fields.IntField(primary_key=True)
            middle = fields.ForeignKeyField("sensitive_cycle.SensitiveMiddle", related_name="a_partners")

        class SensitiveEnd(Model):
            id = fields.IntField(primary_key=True)
            secret = fields.CharField(max_length=50, sensitive=True)
            middle = fields.ForeignKeyField("sensitive_cycle.SensitiveMiddle", related_name="b_ends")

        Hare.register_live_models(
            [SensitiveTop, SensitiveMiddle, SensitivePartner, SensitiveEnd], app_label="sensitive_cycle"
        )

        assert SensitiveFilterKeys.reaches_sensitive_fields(SensitiveTop) is True
        assert SensitiveFilterKeys.reaches_sensitive_fields(SensitivePartner) is True
        assert SensitiveFilterKeys.reads_sensitive_values(SensitivePartner, "middle__b_ends__secret") is True
