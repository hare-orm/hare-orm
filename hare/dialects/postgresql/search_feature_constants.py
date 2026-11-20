from hare.dialects.base.search.enums import TextSearchOperation

POSTGRESQL_TEXT_SEARCH_OPERATIONS = frozenset(
    operation for operation in TextSearchOperation if operation is not TextSearchOperation.RANK_FIELD_WEIGHTS
)
