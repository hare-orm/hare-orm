"""Full-text search, on every dialect with it - PostgreSQL's tsvector search and SQLite's FTS5
through the model's ``FullTextIndex``; each dialect writes the SQL its own way."""

from __future__ import annotations

from hare.search.enums import SearchOperator, SearchType
from hare.search.query.combined_search_query import CombinedSearchQuery
from hare.search.query.declarations import RawSearchQueryText
from hare.search.query.search_query import SearchQuery
from hare.search.query.search_query_combinable import SearchQueryCombinable
from hare.search.search_headline import SearchHeadline
from hare.search.search_rank import SearchRank
from hare.search.vector.combined_search_vector import CombinedSearchVector
from hare.search.vector.search_vector import SearchVector
from hare.search.vector.search_vector_combinable import SearchVectorCombinable

__all__ = [
    "CombinedSearchQuery",
    "CombinedSearchVector",
    "RawSearchQueryText",
    "SearchHeadline",
    "SearchOperator",
    "SearchQuery",
    "SearchQueryCombinable",
    "SearchRank",
    "SearchType",
    "SearchVector",
    "SearchVectorCombinable",
]
