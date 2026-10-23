from __future__ import annotations

import logging
from collections.abc import Collection


class DriverStatementLogFilter(logging.Filter):
    """Drops the DEBUG records a driver library writes of each statement it runs with the statement's
    values in them - a ``sensitive=True`` field's too. hare logs every statement itself, its sensitive
    parameters shown as ``<hidden>``.

    Args:
        record_formats: The formats of the records dropped, as the library writes them.
    """

    def __init__(self, record_formats: Collection[str]) -> None:
        super().__init__()
        self.record_formats = frozenset(record_formats)

    def filter(self, record: logging.LogRecord) -> bool:
        return record.msg not in self.record_formats

    @classmethod
    def install(cls, logger_name: str, record_formats: Collection[str]) -> None:
        """Puts the filter on a library's logger, once.

        Args:
            logger_name: The library's logger.
            record_formats: The formats of the records dropped.
        """
        library_logger = logging.getLogger(logger_name)
        if not any(isinstance(log_filter, cls) for log_filter in library_logger.filters):
            library_logger.addFilter(cls(record_formats))
