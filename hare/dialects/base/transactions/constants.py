from __future__ import annotations

#: How long an ordinary query waits for its turn while a sibling task's savepoint is open on the
#: same transaction, before raising TransactionManagementError.
AMBIENT_QUERY_SAVEPOINT_WAIT_TIMEOUT_SECONDS = 30.0
