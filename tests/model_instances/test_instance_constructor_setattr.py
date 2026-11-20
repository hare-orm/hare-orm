"""A model overriding ``__setattr__`` gets every constructor keyword and default through it - the native
constructor sets a plain column past ``Model.__setattr__`` only for a model keeping it."""

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.models import Model

pytestmark = pytest.mark.sqlite_only


class Recorded(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=30)
    amount = fields.IntField(default=4)

    def __setattr__(self, name, value):
        self.__dict__.setdefault("seen", []).append(name)
        super().__setattr__(name, value)


class Plain(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=30)


@pytest.mark.asyncio
async def test_an_overridden_setattr_sees_every_keyword_and_default():
    async with hare_test_context([__name__], db_url="sqlite+aiosqlite://:memory:"):
        obj = Recorded(label="x")
        assert {"label", "amount"} <= set(obj.seen)
        assert (obj.label, obj.amount) == ("x", 4)
        plain = Plain(label="y")
        assert "seen" not in plain.__dict__
        assert plain.label == "y"
