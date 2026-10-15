"""Where each value of a plan comes from: an object made again for each copy of a query stands for
the object it was made from, a condition derived from a value keeps that value's origin, and a
class declaring how its attributes meet the plan gets its description generated from them."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from hare.query.expressions import Exists, F, Q, Value
from hare.query.plans.constants import OPTIONAL_VALUE_ORIGIN
from hare.query.plans.description.declared_plan_parts import DeclaredPlanParts
from hare.query.plans.description.plan_context import PlanContext
from hare.query.plans.description.plan_description import PlanDescription
from hare.query.plans.description.plannable import Plannable
from hare.query.plans.enums import PlanKeyForm, PlanPartType
from hare.query.plans.plan_origins import PlanOrigins
from hare.query.plans.statement.query_key_compiler import QueryKeyCompiler
from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
from hare.sql.terms.values.value_wrapper import ValueWrapper
from tests.testmodels import IntFields


def test_a_copy_stands_for_the_object_it_was_made_from():
    condition = Q(intnum=1)
    stamped = condition._with_filter_call_generation(3)
    negated = ~stamped
    assert PlanOrigins.get_value_origin(negated, "intnum") == PlanOrigins.get_value_origin(condition, "intnum")
    assert PlanOrigins.get_token(negated) == PlanOrigins.get_token(condition)


def test_a_derived_condition_keeps_the_origin_of_its_value():
    origin = PlanOrigins.get_value_origin(Q(writer=1), "writer")
    derived = PlanOrigins.derive(Q(writer__id=1), {"writer__id": origin})
    assert PlanOrigins.get_value_origin(derived, "writer__id") == origin
    assert PlanOrigins.get_value_origin(derived, "other") != origin


def test_origins_are_listed_only_while_a_plan_is_recorded():
    expression = Value(5)
    assert expression.get_plan_description(PlanContext.EMPTY).origins is None
    described = PlanOrigins.describe(lambda: expression.get_plan_description(PlanContext.EMPTY))
    assert described.origins == [PlanOrigins.get_value_origin(expression, "value")]
    assert PlanOrigins.records is False


def test_a_description_made_while_another_is_made_lists_its_origins_too():
    def describe_inside() -> PlanDescription | None:
        PlanOrigins.describe(lambda: None)
        return Value(1).get_plan_description(PlanContext.EMPTY)

    assert PlanOrigins.describe(describe_inside).origins is not None


def test_values_of_one_origin_bind_from_the_first_and_only_when_they_are_one_object():
    shared = Q(intnum=1)
    description = PlanOrigins.describe(lambda: Q(shared, shared).get_plan_description(PlanContext.EMPTY))
    assert len(description.values) == 2
    assert description.origins[0] == description.origins[1]
    # No reference of the second value is needed - it is the first one's.
    value_references = [(description.origins[0], object())]
    sources = StatementPlanRuns.get_value_sources(description, value_references)
    assert sources is not None
    assert sources[1] == ((1, 0),)


def test_a_reference_of_no_described_value_keeps_no_plan():
    description = PlanOrigins.describe(lambda: Q(intnum=1).get_plan_description(PlanContext.EMPTY))
    assert StatementPlanRuns.get_value_sources(description, [(("elsewhere", "intnum"), object())]) is None
    assert StatementPlanRuns.get_value_sources(description, []) is None


def test_declared_parts_generate_the_description():
    class Shifted(Value):
        plan_parts: ClassVar[DeclaredPlanParts] = (
            ("offset", PlanPartType.KEY),
            ("value", PlanPartType.LITERAL),
        )

        def __init__(self, value: Any, offset: int) -> None:
            super().__init__(value)
            self.offset = offset

    description = PlanOrigins.describe(lambda: Shifted(7, 2).get_plan_description(PlanContext.EMPTY))
    assert description.structure[:2] == (Shifted, 2)
    assert description.values == [7]
    assert description.structure != Shifted(7, 3).get_plan_description(PlanContext.EMPTY).structure


def test_a_class_declaring_parts_cannot_describe_itself_too():
    with pytest.raises(TypeError, match="declares plan_parts and get_plan_description"):

        class Both(Plannable):
            plan_parts: ClassVar[DeclaredPlanParts] = ()

            def get_plan_description(self, context: PlanContext) -> PlanDescription | None:
                return None


def test_a_slot_no_part_classifies_is_rejected():
    with pytest.raises(TypeError, match="classifies none of its slots"):

        class Unclassified(Plannable):
            __slots__ = ("kept", "forgotten")
            plan_parts: ClassVar[DeclaredPlanParts] = (("kept", PlanPartType.KEY),)


@pytest.mark.asyncio
async def test_an_exists_condition_describes_its_query(db):
    condition = Q(Exists(IntFields.objects.filter(intnum=F("intnum_null"), id=3)))
    description = PlanOrigins.describe(lambda: condition.get_plan_description(PlanContext.EMPTY))
    assert description.values == [3]
    assert len(description.origins) == 1


class Point(Plannable):
    """A plannable of parts of the types a class not built from ``Function`` declares."""

    plan_parts: ClassVar[DeclaredPlanParts] = (
        ("keeps_plan", PlanPartType.KEEPS_PLAN_METHOD),
        ("label", PlanPartType.NONE),
        ("plannable_point", PlanPartType.NONE),
        ("coordinates", PlanPartType.ENCODED_ARGUMENT),
        ("get_text", PlanPartType.VALUE_METHOD),
        ("parameters", PlanPartType.PARAMETERS),
        ("condition", PlanPartType.NONE),
        ("get_condition", PlanPartType.JOIN_CONDITION_METHOD),
    )

    def __init__(self, coordinates: Any, *, plannable: bool = True) -> None:
        self.label = "point"
        self.coordinates = coordinates
        self.parameters = [ValueWrapper(1), ValueWrapper(2)]
        self.condition = Q(intnum=3)
        self.plannable_point = plannable

    def keeps_plan(self) -> bool:
        return self.plannable_point

    def get_text(self) -> str:
        return f"POINT{self.coordinates}"

    def get_condition(self) -> Q:
        return self.condition


def test_part_types_describe_their_values():
    point = Point((1.5, 2.5))
    description = PlanOrigins.describe(lambda: point.get_plan_description(PlanContext.EMPTY))
    # The pair is bound whole, the text a method gives, each parameter, then the condition's value.
    assert description.values == [(1.5, 2.5), "POINT(1.5, 2.5)", 1, 2, 3]
    assert description.origins[:4] == [
        PlanOrigins.get_value_origin(point, "coordinates"),
        PlanOrigins.get_value_origin(point, "get_text"),
        PlanOrigins.get_value_origin(point, "parameters", 0),
        PlanOrigins.get_value_origin(point, "parameters", 1),
    ]
    # A condition folded into JOINs binds its values optionally.
    assert description.origins[4] == (OPTIONAL_VALUE_ORIGIN, PlanOrigins.get_value_origin(point.condition, "intnum"))
    assert Point((1.5, 2.5), plannable=False).get_plan_description(PlanContext.EMPTY) is None


def test_a_value_of_a_condition_folded_into_no_join_binds_nothing():
    point = Point((1.5, 2.5))
    description = PlanOrigins.describe(lambda: point.get_plan_description(PlanContext.EMPTY))
    references = [(origin, object()) for origin in description.origins[:4]]
    sources = StatementPlanRuns.get_value_sources(description, references)
    assert sources is not None
    assert sources[0][4] == ()
    # A value of a part that isn't optional needs a reference.
    assert StatementPlanRuns.get_value_sources(description, references[1:]) is None


def test_a_slot_of_an_unknown_form_is_rejected():
    class Unknown:
        plan_slots: ClassVar[Any] = (("model", "unknown"),)

    with pytest.raises(TypeError, match="unknown form"):
        QueryKeyCompiler.compile(Unknown, "plan_slots")


def test_declared_slots_generate_the_key():
    class Sliced:
        plan_slots: ClassVar[Any] = (
            ("name", PlanKeyForm.VALUE),
            ("names", PlanKeyForm.SORTED_TUPLE),
            ("limit", PlanKeyForm.BOUND),
            ("offset", PlanKeyForm.BOUND_INTO_ANOTHER),
        )

        def __init__(self, limit: int | None, offset: int | None) -> None:
            self.name = "sliced"
            self.names = {"b", "a"}
            self.limit = limit
            self.offset = offset

    describe = QueryKeyCompiler.compile(Sliced, "plan_slots")
    query = Sliced(5, 10)
    own = describe(query, False)
    assert own.structure == (Sliced, "plan_slots", "sliced", ("a", "b"), True, True)
    # A query of its own binds its OFFSET apart; one built into another binds it too.
    assert own.values == [5]
    assert describe(query, True).values == [5, 10]
    assert describe(Sliced(None, None), False).structure[-2:] == (False, False)
