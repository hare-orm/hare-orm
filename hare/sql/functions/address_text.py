from __future__ import annotations

from typing import Any

from hare.sql.functions.cast import Cast
from hare.sql.types.sql_types import SqlTypes


class AddressText(Cast):
    """The text of an IP address the text lookups match - its ``VARCHAR`` cast; a dialect holding an
    IPv4 address as an IPv6 one writes it as the IPv4 text."""

    def __init__(self, term: Any) -> None:
        """
        Args:
            term: The address.
        """
        super().__init__(term, SqlTypes.VARCHAR)
