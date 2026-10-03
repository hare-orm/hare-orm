from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING

from hare.contrib.repeated_queries.repeated_query_entry import RepeatedQueryEntry
from hare.exceptions import QueryError
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.contrib.repeated_queries.repeated_query_detector import RepeatedQueryDetector


class RepeatedQueryCollection:
    """Per-shape query counts of one ``RepeatedQueryDetector.collect()`` block - ``with`` or
    ``async with``. Counts every query issued in the block and the tasks it starts while it is
    open; nested blocks count their queries in every enclosing block too.
    """

    def __init__(self, detector_class: type[RepeatedQueryDetector]) -> None:
        self.detector_class = detector_class
        self.entries_by_shape_key: dict[str, RepeatedQueryEntry] = {}
        self.total = 0
        self.is_open = False
        self.observing: AbstractContextManager[None] | None = None

    def __enter__(self) -> RepeatedQueryCollection:
        if self.is_open:
            raise QueryError("RepeatedQueryCollection is already active")
        self.is_open = True
        self.observing = Observers.observing(QueryExecuted, self.on_query)
        self.observing.__enter__()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.is_open = False
        if self.observing is not None:
            observing, self.observing = self.observing, None
            observing.__exit__(None, None, None)

    async def __aenter__(self) -> RepeatedQueryCollection:
        return self.__enter__()

    async def __aexit__(self, *exc_info: object) -> None:
        self.__exit__(*exc_info)

    def __iter__(self) -> Iterator[RepeatedQueryEntry]:
        return iter(list(self.entries_by_shape_key.values()))

    def __len__(self) -> int:
        return len(self.entries_by_shape_key)

    def count_for(self, sql_or_shape_key: str) -> int:
        """How many queries of a shape were counted.

        Args:
            sql_or_shape_key: Raw SQL or a shape key.

        Returns:
            The count, 0 for a shape never seen.
        """
        entry = self.entries_by_shape_key.get(self.detector_class.shape_key(sql_or_shape_key))
        return entry.count if entry is not None else 0

    def repeated(self, threshold: int = 2) -> list[RepeatedQueryEntry]:
        """The shapes issued at least ``threshold`` times, most frequent first.

        Args:
            threshold: The minimum count, at least 1.

        Returns:
            The entries.

        Raises:
            ValueError: ``threshold`` is less than 1.
        """
        if threshold < 1:
            raise QueryError(f"threshold must be at least 1, got {threshold}")
        matching_entries = [entry for entry in self.entries_by_shape_key.values() if entry.count >= threshold]
        return sorted(matching_entries, key=lambda entry: entry.count, reverse=True)

    def on_query(self, event: QueryExecuted) -> None:
        """Counts a query - the observer the block installs.

        Args:
            event: The query.
        """
        if not self.is_open:
            return
        shape_key = self.detector_class.shape_key(event.sql)
        entry = self.entries_by_shape_key.get(shape_key)
        if entry is None:
            entry = RepeatedQueryEntry(
                shape_key=shape_key,
                count=0,
                sql_sample=event.sql,
                call_site=self.detector_class.capture_application_call_site(),
            )
            self.entries_by_shape_key[shape_key] = entry
        entry.count += 1
        self.total += 1
