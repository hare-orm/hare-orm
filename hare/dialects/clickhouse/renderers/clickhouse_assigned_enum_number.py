from __future__ import annotations

from typing import TYPE_CHECKING

from hare.dialects.clickhouse.renderers.constants import CLICKHOUSE_ASSIGNED_ENUM_TEMPLATE
from hare.sql.terms.functions.function import Function

if TYPE_CHECKING:
    from hare.sql.sql_context import SqlContext
    from hare.sql.terms.term import Term


class ClickhouseAssignedEnumNumber(Function):
    """A number an UPDATE writes into an integer enum column, refused when no member of the enum has
    it - a mutation writes such a number into an ``Enum8``/``Enum16`` column, which the drivers then
    read as NULL or fail on.

    Args:
        term: The number.
        member_numbers: The numbers of the enum's members.
    """

    def __init__(self, term: Term, member_numbers: list[int]) -> None:
        super().__init__("hare_assigned_enum_number", term)
        self.member_numbers = sorted(member_numbers)

    def get_function_sql(self, sql_context: SqlContext) -> str:
        return CLICKHOUSE_ASSIGNED_ENUM_TEMPLATE.format(
            self.get_arg_sql(self.args[0], sql_context), ", ".join(str(number) for number in self.member_numbers)
        )
