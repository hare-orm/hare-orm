import datetime
import uuid
from decimal import Decimal

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import (
    FieldError,
    QueryError,
)
from hare.query.expressions import ArithmeticOperator, Case, F, OuterRef, Q, Subquery, When
from hare.query.functions import Coalesce, Count, Length, Lower, Sum
from tests import testmodels
from tests.testmodels import Currency, Event, ExpressionTypeRow, JSONFields, Tournament


def test_arithmetic():
    """Test F expression arithmetic operations."""
    f = F("name")

    negated = -f
    assert negated.connector == ArithmeticOperator.MUL
    assert negated.right.value == -1

    added = f + 1
    assert added.connector == ArithmeticOperator.ADD
    assert added.right.value == 1

    radded = 1 + f
    assert radded.connector == ArithmeticOperator.ADD
    assert radded.left.value == 1
    assert radded.right == f

    subbed = f - 1
    assert subbed.connector == ArithmeticOperator.SUB
    assert subbed.right.value == 1

    rsubbed = 1 - f
    assert rsubbed.connector == ArithmeticOperator.SUB
    assert rsubbed.left.value == 1

    mulled = f * 2
    assert mulled.connector == ArithmeticOperator.MUL
    assert mulled.right.value == 2

    rmulled = 2 * f
    assert rmulled.connector == ArithmeticOperator.MUL
    assert rmulled.left.value == 2

    divved = f / 2
    assert divved.connector == ArithmeticOperator.DIV
    assert divved.right.value == 2

    rdivved = 2 / f
    assert rdivved.connector == ArithmeticOperator.DIV
    assert rdivved.left.value == 2

    powed = f**2
    assert powed.connector == ArithmeticOperator.POW
    assert powed.right.value == 2

    rpowed = 2**f
    assert rpowed.connector == ArithmeticOperator.POW
    assert rpowed.left.value == 2

    modded = f % 2
    assert modded.connector == ArithmeticOperator.MOD
    assert modded.right.value == 2

    rmodded = 2 % f
    assert rmodded.connector == ArithmeticOperator.MOD
    assert rmodded.left.value == 2


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values_with_json_field_attribute(db):
    """Test F expression with JSON field attribute."""
    await JSONFields.objects.create(data={"attribute": 1})
    res = await JSONFields.objects.annotate(attribute=F("data__attribute")).first()
    assert int(res.attribute) == 1


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values_with_json_field_attribute_of_attribute(db):
    """Test F expression with nested JSON field attribute."""
    await JSONFields.objects.create(data={"attribute": {"subattribute": "value"}})
    res = await JSONFields.objects.annotate(subattribute=F("data__attribute__subattribute")).first()
    assert res.subattribute == "value"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values_with_json_field_str_array_element(db):
    """Test F expression with JSON field string array element."""
    await JSONFields.objects.create(data=["a", "b", "c"])
    res = await JSONFields.objects.annotate(array_element=F("data__0")).first()
    assert res.array_element == "a"
    res = await JSONFields.objects.annotate(array_element=F("data__1")).first()
    assert res.array_element == "b"
    res = await JSONFields.objects.annotate(array_element=F("data__2")).first()
    assert res.array_element == "c"
    res = await JSONFields.objects.annotate(array_element=F("data__3")).first()
    assert res.array_element is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values_with_json_field_array_attribute(db):
    """Test F expression with JSON field array attribute."""
    await JSONFields.objects.create(data={"array": ["a", "b", "c"]})
    res = await JSONFields.objects.annotate(array_attribute=F("data__array__0")).first()
    assert res.array_attribute == "a"
    res = await JSONFields.objects.annotate(array_attribute=F("data__array__1")).first()
    assert res.array_attribute == "b"
    res = await JSONFields.objects.annotate(array_attribute=F("data__array__2")).first()
    assert res.array_attribute == "c"


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_values_with_json_field_int_array_element(db):
    """
    Test F expression with JSON field integer array element.

    Among the supported dialects, only SQLite will return the correct type.
    """
    await JSONFields.objects.create(data=[1, 2, 3])
    res = await JSONFields.objects.annotate(array_element=F("data__0")).first()
    assert int(res.array_element) == 1
    res = await JSONFields.objects.annotate(array_element=F("data__1")).first()
    assert int(res.array_element) == 2
    res = await JSONFields.objects.annotate(array_element=F("data__2")).first()
    assert int(res.array_element) == 3
    res = await JSONFields.objects.annotate(array_element=F("data__3")).first()
    assert res.array_element is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_filter_with_json_field_attribute(db):
    """Test F expression filter with JSON field attribute."""
    exp = await JSONFields.objects.create(data={"attribute": "a"})
    res = await JSONFields.objects.annotate(attribute=F("data__attribute")).filter(attribute="a").first()
    assert res.id == exp.id
    res = await JSONFields.objects.annotate(attribute=F("data__attribute")).filter(attribute="b").first()
    assert res is None


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_filter_with_json_field_attribute_of_attribute(db):
    """Test F expression filter with nested JSON field attribute."""
    exp = await JSONFields.objects.create(data={"attribute": {"subattribute": "value"}})
    res = (
        await JSONFields.objects.annotate(subattribute=F("data__attribute__subattribute"))
        .filter(subattribute="value")
        .first()
    )
    assert res.id == exp.id


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_filter_with_json_field_str_array_element(db):
    """Test F expression filter with JSON field string array element."""
    exp = await JSONFields.objects.create(data=["a", "b", "c"])
    res = await JSONFields.objects.annotate(array_element=F("data__0")).filter(array_element="a").first()
    assert res.id == exp.id
    res = await JSONFields.objects.annotate(array_element=F("data__1")).filter(array_element="b").first()
    assert res.id == exp.id


