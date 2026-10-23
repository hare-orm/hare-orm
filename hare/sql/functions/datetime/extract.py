from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hare.sql.builder_methods import BuilderMethods
from hare.sql.terms.functions.function import Function
from hare.sql.terms.term import Term

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Self

    from hare.sql.builder.tables.table import Table
    from hare.sql.terms.node import TNode


class Extract(Function):
    """A part of a date or time - written by the dialect's renderer."""

    requires_dialect_renderer = True

    def __init__(
        self,
        date_part: Any,
        term: Term,
        alias: str | None = None,
        *,
        zone_name: str | None = None,
        use_local_zone_when_naive: bool = False,
        as_integer: bool = False,
        zone_as_literal: bool = False,
        is_time_of_day: bool = False,
    ) -> None:
        """
        Args:
            date_part: Which component to extract (``DatePart.YEAR``, etc).
            term: The term to extract from.
            zone_name: When set, the extraction happens in this named time zone instead of
                whatever zone the underlying value is already stored/returned in. Only passed
                for an aware ``DatetimeField`` under ``use_timezone=True``; a ``DateField`` has no
                time component for a zone to shift, and passing ``None`` here (the default)
                renders exactly the previous, unqualified extraction.
            use_local_zone_when_naive: When ``zone_name`` is ``None`` and this is ``True``, the
                extraction reads the value in the machine's local zone - on a dialect whose
                timestamp column holds an instant (a naive value held as an instant in that zone,
                while a bare ``EXTRACT`` would read it in the session's zone). A dialect storing the
                wall-clock digits themselves extracts them as they are and ignores it. Only set for a
                naive ``DatetimeField`` (``use_timezone=False``).
            alias: Optional alias for the term.
            as_integer: Cast the result to an integer - ``EXTRACT`` may return a numeric.
            zone_as_literal: Render ``zone_name`` as a literal rather than a parameter - an
                extraction selected, grouped, ordered and made ``DISTINCT ON`` renders the same
                expression in each clause, which a database compares by text (two parameters
                are two expressions). The zone is a validated IANA name.
            is_time_of_day: The term is a time of day - on a database storing one as text, the text
                is read as a time first.
        """
        super().__init__("EXTRACT", date_part, alias=alias)
        self.field = term
        self.date_part = date_part
        self.zone_name = zone_name
        self.use_local_zone_when_naive = use_local_zone_when_naive
        self.as_integer = as_integer
        self.zone_as_literal = zone_as_literal
        self.is_time_of_day = is_time_of_day

    def nodes_(self) -> Iterator[TNode]:
        yield from super().nodes_()
        yield from self.field.nodes_()

    @property
    def is_aggregate(self) -> bool | None:  # type:ignore[override]
        """Whether the extraction reads an aggregate - by the term it reads, kept apart from its
        arguments: of a column it is grouped by, not taken for a constant."""
        return Term.get_combined_is_aggregate([*(arg.is_aggregate for arg in self.args), self.field.is_aggregate])

    @BuilderMethods.builder
    def replace_table(self, current_table: Table | None, new_table: Table | None) -> Self:
        """Replaces all occurrences of the specified table with the new table.

        Useful when reusing fields across queries.

        Args:
            current_table: The table to be replaced.
            new_table: The table to replace with.

        Returns:
            A copy of the term with the tables replaced.
        """
        self.args = [parameter.replace_table(current_table, new_table) for parameter in self.args]
        self.field = self.field.replace_table(current_table, new_table)
        return self
