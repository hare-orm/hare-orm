"""OutboxJsonEncoding: the values besides JSON's own an outbox event holds, and TypeError for any
other - a ValidationError at enqueue(), never a quiet str()."""

import datetime
import decimal
import enum
import ipaddress
import json
import uuid

import pytest

from hare.contrib.outbox import OutboxJsonEncoding
from hare.exceptions import ValidationError
from tests.contrib.outbox.models import DemoOutboxEvent


class Color(enum.Enum):
    RED = "red"


def test_the_values_besides_jsons_own():
    value = {
        "decimal": decimal.Decimal("12.50"),
        "uuid": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "datetime": datetime.datetime(2026, 10, 6, 12, 0, tzinfo=datetime.UTC),
        "date": datetime.date(2026, 10, 6),
        "time": datetime.time(12, 30),
        "timedelta": datetime.timedelta(minutes=2),
        "address": ipaddress.ip_address("10.0.0.1"),
        "network": ipaddress.ip_network("10.0.0.0/8"),
        "bytes": b"\x00\x01",
        "enum": Color.RED,
        "plain": [1, "a", None, True, 1.5],
    }
    assert json.loads(OutboxJsonEncoding.dumps(value)) == {
        "decimal": "12.50",
        "uuid": "12345678-1234-5678-1234-567812345678",
        "datetime": "2026-10-06T12:00:00+00:00",
        "date": "2026-10-06",
        "time": "12:30:00",
        "timedelta": 120.0,
        "address": "10.0.0.1",
        "network": "10.0.0.0/8",
        "bytes": "AAE=",
        "enum": "red",
        "plain": [1, "a", None, True, 1.5],
    }


def test_any_other_value_raises():
    with pytest.raises(TypeError, match="set"):
        OutboxJsonEncoding.dumps({"value": {1, 2}})


@pytest.mark.asyncio
async def test_enqueue_writes_and_refuses_through_it(db_outbox):
    event = await DemoOutboxEvent.enqueue("widget.created", {"total": decimal.Decimal("1.10")})
    assert (await DemoOutboxEvent.objects.get(id=event.id)).payload == {"total": "1.10"}

    with pytest.raises(ValidationError):
        await DemoOutboxEvent.enqueue("widget.created", {"value": object()})
    with pytest.raises(ValidationError):
        await DemoOutboxEvent.enqueue("widget.created", {}, headers={"value": object()})
    assert await DemoOutboxEvent.objects.all().count() == 1