# ============================================================================
# F() on a plain field, in a query that also joins another table with a same-named column -
# F.get_result()'s "regular model field" branch used to build a bare, unqualified HareSqlField(column)
# instead of table[column]. Harmless on its own (an unqualified reference is unambiguous SQL when
# nothing else in the query has a column by that name), but Event and Tournament both have a
# "name" column - the moment a join to Tournament entered the same query for any reason, F("name")
# meaning Event's own name collided with Tournament's, raising a DB-level "ambiguous column" error
# even though the code always meant one specific field.
# ============================================================================


@pytest.mark.asyncio
async def test_f_on_plain_field_qualified_against_joined_table_name_collision(db):
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="E1", tournament=tournament)

    # the join comes from filtering through the relation, not from F() itself
    rows = await Event.objects.filter(tournament__name="T1").annotate(x=F("name")).values("name", "x")

    assert rows == [{"name": "E1", "x": "E1"}]


# ============================================================================
# Mixed int/float (and int/Decimal) arithmetic on an integer-family F() expression must not
# silently truncate the fractional part - a bare Python literal combined with an IntField/
# BigIntField/SmallIntField used to leave the literal's bind parameter type unspecified,
# which Postgres then resolved to match the int-family column instead of the literal's own
# real type (confirmed against real asyncpg: SELECT n_int + $1 FROM t infers $1 as int4/int8/
# int2 - asyncpg then truncates a float/Decimal value silently when encoding it into that
# integer parameter). .annotate(...).values(...) is used (not .update() into an int column)
# because it reads back the raw, uncoerced arithmetic result instead of routing it back through
# IntField.from_db_value(), which would re-truncate a correct float result to an int in
# Python itself and mask the bug this guards against.
# ============================================================================


@pytest.mark.asyncio
async def test_int_field_plus_float_literal_returns_float(db):
    await testmodels.IntFields.objects.create(intnum=7)
    rows = await testmodels.IntFields.objects.annotate(result=F("intnum") + 0.5).values("result")
    assert rows == [{"result": 7.5}]
    assert isinstance(rows[0]["result"], float)


@pytest.mark.asyncio
async def test_int_field_minus_float_literal_returns_float(db):
    await testmodels.IntFields.objects.create(intnum=7)
    rows = await testmodels.IntFields.objects.annotate(result=F("intnum") - 0.5).values("result")
    assert rows == [{"result": 6.5}]
    assert isinstance(rows[0]["result"], float)


@pytest.mark.asyncio
async def test_int_field_times_float_literal_returns_float(db):
    await testmodels.IntFields.objects.create(intnum=7)
    rows = await testmodels.IntFields.objects.annotate(result=F("intnum") * 0.5).values("result")
    assert rows == [{"result": 3.5}]
    assert isinstance(rows[0]["result"], float)


