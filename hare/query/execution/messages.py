DEPENDENCY_REQUIRES_EXECUTION = (
    "This query needs values from another connection before its SQL can be compiled; await the query first."
)
DEPENDENT_SET_OPERATION = (
    "Set operations on a relation to a model on another connection aren't supported; "
    "read and combine its keys with values_list('pk', flat=True) instead."
)
SELECT_FOR_UPDATE_OUTSIDE_TRANSACTION = (
    "select_for_update() requires an active Transactions.atomic() block - a "
    "SELECT ... FOR UPDATE lock is only meaningful for the lifetime of the enclosing "
    "transaction; used outside one, the lock is acquired and immediately released "
    "(autocommit), giving no real protection while looking like it does"
)
