from __future__ import annotations

import contextvars
import math
import os
import time
import traceback
from typing import ClassVar

from hare.contrib.repeated_queries.constants import (
    REPEATED_QUERY_MAX_THRESHOLD,
    REPEATED_QUERY_MAX_WINDOW_SECONDS,
    WARN_ACTION,
)
from hare.contrib.repeated_queries.repeated_query_collection import RepeatedQueryCollection
from hare.contrib.repeated_queries.repeated_query_report import RepeatedQueryReport, ReportAction
from hare.contrib.repeated_queries.shape_stats import ShapeStats
from hare.core.log import logger
from hare.exceptions import ConfigurationError
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted


class RepeatedQueryDetector:
    """Reports repeated ("N+1") queries: once ``threshold`` queries of one shape run within
    ``window_seconds`` in one unit of work, it reports them - once per window - with the stack of
    the application code that issued the last one.

    A query's shape is its SQL text with the whitespace collapsed: values are bound parameters, so
    two queries that differ only by values share a shape. A unit of work is a request: the
    framework integrations call ``reset_counts()`` at the start of each; elsewhere, call it at the
    start of each unit, in the task that runs it - the tasks it starts then add to the same counts.

    ::

        async with RepeatedQueryDetector(threshold=10, window_seconds=1.0):
            ...

    Args:
        threshold: How many queries of one shape within the window are repeated - an int in
            ``1..REPEATED_QUERY_MAX_THRESHOLD``.
        window_seconds: The window a shape is counted over - a number in
            ``(0, REPEATED_QUERY_MAX_WINDOW_SECONDS]``. A shape below the threshold this long after
            its first query starts counting fresh.
        action: ``"warn"`` logs a warning; a callable gets each ``RepeatedQueryReport``.

    Raises:
        ConfigurationError: ``threshold``/``window_seconds`` has the wrong type or is out of range,
            or ``action`` is a string other than ``"warn"``.
    """

    #: Each detector's shape counts in the current unit of work - None until the first query or
    #: ``reset_counts()``.
    current_counts: ClassVar[contextvars.ContextVar[dict[RepeatedQueryDetector, dict[str, ShapeStats]] | None]] = (
        contextvars.ContextVar("hare_repeated_query_counts", default=None)
    )

    #: The hare package directory, normalized for comparison with traceback frame filenames.
    library_directory: ClassVar[str] = os.path.normcase(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )

    def __init__(
        self, *, threshold: int = 10, window_seconds: float = 1.0, action: ReportAction | str = WARN_ACTION
    ) -> None:
        RepeatedQueryDetector.validate_options(threshold, window_seconds)
        if callable(action):
            self.action: ReportAction = action
        elif action == WARN_ACTION:
            self.action = RepeatedQueryDetector.warn
        else:
            raise ConfigurationError(f"Unknown action {action!r} - pass {WARN_ACTION!r} or a callable")
        self.threshold = threshold
        self.window_seconds = window_seconds

    async def start(self) -> None:
        """Starts counting - nothing when already started."""
        Observers.observe(QueryExecuted, self.on_query)

    async def stop(self) -> None:
        """Stops counting - nothing when not started."""
        Observers.unobserve(QueryExecuted, self.on_query)

    async def __aenter__(self) -> RepeatedQueryDetector:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    @classmethod
    def reset_counts(cls) -> None:
        """Starts a unit of work: every detector counts from zero in the current context and the
        tasks it starts from now on."""
        cls.current_counts.set({})

    def get_counts(self) -> dict[str, ShapeStats]:
        """This detector's shape counts in the current unit of work - started here when the
        context has none yet."""
        counts_by_detector = RepeatedQueryDetector.current_counts.get()
        if counts_by_detector is None:
            counts_by_detector = {}
            RepeatedQueryDetector.current_counts.set(counts_by_detector)
        return counts_by_detector.setdefault(self, {})

    def on_query(self, event: QueryExecuted) -> None:
        """Counts a query, reporting its shape when it reaches the threshold - the observer
        ``start()`` installs, called in the task that ran the query.

        Args:
            event: The query.
        """
        shape_key = RepeatedQueryDetector.shape_key(event.sql)
        counts = self.get_counts()
        now = time.monotonic()
        stats = counts.get(shape_key)
        if stats is None or (now - stats.window_start) >= self.window_seconds:
            stats = ShapeStats(sql_sample=event.sql, window_start=now)
            counts[shape_key] = stats
        stats.count += 1
        if stats.count == self.threshold:
            self.action(
                RepeatedQueryReport(
                    shape_key=shape_key,
                    count=stats.count,
                    threshold=self.threshold,
                    window_seconds=self.window_seconds,
                    sql_sample=stats.sql_sample,
                    call_site=RepeatedQueryDetector.capture_application_call_site(),
                )
            )

    @staticmethod
    def shape_key(sql: str) -> str:
        """A query's shape: its SQL with every run of whitespace collapsed to one space. A shape
        key is its own shape.

        Args:
            sql: The SQL.

        Returns:
            The shape key.
        """
        return " ".join(sql.split())

    @staticmethod
    def collect() -> RepeatedQueryCollection:
        """A ``with``/``async with`` block counting its queries per shape - without ``start()``.

        Example:
            async with RepeatedQueryDetector.collect() as collection:
                await handle_request()
            assert collection.repeated(threshold=2) == []

        Returns:
            The collection.
        """
        return RepeatedQueryCollection(RepeatedQueryDetector)

    @classmethod
    def capture_application_call_site(cls) -> str:
        """The current stack up to the innermost frame outside the hare package - the application
        code that issued the query, when called from a query observer.

        Returns:
            The formatted stack.
        """
        frames = traceback.extract_stack()[:-1]
        application_frame_count = len(frames)
        while application_frame_count > 0 and cls.is_library_frame(frames[application_frame_count - 1]):
            application_frame_count -= 1
        if application_frame_count == 0:
            application_frame_count = len(frames)
        return "".join(traceback.format_list(frames[:application_frame_count]))

    @classmethod
    def is_library_frame(cls, frame: traceback.FrameSummary) -> bool:
        """Whether a frame belongs to the hare package itself."""
        return os.path.normcase(os.path.abspath(frame.filename)).startswith(cls.library_directory + os.sep)

    @staticmethod
    def validate_options(threshold: int, window_seconds: float) -> None:
        """Checks the options for type and range.

        Raises:
            ConfigurationError: An option has the wrong type or is out of range.
        """
        if (
            isinstance(threshold, bool)
            or not isinstance(threshold, int)
            or not 1 <= threshold <= REPEATED_QUERY_MAX_THRESHOLD
        ):
            raise ConfigurationError(
                f"threshold must be an int in 1..{REPEATED_QUERY_MAX_THRESHOLD}, got {threshold!r}"
            )
        if (
            isinstance(window_seconds, bool)
            or not isinstance(window_seconds, int | float)
            or not math.isfinite(window_seconds)
            or not 0 < window_seconds <= REPEATED_QUERY_MAX_WINDOW_SECONDS
        ):
            raise ConfigurationError(
                f"window_seconds must be a number > 0 and <= {REPEATED_QUERY_MAX_WINDOW_SECONDS}, "
                f"got {window_seconds!r}"
            )

    @staticmethod
    def warn(report: RepeatedQueryReport) -> None:
        """The default action: logs the report as a warning.

        Args:
            report: The report.
        """
        logger.warning(
            "Possible N+1 query: %d queries of the same shape within %.1fs (threshold %d): %s\n%s",
            report.count,
            report.window_seconds,
            report.threshold,
            report.sql_sample,
            report.call_site,
        )