@pytest.mark.asyncio
async def test_int_field_divided_by_float_literal_returns_float(db):
    await testmodels.IntFields.objects.create(intnum=7)
    rows = await testmodels.IntFields.objects.annotate(result=F("intnum") / 0.5).values("result")
    assert rows == [{"result": 14.0}]
    assert isinstance(rows[0]["result"], float)


@pytest.mark.asyncio
async def test_negative_int_field_plus_float_literal_returns_float(db):
    """Truncation-toward-zero (what asyncpg's broken int4 codec silently did) gives a different,
    also-wrong answer than floor() would for a negative base value - -6.5 truncates to -6, not
    -7 - so a negative base value distinguishes "still silently truncating" from "actually
    fixed" in a way a positive-only case cannot."""
    await testmodels.IntFields.objects.create(intnum=-7)
    rows = await testmodels.IntFields.objects.annotate(result=F("intnum") + 0.5).values("result")
    assert rows == [{"result": -6.5}]
    assert isinstance(rows[0]["result"], float)


@pytest.mark.asyncio
async def test_big_int_field_plus_float_literal_returns_float(db):
    await testmodels.BigIntFields.objects.create(intnum=7)
    rows = await testmodels.BigIntFields.objects.annotate(result=F("intnum") + 0.5).values("result")
    assert rows == [{"result": 7.5}]
    assert isinstance(rows[0]["result"], float)


@pytest.mark.asyncio
async def test_small_int_field_plus_float_literal_returns_float(db):
    await testmodels.SmallIntFields.objects.create(smallintnum=7)
    rows = await testmodels.SmallIntFields.objects.annotate(result=F("smallintnum") + 0.5).values("result")
    assert rows == [{"result": 7.5}]
    assert isinstance(rows[0]["result"], float)


@pytest.mark.asyncio
async def test_int_field_plus_decimal_literal_returns_fractional_value(db):
    """Same root cause as the float case above - a bare Decimal literal also has no
    output_field, so its bind parameter was also left to resolve against the int-family
    column's own type, and asyncpg's int4 codec silently truncated a Decimal input the same
    way it does a float one (confirmed empirically: `int4 + Decimal('0.5')` bind parameter
    resolves to int4 and comes back as the truncated int 7, not 7.5).

    Which concrete Python type comes back (Decimal on Postgres, float on SQLite - SQLite has
    no native Decimal storage class) is a legitimate per-backend difference, not part of the
    bug - what must hold on every backend is that the fractional part survives instead of
    silently truncating to the plain int 7.
    """
    await testmodels.IntFields.objects.create(intnum=7)
    rows = await testmodels.IntFields.objects.annotate(result=F("intnum") + Decimal("0.5")).values("result")
    assert rows[0]["result"] == Decimal("7.5")
    assert not isinstance(rows[0]["result"], int)


# ============================================================================
# F("relation__field") used directly as a filter value (not inside annotate()) dropped its own
# JOIN entirely - Q._get_actual_filter_params() read only `.term` off the Expression's
# get_result(), discarding `.joins`, so the generated SQL referenced a table never actually
# joined ("missing FROM-clause entry"/"no such column").
# ============================================================================


@pytest.mark.asyncio
async def test_f_relation_field_as_filter_value_joins_the_relation(db):
    tournament = await Tournament.objects.create(name="T1")
    matching = await Event.objects.create(name="T1", tournament=tournament)
    await Event.objects.create(name="Other", tournament=tournament)

    result = await Event.objects.filter(name=F("tournament__name"))

    assert [event.pk for event in result] == [matching.pk]


@pytest.mark.asyncio
async def test_f_relation_field_as_filter_value_with_gt(db):
    tournament = await Tournament.objects.create(name="M")
    await Event.objects.create(name="A", tournament=tournament)
    greater = await Event.objects.create(name="Z", tournament=tournament)

    result = await Event.objects.filter(name__gt=F("tournament__name"))

    assert [event.pk for event in result] == [greater.pk]


