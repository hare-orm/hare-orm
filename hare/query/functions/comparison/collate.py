from __future__ import annotations

from typing import Any, ClassVar

from hare.exceptions import QueryError
from hare.query.expressions import Function
from hare.query.functions.comparison.function_arguments import FunctionArguments
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.constants import COLLATION_NAME_PATTERN
from hare.sql.functions.text.collate import Collate as CollateTerm
from hare.sql.terms.term import Term


class Collate(Function):
    """A text value compared and sorted by a database collation: ``Collate("name", "NOCASE")`` on
    SQLite, ``Collate("name", "C")`` on Postgres."""

    #: Its options are part of the key (``get_plan_options()``).
    plan_parts: ClassVar[DeclaredPlanParts] = (
        *Function.plan_parts,
        ("collation", PlanPartType.NONE),
    )

    populate_field_object = True
    keeps_argument_collation = True

    def __init__(self, expression: Any, collation: str) -> None:
        """
        Args:
            expression: A field name or an expression.
            collation: The collation's name - letters, digits, ``_``, ``-``, ``.`` and ``@``.

        Raises:
            QueryError: The name has another character.
        """
        if not isinstance(collation, str) or not COLLATION_NAME_PATTERN.fullmatch(collation):
            raise QueryError(f"Collate() collation must be a plain name, got {collation!r}")
        self.collation = collation
        super().__init__(FunctionArguments.get_expression(expression))

    def get_plan_options(self) -> tuple[Any, ...]:
        return (self.collation,)

    def _get_function_field(self, term: Term | str, *default_values: Any) -> CollateTerm:
        return CollateTerm(term, f'"{self.collation}"')
