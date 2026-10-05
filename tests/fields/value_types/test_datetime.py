import pytest

from hare import fields
from hare.exceptions import ConfigurationError

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    "options",
    [
        pytest.param({"auto_now": True}, id="auto_now_only"),
        pytest.param({"auto_now_add": True}, id="auto_now_add_only"),
        pytest.param({}, id="neither_flag"),
    ],
)
@pytest.mark.parametrize(
    "field_class",
    [pytest.param(fields.DatetimeField, id="datetimefield"), pytest.param(fields.TimeField, id="timefield")],
)
def test_the_automatic_flags_of_a_field(field_class, options):
    field = field_class(**options)
    assert field.auto_now is options.get("auto_now", False)
    assert field.auto_now_add is options.get("auto_now_add", False)

    assert field.deconstruct()[2] == options


@pytest.mark.parametrize(
    "field_class",
    [
        pytest.param(fields.DatetimeField, id="datetimefield_both_flags_raises"),
        pytest.param(fields.TimeField, id="timefield_both_flags_raises"),
    ],
)
def test_both_automatic_flags_are_refused(field_class):
    with pytest.raises(ConfigurationError, match="You can choose only 'auto_now' or 'auto_now_add'"):
        field_class(auto_now=True, auto_now_add=True)


@pytest.mark.parametrize(
    ("options", "read_only"),
    [
        pytest.param({"auto_now": True}, True, id="datetimefield_constraints_auto_now"),
        pytest.param({"auto_now_add": True}, True, id="datetimefield_constraints_auto_now_add"),
        pytest.param({}, None, id="datetimefield_constraints_neither"),
    ],
)
def test_an_automatic_flag_sets_the_read_only_constraint(options, read_only):
    constraints = fields.DatetimeField(**options).constraints
    assert constraints.get("readOnly") is read_only


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

    from hare.fields.data.temporal.temporal_values import TemporalValues

    parse = TemporalValues.parse_iso_datetime
    assert parse("2026-01-01 12:30:45.123456") == datetime_class(2026, 1, 1, 12, 30, 45, 123456)
    assert parse("2026-01-01 00:00:00+00:00").tzinfo is timezone_class.utc
    assert parse("2026-01-01T12:30:45-05:30").utcoffset() == -timedelta_class(hours=5, minutes=30)
    assert parse("2026-01") == datetime_class(2026, 1, 1)
    assert parse("2026") == datetime_class(2026, 1, 1)
    with pytest.raises(ValueError):
        parse("not a date")