@pytest.mark.asyncio
async def test_lower_f_relation_field_as_filter_value_joins_the_relation(db):
    tournament = await Tournament.objects.create(name="t1")
    matching = await Event.objects.create(name="t1", tournament=tournament)
    await Event.objects.create(name="T1", tournament=tournament)

    result = await Event.objects.filter(name=Lower(F("tournament__name")))

    assert [event.pk for event in result] == [matching.pk]


@pytest.mark.asyncio
async def test_f_relation_field_as_filter_value_inside_q_or(db):
    tournament = await Tournament.objects.create(name="T1")
    matching = await Event.objects.create(name="T1", tournament=tournament)
    other = await Event.objects.create(name="Other", tournament=tournament)

    result = await Event.objects.filter(Q(name=F("tournament__name")) | Q(pk=other.pk))

    assert sorted(event.pk for event in result) == sorted([matching.pk, other.pk])


@pytest.mark.asyncio
async def test_f_relation_field_as_filter_value_inside_exclude(db):
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="T1", tournament=tournament)
    other = await Event.objects.create(name="Other", tournament=tournament)

    result = await Event.objects.exclude(name=F("tournament__name"))

    assert [event.pk for event in result] == [other.pk]


@pytest.mark.asyncio
async def test_f_relation_field_as_filter_value_inside_get(db):
    tournament = await Tournament.objects.create(name="T1")
    matching = await Event.objects.create(name="T1", tournament=tournament)

    found = await Event.objects.get(name=F("tournament__name"))

    assert found.pk == matching.pk


@pytest.mark.asyncio
async def test_f_expression_inside_in_list_raises_configuration_error(db):
    """An Expression can't be resolved into a Term (with its own join threaded into the query)
    from inside a plain value_encoder - each __in/__not_in element only ever gets
    field.to_db_value() called on it directly. A CharField's to_db_value() would otherwise
    silently stringify the Expression object itself (matching nothing); this raises a clear,
    actionable error instead."""
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="T1", tournament=tournament)

    with pytest.raises(QueryError, match="Expression"):
        await Event.objects.filter(name__in=[F("tournament__name")])


@pytest.mark.asyncio
async def test_f_expression_inside_not_in_list_raises_configuration_error(db):
    tournament = await Tournament.objects.create(name="T1")
    await Event.objects.create(name="T1", tournament=tournament)

    with pytest.raises(QueryError, match="Expression"):
        await Event.objects.filter(name__not_in=[F("tournament__name")])


