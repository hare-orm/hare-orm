import pytest

from hare import fields
from hare.exceptions import ConfigurationError


def test_datetimefield_auto_now_only():
    """Test DatetimeField with only auto_now=True."""
    field = fields.DatetimeField(auto_now=True)
    assert field.auto_now is True
    assert field.auto_now_add is False

    assert field.deconstruct()[2] == {"auto_now": True}


def test_datetimefield_auto_now_add_only():
    """Test DatetimeField with only auto_now_add=True."""
    field = fields.DatetimeField(auto_now_add=True)
    assert field.auto_now is False
    assert field.auto_now_add is True

    assert field.deconstruct()[2] == {"auto_now_add": True}


def test_datetimefield_both_flags_raises():
    """Test DatetimeField raises when both auto_now and auto_now_add are True."""
    with pytest.raises(ConfigurationError, match="You can choose only 'auto_now' or 'auto_now_add'"):
        fields.DatetimeField(auto_now=True, auto_now_add=True)


def test_datetimefield_neither_flag():
    """Test DatetimeField with neither flag set."""
    field = fields.DatetimeField()
    assert field.auto_now is False
    assert field.auto_now_add is False

    assert field.deconstruct()[2] == {}


def test_datetimefield_constraints_auto_now():
    """Test that auto_now sets readOnly constraint."""
    field = fields.DatetimeField(auto_now=True)
    constraints = field.constraints
    assert constraints.get("readOnly") is True


def test_datetimefield_constraints_auto_now_add():
    """Test that auto_now_add sets readOnly constraint."""
    field = fields.DatetimeField(auto_now_add=True)
    constraints = field.constraints
    assert constraints.get("readOnly") is True


def test_datetimefield_constraints_neither():
    """Test that neither flag sets no readOnly constraint."""
    field = fields.DatetimeField()
    constraints = field.constraints
    assert "readOnly" not in constraints


def test_aware_infinity_reads_back_as_the_naive_infinity_with_use_tz_false():
    """The Rust driver decodes TIMESTAMPTZ -infinity/infinity as an aware datetime.min/max,
    asyncpg as a naive one - both are the naive infinity under use_timezone=False, not a local time."""
    import datetime

    from tests.utils.timezone_context import override_timezone

    field = fields.DatetimeField()
    ordinary = datetime.datetime(2024, 5, 6, 7, 8, tzinfo=datetime.UTC)
    with override_timezone(use_timezone=False):
        assert field.from_db_value(datetime.datetime.min.replace(tzinfo=datetime.UTC)) == datetime.datetime.min
        assert field.from_db_value(datetime.datetime.max.replace(tzinfo=datetime.UTC)) == datetime.datetime.max
        assert field.from_db_value(datetime.datetime.min) == datetime.datetime.min
        assert field.from_db_value(ordinary) == ordinary.astimezone().replace(tzinfo=None)


def test_iso_datetime_parser_reads_every_form_the_iso8601_package_read():
    """Where ciso8601 isn't installed, datetime text is parsed by datetime.fromisoformat() - the
    iso8601 package, dozens of times slower, parsed every row before - and the forms only the
    iso8601 package reads still go to it."""
    from datetime import datetime as datetime_class, timedelta as timedelta_class, timezone as timezone_class

    from hare.fields.data.temporal import TemporalValues

    parse = TemporalValues.parse_iso_datetime
    assert parse("2026-01-01 12:30:45.123456") == datetime_class(2026, 1, 1, 12, 30, 45, 123456)
    assert parse("2026-01-01 00:00:00+00:00").tzinfo is timezone_class.utc
    assert parse("2026-01-01T12:30:45-05:30").utcoffset() == -timedelta_class(hours=5, minutes=30)
    assert parse("2026-01") == datetime_class(2026, 1, 1)
    assert parse("2026") == datetime_class(2026, 1, 1)
    with pytest.raises(ValueError):
        parse("not a date")
