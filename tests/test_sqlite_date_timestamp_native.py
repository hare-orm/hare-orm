"""The native day-start and local-now functions give what the Python ones give - the same
value, or the same exception - for every argument."""

import datetime
import random
from collections.abc import Callable
from typing import Any

import pytest

from hare.dialects.sqlite.functions.comparison.sqlite_date_timestamp import SqliteDateTimestamp
from hare.dialects.sqlite.functions.datetime.sqlite_local_now import SqliteLocalNow
from hare.dialects.sqlite.functions.native_functions import SqliteNativeFunctions
from hare.utils import Timezone

pytestmark = pytest.mark.skipif(SqliteNativeFunctions.module is None, reason="rust.native isn't built")

ZONES = [
    None, "UTC", "Europe/Berlin", "America/Sao_Paulo", "America/Havana", "Asia/Tehran", "Pacific/Apia",
    "Asia/Kolkata", "Europe/Moscow", "Australia/Lord_Howe", "America/St_Johns", "US/central",
]  # fmt: skip


def get_outcome(function: Callable[..., Any], *arguments: Any) -> tuple[str, Any]:
    try:
        result = function(*arguments)
    except Exception as error:
        return "error", type(error)
    return "value", (type(result), result)


def refuse(*arguments: Any) -> Any:
    raise AssertionError(f"the Python function was called with {arguments}")


def test_date_timestamp() -> None:
    get_start_text = SqliteNativeFunctions.module.DateTimestamp(
        Timezone.parse, SqliteDateTimestamp.get_start_text
    ).get_start_text
    generator = random.Random(9)
    days = [datetime.date(1, 1, 1) + datetime.timedelta(days=generator.randint(0, 3_652_058)) for _ in range(150)]
    days += [datetime.date(1970, 1, 1) + datetime.timedelta(days=generator.randint(0, 25_000)) for _ in range(300)]
    texts = [day.isoformat() for day in days]
    texts += [f"{text} 10:30:00" for text in texts[:50]] + [f"{text}T23:59:59.5+05:00" for text in texts[50:100]]
    texts += [
        "0001-01-01", "9999-12-31", "2016-10-16", "2011-12-30", "2020-03-08", " 2020-01-02", "20200102",
        "2020-02-30", "0000-01-01", "2020-1-02", "2020-01-0", "",
    ]  # fmt: skip
    for text in [*texts, None, 5, b"2020-01-02"]:
        for zone_name in ZONES:
            native_outcome = get_outcome(get_start_text, text, zone_name)
            assert native_outcome == get_outcome(SqliteDateTimestamp.get_start_text, text, zone_name), (
                text,
                zone_name,
            )
    for zone_name in ["Nowhere/Zone", 5]:
        native_outcome = get_outcome(get_start_text, "2020-01-02", zone_name)
        assert native_outcome == get_outcome(SqliteDateTimestamp.get_start_text, "2020-01-02", zone_name)
    native_only = SqliteNativeFunctions.module.DateTimestamp(Timezone.parse, refuse).get_start_text
    assert native_only("2020-01-02 10:00:00", None) == "2020-01-02 00:00:00"
    assert native_only("2016-10-16", "America/Sao_Paulo") == "2016-10-16 03:00:00+00:00"


def test_local_now() -> None:
    get_local_now_text = SqliteNativeFunctions.module.LocalNow(Timezone.parse, refuse).get_local_now_text
    for zone_name in [zone for zone in ZONES if zone is not None]:
        for value_type in ["date", "time"]:
            native_text = get_local_now_text(zone_name, value_type)
            python_text = SqliteLocalNow.get_local_now_text(zone_name, value_type)
            if value_type == "date":
                assert native_text == python_text, zone_name
                continue
            native_time = datetime.time.fromisoformat(native_text)
            python_time = datetime.time.fromisoformat(python_text)
            assert native_time.utcoffset() == python_time.utcoffset(), zone_name
            today = datetime.date(2000, 1, 1)
            gap = datetime.datetime.combine(today, python_time) - datetime.datetime.combine(today, native_time)
            assert abs(gap) < datetime.timedelta(seconds=1), (zone_name, native_text, python_text)
    fallback_outcome = get_outcome(
        SqliteNativeFunctions.module.LocalNow(Timezone.parse, refuse).get_local_now_text, 5, "date"
    )
    assert fallback_outcome[0] == "error"
