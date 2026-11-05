DEPENDENCY_REQUIRES_EXECUTION = (
    "This query needs values from another connection before its SQL can be compiled; await the query first."
)
DEPENDENT_SET_OPERATION = (
    "Set operations on a relation to a model on another connection aren't supported; "
    "read and combine its keys with values_list('pk', flat=True) instead."
)
