from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING

from hare.sql.terms.term import Term
from hare.sql.terms.values.value_wrapper import ValueWrapper

if TYPE_CHECKING:  # pragma: nocoverage
    from hare.sql import SqlContext
    from hare.sql.terms.node import TNode


class JsonArrayRowValues(Term):
    """``(SELECT json_extract(value, '$[0]'), ... FROM json_each(?))`` - value rows bound as ONE JSON
    array of arrays.

    Args:
        rows_json: The JSON array-of-arrays text.
        decode_hex_columns: Per column, whether its elements are hex strings to decode back into
            BLOBs.
    """

    def __init__(self, rows_json: str, decode_hex_columns: tuple[bool, ...]) -> None:
        super().__init__()
        self.rows_json = ValueWrapper(rows_json)
        self.decode_hex_columns = decode_hex_columns

    def nodes_(self) -> Iterator[TNode]:
        yield self  # type:ignore[misc]
        yield from self.rows_json.nodes_()

    def get_sql(self, sql_context: SqlContext) -> str:
        selected_values = []
        for index, decode_hex in enumerate(self.decode_hex_columns):
            element_sql = f"json_extract(value, '$[{index}]')"
            selected_values.append(f"unhex({element_sql})" if decode_hex else element_sql)
        rows_sql = self.rows_json.get_sql(sql_context)
        return f"(SELECT {', '.join(selected_values)} FROM json_each({rows_sql}))"  # nosec B608
