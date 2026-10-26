from __future__ import annotations

import dataclasses
import datetime
import email.utils
import random

from hare.contrib.request_query.api_getter.constants import (
    API_RETRIED_STATUS_CODES,
    DEFAULT_API_RETRY_BASE_DELAY_SECONDS,
    DEFAULT_API_RETRY_MAX_DELAY_SECONDS,
)


@dataclasses.dataclass(frozen=True, slots=True)
class ApiGetterRetry:
    """How a source's request is repeated when it fails for a while - a broken connection or an answer of
    ``status_codes``: after a random pause up to ``base_delay_seconds * 2 ** repeat`` (no more than
    ``max_delay_seconds``), or as long as the answer's ``Retry-After`` asks, within the same limit.

    Args:
        attempts: How many times a request is repeated - 0 for none.
        base_delay_seconds: The longest pause before the first repeat.
        max_delay_seconds: The longest pause before any repeat.
        status_codes: The answers repeated.
    """

    attempts: int = 0
    base_delay_seconds: float = DEFAULT_API_RETRY_BASE_DELAY_SECONDS
    max_delay_seconds: float = DEFAULT_API_RETRY_MAX_DELAY_SECONDS
    status_codes: frozenset[int] = API_RETRIED_STATUS_CODES

    def __post_init__(self) -> None:
        # Any collection of statuses is taken - the retry stays hashable.
        object.__setattr__(self, "status_codes", frozenset(self.status_codes))

    def get_delay(self, repeat: int, retry_after: str | None) -> float:
        """The pause before a repeat.

        Args:
            repeat: How many repeats were made - 0 before the first.
            retry_after: The answer's ``Retry-After``, None without one.

        Returns:
            The seconds.
        """
        if retry_after is not None and (seconds := ApiGetterRetry.read_retry_after(retry_after)) is not None:
            return min(seconds, self.max_delay_seconds)
        # A random pause - requests failing together don't repeat together.
        return random.uniform(0, min(self.max_delay_seconds, self.base_delay_seconds * 2**repeat))  # nosec B311

    @staticmethod
    def read_retry_after(value: str) -> float | None:
        """The seconds a ``Retry-After`` asks to wait - given as seconds or as an HTTP date.

        Args:
            value: The header's value.

        Returns:
            The seconds, None for a value of neither form.
        """
        seconds = value.strip()
        # Digits of ASCII alone - ``str.isdigit()`` takes "²" too.
        if seconds.isascii() and seconds.isdigit():
            return float(seconds)
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=datetime.UTC)
        return max((moment - datetime.datetime.now(datetime.UTC)).total_seconds(), 0.0)
