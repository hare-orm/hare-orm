from __future__ import annotations

from typing import Any

from hare.dialects.base.connection.connection_option import ConnectionOption
from hare.dialects.enums import ConnectionOptionType
from hare.dialects.postgresql.connection.search_path import SearchPath


class SearchPathOption(ConnectionOption):
    """Read a SearchPath, a sequence of exact names, or PostgreSQL's setting syntax."""

    def __init__(self) -> None:
        super().__init__("search_path", ConnectionOptionType.TEXT)

    def parse(self, raw_value: Any) -> SearchPath:
        if isinstance(raw_value, SearchPath):
            return raw_value
        return SearchPath.parse(raw_value) if isinstance(raw_value, str) else SearchPath(*raw_value)