F_COLUMN_DECODING_CASES = [
    (testmodels.DecimalFields, "decimal", {"decimal": Decimal("1.2345"), "decimal_nodec": 1}),
    (testmodels.DatetimeFields, "datetime", {"datetime": datetime.datetime(2024, 5, 6, 7, 8, 9, tzinfo=datetime.UTC)}),
    (testmodels.DateFields, "date", {"date": datetime.date(2024, 5, 6)}),
    (testmodels.TimeDeltaFields, "timedelta", {"timedelta": datetime.timedelta(days=1, seconds=5)}),
    (testmodels.UUIDFields, "data", {"data": uuid.UUID("8f5f2cbd-7a2f-4c39-8d4c-2c8f6f7f4a11")}),
    (testmodels.BooleanFields, "boolean", {"boolean": True}),
    (testmodels.EnumFields, "service", {"service": testmodels.Service.database_design}),
    (testmodels.EnumFields, "currency", {"service": testmodels.Service.database_design, "currency": Currency.EUR}),
    (JSONFields, "data", {"data": {"nested": {"a": [1, 2]}, "flag": True}}),
    (JSONFields, "data_null", {"data": {}, "data_null": [1, "two"]}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("model", "field_name", "create_kwargs"), F_COLUMN_DECODING_CASES)
async def test_bare_f_annotation_decodes_like_the_column(db, model, field_name, create_kwargs):
    """A bare F() over a model column is decoded through the field, exactly like .values()."""
    await model.objects.create(**create_kwargs)
    expected = (await model.objects.all().values_list(field_name, flat=True))[0]
    assert expected is not None

    annotated = model.objects.annotate(copied=F(field_name))
    assert (await annotated.values_list("copied", flat=True)) == [expected]
    assert (await annotated.values("copied")) == [{"copied": expected}]
    instance = await annotated.first()
    assert instance.copied == expected
    assert type(instance.copied) is type(expected)
    assert type((await annotated.values_list("copied", flat=True))[0]) is type(expected)
    # Unchanged on the query-shape cache's second, cache-hitting execution.
    assert type((await annotated.values_list("copied", flat=True))[0]) is type(expected)

    union_instances = await annotated.union(model.objects.annotate(copied=F(field_name)), all=True)
    assert [union_instance.copied for union_instance in union_instances] == [expected, expected]
    assert {type(union_instance.copied) for union_instance in union_instances} == {type(expected)}

    chained = (
        await model.objects.annotate(copied=F(field_name)).annotate(again=F("copied")).values_list("again", flat=True)
    )
    assert chained == [expected]


@pytest.mark.asyncio
async def test_bare_f_annotation_through_relation_decodes(db):
    """F("relation__field") and F("relation") decode through the related model's fields."""
    tournament = await Tournament.objects.create(name="Cup", desc="d")
    event = await Event.objects.create(name="Final", tournament=tournament)

    queryset = Event.objects.filter(pk=event.pk).annotate(
        tournament_created=F("tournament__created"), tournament_key=F("tournament")
    )
    expected_created = (await Tournament.objects.filter(pk=tournament.pk).values_list("created", flat=True))[0]
    row = (await queryset.values("tournament_created", "tournament_key"))[0]
    assert row == {"tournament_created": expected_created, "tournament_key": tournament.pk}
    assert isinstance(row["tournament_created"], datetime.datetime)

    instance = await queryset.select_related("tournament").first()
    assert instance.tournament_created == expected_created
    assert instance.tournament.name == "Cup"

    subquery_value = (
        await Tournament.objects.filter(pk=tournament.pk)
        .annotate(
            latest_event_tournament_created=Subquery(
                Event.objects.filter(tournament_id=OuterRef("pk"))
                .annotate(copied=F("tournament__created"))
                .limit(1)
                .values("copied")
            )
        )
        .values_list("latest_event_tournament_created", flat=True)
    )
    assert subquery_value == [expected_created]


@pytest.mark.asyncio
async def test_f_reference_to_annotation_keeps_annotation_value_type(db):
    """F() naming an annotation is decoded as that annotation's own value, not its argument's field."""
    await Tournament.objects.create(name="Cup", desc="d")
    lengths = await Tournament.objects.annotate(name_length=Length("name")).annotate(copied=F("name_length"))
    assert lengths[0].copied == 3
    assert await Tournament.objects.annotate(name_length=Length("name")).annotate(copied=F("name_length")).values_list(
        "copied", flat=True
    ) == [3]


@pytest.mark.asyncio
async def test_pk_alias_in_f_and_outer_ref(db):
    """ "pk" names the primary key in F(), F("relation__pk") and OuterRef(), as in .filter()."""
    tournament = await Tournament.objects.create(name="Cup", desc="d")
    await Event.objects.create(name="Final", tournament=tournament)
    assert await Tournament.objects.annotate(key=F("pk")).values_list("key", flat=True) == [tournament.pk]
    assert await Event.objects.annotate(key=F("tournament__pk")).values_list("key", flat=True) == [tournament.pk]
    assert await Tournament.objects.annotate(
        event_name=Subquery(Event.objects.filter(tournament_id=OuterRef("pk")).values("name"))
    ).values_list("event_name", flat=True) == ["Final"]


@pytest.mark.asyncio
async def test_union_decodes_annotation_through_its_field(db):
    """A union's instances decode an annotation like a plain query's do."""
    await testmodels.DecimalFields.objects.create(decimal=Decimal("1.5"), decimal_nodec=1)
    branch = testmodels.DecimalFields.objects.annotate(amount=Coalesce("decimal_null", F("decimal")))
    union_instances = await branch.union(
        testmodels.DecimalFields.objects.annotate(amount=Coalesce("decimal_null", F("decimal")))
    )
    assert [union_instance.amount for union_instance in union_instances] == [Decimal("1.5000")]
    assert isinstance(union_instances[0].amount, Decimal)
    mixed_instances = await branch.union(testmodels.DecimalFields.objects.annotate(amount=F("id")), all=True)
    assert len(mixed_instances) == 2


@pytest.mark.asyncio
async def test_arithmetic_on_combined_expressions_functions_and_case(db):
    await testmodels.IntFields.objects.create(id=1, intnum=10)
    row = await testmodels.IntFields.objects.annotate(
        doubled=(F("intnum") + 1) * 2,
        negated_plus_one=-F("intnum") + 1,
        reflected=100 - (F("intnum") - 1),
        coalesce_plus_one=Coalesce("intnum_null", 0) + 1,
        case_plus_one=Case(When(intnum__gt=5, then=1), default=0) + 1,
        case_times_field=Case(When(intnum__gt=5, then=2), default=1) * F("intnum"),
        field_plus_fractional_case=F("intnum") + Case(When(intnum__gt=5, then=0.5), default=0),
    ).values(
        "doubled",
        "negated_plus_one",
        "reflected",
        "coalesce_plus_one",
        "case_plus_one",
        "case_times_field",
        "field_plus_fractional_case",
    )
    assert row[0] == {
        "doubled": 22,
        "negated_plus_one": -9,
        "reflected": 91,
        "coalesce_plus_one": 1,
        "case_plus_one": 2,
        "case_times_field": 20,
        "field_plus_fractional_case": 10.5,
    }
    assert await testmodels.IntFields.objects.all().aggregate(total=Sum("intnum") + 1) == {"total": 11}

    await testmodels.IntFields.objects.all().update(intnum=(F("intnum") + 1) * 2)
    assert await testmodels.IntFields.objects.all().values_list("intnum", flat=True) == [22]


@pytest.mark.asyncio
async def test_arithmetic_on_function_result(db):
    await testmodels.CharFields.objects.create(id=1, char="abc")
    assert await testmodels.CharFields.objects.annotate(doubled_length=Length("char") * 2).values_list(
        "doubled_length", flat=True
    ) == [6]


@pytest.mark.asyncio
async def test_integer_modulo_of_compound_operand(db):
    await testmodels.IntFields.objects.create(id=1, intnum=10)
    remainders = await testmodels.IntFields.objects.annotate(
        by_difference=F("intnum") % (F("intnum") - 3),
        by_product=F("intnum") % (F("intnum") * F("intnum")),
    ).values_list("by_difference", "by_product")
    assert remainders == [(3, 10)]


@pytest.mark.asyncio
async def test_computed_numbers_combine_with_any_numeric_column(db):
    await ExpressionTypeRow.objects.create(
        num=3,
        fl=1.5,
        dec=Decimal("1.10"),
        dec3=Decimal("2.125"),
        d=datetime.date(2020, 1, 2),
        dt=datetime.datetime(2020, 1, 2, tzinfo=datetime.UTC),
        td=datetime.timedelta(seconds=2),
        s="abc",
        flag=True,
    )
    with pytest.raises(FieldError, match="different field type"):
        await ExpressionTypeRow.objects.annotate(c=F("dec") + F("fl")).values_list("c", flat=True)
    with pytest.raises(FieldError, match="different field type"):
        await ExpressionTypeRow.objects.annotate(c=F("num") + F("s")).values_list("c", flat=True)
    integer_with_fraction = await ExpressionTypeRow.objects.annotate(
        num_plus_decimal=F("num") + F("dec"), float_times_num=F("fl") * F("num")
    ).values("num_plus_decimal", "float_times_num")
    assert integer_with_fraction == [{"num_plus_decimal": Decimal("4.10"), "float_times_num": 4.5}]
    values = await ExpressionTypeRow.objects.annotate(
        length_plus_decimal=Length("s") + F("dec"),
        count_times_float=Count("id") * F("fl"),
        duration_times_two=F("td") * 2,
        duration_plus_count=F("td") + Count("id"),
    ).values("length_plus_decimal", "count_times_float", "duration_times_two", "duration_plus_count")
    assert values == [
        {
            "length_plus_decimal": Decimal("4.10"),
            "count_times_float": 1.5,
            "duration_times_two": 4_000_000,
            "duration_plus_count": 2_000_001,
        }
    ]
    assert [type(value) for value in values[0].values()] == [Decimal, float, int, int]
    assert str(values[0]["length_plus_decimal"]) == "4.10"
