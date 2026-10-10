from __future__ import annotations

from hare.query.expressions import Expression


class RawSearchQueryText(Expression, abstract=True):
    """An expression giving a search query in the database's own syntax - PostgreSQL's lexemes. A
    ``SearchQuery`` of one reads it as ``SearchType.RAW``, on a connection with
    ``features.supports_text_search_configurations``."""
