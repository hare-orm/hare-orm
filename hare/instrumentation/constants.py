from __future__ import annotations

#: How many of the most recent exceptions of background observers ``Observers.wait_for_pending()``
#: keeps to re-raise - older ones are only counted (every one is logged when it happens).
MAX_STORED_OBSERVER_ERRORS = 100

#: How many background observer dispatches (one per event) may wait or run at once - past it, new
#: dispatches are dropped with a warning instead of piling up behind an observer that stopped
#: finishing.
MAX_PENDING_OBSERVER_DISPATCHES = 1000

#: How long Hare.close_connections() waits for background observers before cancelling the ones
#: still running (with a warning).
OBSERVER_SHUTDOWN_WAIT_TIMEOUT_SECONDS = 10.0


#: How many of the latest durations (waits for a pool connection, connects) a pool keeps for its
#: readers - a reader further behind gets the count of the ones it missed.
DURATION_RECORDS_CAPACITY = 4096

#: The module of each exported name - imported on first use: the database clients import modules
#: of this package while ``Observers`` builds on the clients' package.
EXPORTED_MODULES = {
    "Observers": "hare.instrumentation.observers.observers",
    "QueryCall": "hare.instrumentation.declarations",
    "QueryExecuted": "hare.instrumentation.declarations",
    "QueryTags": "hare.instrumentation.queries.query_tags",
    "QueryWrapper": "hare.instrumentation.queries.query_wrapper",
    "RowOperation": "hare.instrumentation.enums",
    "RowsChanged": "hare.instrumentation.declarations",
    "TransactionEvent": "hare.instrumentation.declarations",
}
