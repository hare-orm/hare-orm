from __future__ import annotations

#: How long a shielded COMMIT/ROLLBACK/savepoint statement is waited for after its task was
#: cancelled - a dead server must not hang the cancellation.
SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS = 30.0

#: How long a top-level COMMIT/ROLLBACK waits for the statements and savepoints other tasks still
#: run on the same transaction (asyncio.gather()/TaskGroup siblings) before giving up - a COMMIT
#: then fails with TransactionManagementError and nothing is committed, a ROLLBACK goes ahead
#: anyway.
TRANSACTION_END_WAIT_TIMEOUT_SECONDS = 30.0

#: The bound of DatabaseClient.ping() - a server that stopped answering without closing the socket
#: never makes the driver raise.
PING_TIMEOUT_SECONDS = 5.0

#: The statement of DatabaseClient.ping() - standard SQL every database runs.
PING_SQL = "SELECT 1"


#: The error of a statement or an ending on a transaction that ended already.
TRANSACTION_FINALISED_MESSAGE = "Transaction already finalised"

#: What Python's codecs say of a text holding half of a surrogate pair - no database stores one.
UNENCODABLE_SURROGATE_REASON = "surrogates not allowed"
#: The refusal of such a text among a statement's values.
UNENCODABLE_SURROGATE_MESSAGE = (
    "A text of the statement holds a lone surrogate (a character from U+D800 to U+DFFF), which UTF-8 has no "
    "encoding of - no database stores it"
)

#: The refusal of a query on a client closed and dropped (``close_connections()``) - it would open a
#: connection nothing closes.
DISCARDED_CLIENT_MESSAGE = (
    "The connection {alias!r} was closed and dropped - a query runs on the connection the context gives now"
)
#: The refusal of a statement of a transaction from another event loop, on a driver whose connections
#: belong to the loop they were opened on.
TRANSACTION_ON_ANOTHER_EVENT_LOOP_MESSAGE = (
    "The transaction on {alias!r} began on another event loop, and the connections of its driver belong to "
    "the loop they were opened on - run its statements on that loop. An application under test running in an "
    "event loop of its own takes the pytest plugin's hare_app_transactional_db"
)
