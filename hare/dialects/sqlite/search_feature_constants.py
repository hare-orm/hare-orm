from hare.dialects.base.search.enums import TextSearchOperation

SQLITE_TEXT_SEARCH_OPERATIONS = frozenset(
    {
        TextSearchOperation.RANK_FIELD_WEIGHTS,
        TextSearchOperation.HEADLINE_START_SEL,
        TextSearchOperation.HEADLINE_STOP_SEL,
        TextSearchOperation.HEADLINE_MAX_WORDS,
        TextSearchOperation.HEADLINE_FRAGMENT_DELIMITER,
    }
)
