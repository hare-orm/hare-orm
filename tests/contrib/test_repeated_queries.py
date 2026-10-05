"""Tests for `hare.contrib.repeated_queries` - the runtime N+1 detector, an observer of
`QueryExecuted`.

The detector's observer is a plain function: it runs in the task that issued the query, before the
query call returns, so nothing has to be waited for.
"""

import math
from datetime import datetime
from unittest.mock import patch

import pytest

from hare.contrib.repeated_queries import RepeatedQueryDetector
from hare.contrib.repeated_queries.constants import (
    REPEATED_QUERY_MAX_THRESHOLD,
    REPEATED_QUERY_MAX_WINDOW_SECONDS,
)
from hare.contrib.test import capture_queries
from hare.exceptions import ConfigurationError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.time import Timezone
from tests.testmodels import Author, Book, DatetimeFields
from tests.utils.timezone_context import override_timezone


@pytest.fixture(autouse=True)
def _reset_detector_counts():
    """The detectors' counters are a `ContextVar` - every test starts a unit of work of its own."""
    RepeatedQueryDetector.reset_counts()
    yield
    RepeatedQueryDetector.reset_counts()


@pytest.mark.asyncio
async def test_crossing_the_threshold_reports_once_with_the_right_count(db):
    author = await Author.objects.create(name="Douglas Adams")
    reports = []
    original_capture = RepeatedQueryDetector.capture_application_call_site

    with patch.object(RepeatedQueryDetector, "capture_application_call_site", wraps=original_capture) as spy:
        async with RepeatedQueryDetector(threshold=3, window_seconds=10.0, action=reports.append):
            for _ in range(5):
                await Book.objects.filter(author_id=author.id).count()

        # Only the call that CROSSES the threshold (the 3rd of 5 identical-shape queries) should
        # report - and capture a call site - not every call past it.
        assert len(reports) == 1
        assert spy.call_count == 1

    report = reports[0]
    assert report.count == 3
    assert report.threshold == 3
    assert "test_repeated_queries.py" in report.call_site
    assert "author_id" in report.sql_sample or "author" in report.sql_sample.lower()


@pytest.mark.asyncio
async def test_different_shapes_never_cross_the_threshold_individually(db):
    await Author.objects.create(name="Douglas Adams")
    reports = []
    async with RepeatedQueryDetector(threshold=3, window_seconds=10.0, action=reports.append):
        # Each of these filters a DIFFERENT field, so each renders different WHERE-clause SQL text -
        # a genuinely different shape, not just a different bound value for the same shape.
        await Book.objects.filter(name="Hitchhiker's Guide").count()
        await Book.objects.filter(subject="Comedy").count()
        await Book.objects.filter(rating=5.0).count()
        await Author.objects.filter(name="Douglas Adams").count()

    assert reports == []


@pytest.mark.asyncio
async def test_two_calls_under_different_timezones_are_the_same_shape(db):
    """A __day/__year/etc lookup's EXTRACT(... AT TIME ZONE 'zone') does NOT bake the zone name
    into the SQL TEXT - like every other value, it reaches the final, already-parameterized SQL as
    a bound placeholder. So the exact same filter call under two different configured timezones is
    intentionally the SAME shape here - this heuristic is only meant to be blind to VALUES, and a
    timezone name is just another value."""
    with override_timezone(use_timezone=True, timezone="Europe/Berlin"):
        berlin_obj = await DatetimeFields.objects.create(
            datetime=datetime(2024, 1, 2, 0, 30, tzinfo=Timezone.default())
        )
        async with capture_queries() as berlin_counter:
            await DatetimeFields.objects.filter(datetime__day=1, id=berlin_obj.id).exists()

    with override_timezone(use_timezone=True, timezone="Asia/Kolkata"):
        kolkata_obj = await DatetimeFields.objects.create(
            datetime=datetime(2024, 1, 2, 0, 30, tzinfo=Timezone.default())
        )
        async with capture_queries() as kolkata_counter:
            await DatetimeFields.objects.filter(datetime__day=1, id=kolkata_obj.id).exists()

    assert berlin_counter.queries and kolkata_counter.queries
    berlin_shape = RepeatedQueryDetector.shape_key(berlin_counter.queries[-1])
    kolkata_shape = RepeatedQueryDetector.shape_key(kolkata_counter.queries[-1])
    assert berlin_shape == kolkata_shape


@pytest.mark.asyncio
async def test_stop_removes_the_observer(db):
    author = await Author.objects.create(name="Douglas Adams")
    reports = []
    detector = RepeatedQueryDetector(threshold=1, window_seconds=10.0, action=reports.append)
    await detector.start()
    await detector.stop()
    # Stopping again, or a detector never started, does nothing.
    await detector.stop()

    await Book.objects.filter(author_id=author.id).count()

    assert reports == []
    assert not any(
        observer.callback == detector.on_query
        for observer in Observers.get_observers(QueryExecuted("", None, 0.0, None, "models"))
    )


