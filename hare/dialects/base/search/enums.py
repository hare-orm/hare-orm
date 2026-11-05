from enum import StrEnum


class TextSearchOperation(StrEnum):
    """Independently supported search operations and optional arguments."""

    VECTOR = "vector"
    VECTOR_CONCATENATION = "vector_concatenation"
    VECTOR_WEIGHT = "vector_weight"
    RAW_QUERY_EXPRESSION = "raw_query_expression"
    RANK_FIELD_WEIGHTS = "rank_field_weights"
    RANK_LABEL_WEIGHTS = "rank_label_weights"
    RANK_NORMALIZATION = "rank_normalization"
    RANK_COVER_DENSITY = "rank_cover_density"
    HEADLINE_START_SEL = "headline_start_sel"
    HEADLINE_STOP_SEL = "headline_stop_sel"
    HEADLINE_MAX_WORDS = "headline_max_words"
    HEADLINE_MIN_WORDS = "headline_min_words"
    HEADLINE_SHORT_WORD = "headline_short_word"
    HEADLINE_HIGHLIGHT_ALL = "headline_highlight_all"
    HEADLINE_MAX_FRAGMENTS = "headline_max_fragments"
    HEADLINE_FRAGMENT_DELIMITER = "headline_fragment_delimiter"
