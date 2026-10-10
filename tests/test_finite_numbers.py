"""A number a setting or an argument takes is finite only when a float can hold it - an int beyond the
float range is refused like NaN and the infinities, with the setting's own error, not OverflowError."""

import pytest

from hare.contrib.notify.notification_listener import NotificationListener
from hare.contrib.outbox.relay import OutboxRelay
from hare.contrib.repeated_queries.repeated_query_detector import RepeatedQueryDetector
from hare.exceptions import ConfigurationError, QueryError, ValidationError
from hare.gis.geometries.point import Point
from hare.numbers.finite_numbers import FiniteNumbers
from tests.contrib.outbox.models import DemoOutboxEvent
from tests.testmodels import IntFields

HUGE_INT = 10**400


async def deliver_nothing(event):
    return None


def ignore_notification(payload):
    return None


@pytest.mark.parametrize("value", [0, -3, 2.5, 10**300])
def test_a_finite_number(value):
    assert FiniteNumbers.is_finite_number(value) is True


@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -float("inf"), HUGE_INT, -HUGE_INT, "1", None]
)
def test_not_a_finite_number(value):
    assert FiniteNumbers.is_finite_number(value) is False


@pytest.mark.asyncio
async def test_settings_refuse_an_int_beyond_the_float_range(db):
    with pytest.raises(QueryError, match="takes a percent"):
        IntFields.objects.sample(HUGE_INT)
    with pytest.raises(ValidationError, match="A coordinate is a finite number"):
        Point(HUGE_INT, 1, srid=4326)
    with pytest.raises(ConfigurationError, match="poll_interval_seconds"):
        OutboxRelay(DemoOutboxEvent, deliver_nothing, poll_interval_seconds=HUGE_INT)
    with pytest.raises(ConfigurationError, match="window_seconds"):
        RepeatedQueryDetector(window_seconds=HUGE_INT)
    with pytest.raises(ConfigurationError, match="backoff"):
        NotificationListener("models", "channel", ignore_notification, backoff=HUGE_INT)