@pytest.mark.asyncio
async def test_start_twice_counts_each_query_once(db):
    author = await Author.objects.create(name="Douglas Adams")
    reports = []
    detector = RepeatedQueryDetector(threshold=2, window_seconds=10.0, action=reports.append)
    await detector.start()
    await detector.start()
    try:
        for _ in range(2):
            await Book.objects.filter(author_id=author.id).count()
    finally:
        await detector.stop()

    assert [report.count for report in reports] == [2]


@pytest.mark.asyncio
async def test_two_detectors_count_on_their_own(db):
    author = await Author.objects.create(name="Douglas Adams")
    low_reports = []
    high_reports = []
    async with (
        RepeatedQueryDetector(threshold=2, window_seconds=10.0, action=low_reports.append),
        RepeatedQueryDetector(threshold=4, window_seconds=10.0, action=high_reports.append),
    ):
        for _ in range(3):
            await Book.objects.filter(author_id=author.id).count()

    assert [report.threshold for report in low_reports] == [2]
    assert high_reports == []


@pytest.mark.asyncio
async def test_reset_counts_starts_a_new_unit_of_work(db):
    author = await Author.objects.create(name="Douglas Adams")
    reports = []
    async with RepeatedQueryDetector(threshold=3, window_seconds=10.0, action=reports.append):
        for _ in range(2):
            await Book.objects.filter(author_id=author.id).count()
        RepeatedQueryDetector.reset_counts()
        for _ in range(2):
            await Book.objects.filter(author_id=author.id).count()

    assert reports == []


@pytest.mark.asyncio
async def test_window_expiry_starts_a_fresh_count(db):
    author = await Author.objects.create(name="Douglas Adams")
    reports = []
    # A window so short it has always elapsed by the time the next query in the loop runs.
    async with RepeatedQueryDetector(threshold=2, window_seconds=1e-9, action=reports.append):
        for _ in range(4):
            await Book.objects.filter(author_id=author.id).count()

    # Every query starts a brand new window (a nanosecond window has always expired), so the
    # count restarts at 1 each time and the threshold of 2 is never reached.
    assert reports == []


def test_shape_key_normalizes_whitespace_only():
    multiline = "SELECT *\n  FROM  book\n WHERE name = ?"
    single_line = "SELECT * FROM book WHERE name = ?"
    assert RepeatedQueryDetector.shape_key(multiline) == RepeatedQueryDetector.shape_key(single_line)


def test_shape_key_keeps_different_statements_distinct():
    assert RepeatedQueryDetector.shape_key("SELECT * FROM book") != RepeatedQueryDetector.shape_key(
        "SELECT * FROM author"
    )


@pytest.mark.asyncio
async def test_an_action_that_raises_is_logged_and_never_breaks_the_query(db, caplog):
    author = await Author.objects.create(name="Douglas Adams")

    def failing_action(report):
        raise AssertionError(f"N+1 detected: {report.count} queries of shape {report.shape_key!r}")

    with caplog.at_level("ERROR", logger="hare"):
        async with RepeatedQueryDetector(threshold=3, window_seconds=10.0, action=failing_action):
            for _ in range(3):
                assert await Book.objects.filter(author_id=author.id).count() == 0

    assert any("raised on" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    "options",
    [
        {"threshold": 0},
        {"threshold": -1},
        {"threshold": REPEATED_QUERY_MAX_THRESHOLD + 1},
        {"threshold": 2.5},
        {"threshold": True},
        {"window_seconds": 0},
        {"window_seconds": -1.0},
        {"window_seconds": math.inf},
        {"window_seconds": math.nan},
        {"window_seconds": REPEATED_QUERY_MAX_WINDOW_SECONDS + 1},
        {"window_seconds": "1"},
    ],
)
def test_detector_rejects_out_of_range_options(options):
    with pytest.raises(ConfigurationError, match=next(iter(options))):
        RepeatedQueryDetector(**({"threshold": 2, "window_seconds": 1.0} | options))


def test_detector_rejects_an_unknown_action_name():
    with pytest.raises(ConfigurationError, match="Unknown action 'raise'"):
        RepeatedQueryDetector(action="raise")


@pytest.mark.asyncio
async def test_threshold_of_one_reports_on_the_first_query(db):
    author = await Author.objects.create(name="Douglas Adams")
    reports = []
    async with RepeatedQueryDetector(threshold=1, window_seconds=10.0, action=reports.append):
        for _ in range(3):
            await Book.objects.filter(author_id=author.id).count()

    assert [report.count for report in reports] == [1]
