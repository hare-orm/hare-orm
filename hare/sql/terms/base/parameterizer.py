from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from hare.sql.enums import DatePart

if TYPE_CHECKING:
    pass
from hare.sql.terms.base.parameter import Parameter
from hare.sql.terms.base.term import Term


class Parameterizer:
    """Replaces values with parameters in a query and keeps them in ``values``.

    Example::

        parameterizer = Parameterizer()
        customers = Table("customers")
        query = Query.from_(customers).select(customers.id).where(customers.lname == "Mustermann")
        query.get_sql(SqliteQuery.SQL_CONTEXT.copy(parameterizer=parameterizer))
        # 'SELECT "id" FROM "customers" WHERE "lname"=?', parameterizer.values == ['Mustermann']
    """

    #: The types some values of which ``should_parameterize()`` refuses - a value of any other
    #: type is always bound as a parameter.
    conditionally_parameterized_types: ClassVar[tuple[type, ...]] = (DatePart, str)

    def __init__(self, placeholder_factory: Callable[[int], str] | None = None) -> None:
        self.placeholder_factory = placeholder_factory
        self.values: list[Any] = []
        #: The term and the number of the parameter it binds as, by the term's id - the term kept,
        #: so no term made while rendering takes the id of one already collected.
        self.indexes_by_source_id: dict[int, tuple[Term, int]] = {}

    def should_parameterize(self, value: Any) -> bool:
        # A DatePart is an SQL keyword (EXTRACT(YEAR FROM ...)), not a value.
        if isinstance(value, DatePart):
            return False

        if isinstance(value, str) and value == "*":
            return False
        return True

    def create_param(self, value: Any, source: Term | None = None, reuse: bool = False) -> Parameter:
        """Binds ``value`` as the next parameter.

        Args:
            value: The value.
            source: The term the value comes from - kept by a ``RecordingParameterizer``.
            reuse: Whether a term rendered again binds as the parameter it bound as the first time -
                for numbered placeholders (``$1``), where the database then sees both places as one
                expression (``DISTINCT ON`` matching ``ORDER BY``).

        Returns:
            The parameter placeholder.
        """
        if reuse and source is not None:
            source_and_index = self.indexes_by_source_id.get(id(source))
            if source_and_index is not None:
                index = source_and_index[1]
                return Parameter(self.placeholder_factory(index)) if self.placeholder_factory else Parameter(idx=index)
        # An enum member binds as its value, the same one its literal would render.
        while isinstance(value, Enum):
            value = value.value
        self._add_value(value, source)
        index = len(self.values)
        if reuse and source is not None:
            self.indexes_by_source_id[id(source)] = (source, index)
        if self.placeholder_factory:
            return Parameter(self.placeholder_factory(index))
        else:
            return Parameter(idx=index)

    def _add_value(self, value: Any, source: Term | None) -> None:
        """Adds the value of a new parameter.

        Args:
            value: The value.
            source: The term it comes from.
        """
        self.values.append(value)

    def record_literal(self, source: Term) -> None:
        """Notes a term whose value is written into the SQL text instead of bound - as a literal,
        or reformatted while rendering. Kept by a ``RecordingParameterizer``.

        Args:
            source: The term.
        """
