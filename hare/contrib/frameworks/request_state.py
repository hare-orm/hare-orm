from __future__ import annotations

from hare.contrib.repeated_queries import RepeatedQueryDetector
from hare.core.routing import Routing


class RequestState:
    """What hare keeps for one request - started empty for each request, not carried over from the
    requests its task served before or from the application's startup."""

    @staticmethod
    def reset() -> None:
        """Starts a request: an empty count of repeated queries (``RepeatedQueryDetector``) and no
        writes whose connections its reads would go to (``Routing``)."""
        RepeatedQueryDetector.reset_counts()
        Routing.forget_writes()
