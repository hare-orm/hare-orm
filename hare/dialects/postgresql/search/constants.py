from __future__ import annotations

import re

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

#: The operator joining the vectors of a ``TSVectorField``'s sources in its generation expression.
POSTGRESQL_TS_VECTOR_CONCATENATION_SQL = " || "
#: A weighted source vector as PostgreSQL echoes it back - ``setweight(<vector>, 'A'::"char")``.
POSTGRESQL_TS_VECTOR_WEIGHTED_PATTERN = re.compile(
    r"setweight\((?P<vector>.+), '(?P<weight>[A-D])'::\"char\"\)", re.DOTALL
)
#: A source vector - ``to_tsvector('english'::regconfig, <source>)``.
POSTGRESQL_TS_VECTOR_PATTERN = re.compile(
    r"to_tsvector\('(?P<config>(?:[^']|'')+)'::regconfig, (?P<source>.+)\)", re.DOTALL
)
#: The source of a vector - its column made text and its NULL empty:
#: ``(COALESCE(title, ''::character varying))::text``, ``COALESCE(body, ''::text)``,
#: ``COALESCE((number)::text, ''::text)``.
POSTGRESQL_TS_VECTOR_SOURCE_PATTERN = re.compile(
    r"\(?COALESCE\(\(?(?P<column>\"(?:[^\"]|\"\")+\"|[a-z_][a-z0-9_$]*)\)?(?:::text)?, ''::[a-z ]+\)\)?(?:::text)?"
)

#: How many label weights ``SearchRank(weights=[...])`` takes - one per label, D first: ``ts_rank()``'s array.
POSTGRESQL_RANK_LABEL_WEIGHT_COUNT = 4
#: The integers of a PostgreSQL ``integer`` - ``ts_rank()``'s normalization bitmask is one.
POSTGRESQL_INTEGER_RANGE = (-(2**31), 2**31 - 1)
