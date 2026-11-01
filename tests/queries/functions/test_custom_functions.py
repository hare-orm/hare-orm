"""A project's own ``Function``: one writing its SQL itself through ``get_function_term()``, and one
naming no SQL function."""

from typing import ClassVar

import pytest

from hare.exceptions import ConfigurationError
from hare.query.expressions import Function
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.enums import PlanPartType
from hare.sql.terms import Function as SqlFunction, LiteralValue
from tests.shared_models.testmodels import CharFields


class Prefix(Function):
    plan_parts: ClassVar[DeclaredPlanParts] = (*Function.plan_parts, ("length", PlanPartType.KEY))

    def __init__(self, field: str, length: int) -> None:
        super().__init__(field)
        self.length = int(length)

    def get_function_term(self, term, *default_values):
        return SqlFunction("SUBSTR", term, LiteralValue("1"), LiteralValue(str(self.length)))


class Unnamed(Function):
    pass


@pytest.mark.asyncio
async def test_a_function_writing_its_option_into_its_sql_keeps_a_plan_per_option(db):
    await CharFields.objects.create(id=1, char="hello")
    for length, expected in ((1, "h"), (3, "hel"), (1, "h"), (5, "hello")):
        values = await CharFields.objects.annotate(prefix=Prefix("char", length)).values_list("prefix", flat=True)
        assert values == [expected]


@pytest.mark.asyncio
async def test_a_function_naming_no_sql_function_is_refused_before_the_query(db):
    """The field's name became the function's: "char"() went to the database."""
    with pytest.raises(ConfigurationError, match="Unnamed names no SQL function"):
        await CharFields.objects.annotate(value=Unnamed("char")).values_list("value", flat=True)
