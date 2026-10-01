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

#: The text a ``QueryExecuted`` of a bulk COPY load carries as its SQL.
COPY_STATEMENT_TEMPLATE = "COPY {table} ({columns}) FROM STDIN"
