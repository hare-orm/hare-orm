from __future__ import annotations

#: The tsquery function per search type, keyed by ``SearchType`` values.
SEARCH_TYPE_FUNCTIONS: dict[str, str] = {
    "plain": "PLAINTO_TSQUERY",
    "phrase": "PHRASETO_TSQUERY",
    "raw": "TO_TSQUERY",
    "websearch": "WEBSEARCH_TO_TSQUERY",
}

#: The tsquery operator joining two queries, keyed by ``SearchOperator`` values.
POSTGRESQL_SEARCH_QUERY_OPERATORS: dict[str, str] = {"and": " && ", "or": " || "}

#: The ``ts_headline()`` option of each ``SearchHeadline`` argument.
POSTGRESQL_HEADLINE_OPTION_NAMES: dict[str, str] = {
    "start_sel": "StartSel",
    "stop_sel": "StopSel",
    "max_words": "MaxWords",
    "min_words": "MinWords",
    "short_word": "ShortWord",
    "highlight_all": "HighlightAll",
    "max_fragments": "MaxFragments",
    "fragment_delimiter": "FragmentDelimiter",
}
