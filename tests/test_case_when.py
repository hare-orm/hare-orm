import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.exceptions import FieldError
from hare.query.expressions import Case, F, Q, Value, When
from hare.query.functions import Coalesce, Count, Max, Sum
from tests.testmodels import DateFields, Event, IntFields, JSONFields, Tournament


@pytest_asyncio.fixture
async def intfields_data(db):
    """Create IntFields test data."""
    intfields = [await IntFields.objects.create(intnum=val) for val in range(10)]
    return intfields


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_single_when(db, intfields_data):
    category = Case(When(intnum__gte=8, then="big"), default="default")
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    expected_sql = (
        'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN \'big\' ELSE \'default\' END "category" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_multi_when(db, intfields_data):
    category = Case(When(intnum__gte=8, then="big"), When(intnum__lte=2, then="small"), default="default")
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    expected_sql = (
        'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN \'big\' WHEN "intnum"<=2 '
        "THEN 'small' ELSE 'default' END \"category\" FROM \"intfields\""
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_q_object_when(db, intfields_data):
    category = Case(When(Q(intnum__gt=2, intnum__lt=8), then="middle"), default="default")
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    expected_sql = (
        'SELECT "intnum" "intnum",CASE WHEN "intnum">2 AND "intnum"<8 '
        "THEN 'middle' ELSE 'default' END \"category\" FROM \"intfields\""
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_F_then(db, intfields_data):
    category = Case(When(intnum__gte=8, then=F("intnum_null")), default="default")
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    expected_sql = (
        'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN '
        '"intnum_null" ELSE \'default\' END "category" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_AE_then(db, intfields_data):
    # AE: ArithmeticExpression
    category = Case(When(intnum__gte=8, then=F("intnum") + 1), default="default")
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    expected_sql = (
        'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN '
        '"intnum"+1 ELSE \'default\' END "category" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_func_then(db, intfields_data):
    category = Case(When(intnum__gte=8, then=Coalesce("intnum_null", 10)), default="default")
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    expected_sql = (
        'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN '
        'COALESCE("intnum_null",10) ELSE \'default\' END "category" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_F_default(db, intfields_data):
    category = Case(When(intnum__gte=8, then="big"), default=F("intnum_null"))
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    expected_sql = (
        'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN \'big\' '
        'ELSE "intnum_null" END "category" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_AE_default(db, intfields_data):
    # AE: ArithmeticExpression
    category = Case(When(intnum__gte=8, then=8), default=F("intnum") + 1)
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    then_sql = "CAST(8 AS BIGINT)" if db.get_connection().dialect.name == "postgresql" else "8"
    expected_sql = (
        f'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN {then_sql} ELSE "intnum"+1 END "category" '
        'FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_func_default(db, intfields_data):
    category = Case(When(intnum__gte=8, then=8), default=Coalesce("intnum_null", 10))
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)

    then_sql = "CAST(8 AS BIGINT)" if db.get_connection().dialect.name == "postgresql" else "8"
    expected_sql = (
        f'SELECT "intnum" "intnum",CASE WHEN "intnum">=8 THEN {then_sql} ELSE '
        'COALESCE("intnum_null",10) END "category" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_case_when_in_where(db, intfields_data):
    category = Case(When(intnum__gte=8, then="big"), When(intnum__lte=2, then="small"), default="middle")
    sql = (
        IntFields.objects.all()
        .annotate(category=category)
        .filter(category__in=["big", "small"])
        .values("intnum")
        .sql(parameters_inline=True)
    )
    expected_sql = (
        'SELECT "intnum" "intnum" FROM "intfields" WHERE CASE WHEN "intnum">=8 '
        "THEN 'big' WHEN \"intnum\"<=2 THEN 'small' ELSE 'middle' END IN ('big','small')"
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_annotation_in_when_annotation(db, intfields_data):
    sql = (
        IntFields.objects.all()
        .annotate(intnum_plus_1=F("intnum") + 1)
        .annotate(bigger_than_10=Case(When(Q(intnum_plus_1__gte=10), then=True), default=False))
        .values("id", "intnum", "intnum_plus_1", "bigger_than_10")
        .sql(parameters_inline=True)
    )

    expected_sql = (
        'SELECT "id" "id","intnum" "intnum","intnum"+1 "intnum_plus_1",CASE WHEN '
        '"intnum"+1>=10 THEN true ELSE false END "bigger_than_10" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_func_annotation_in_when_annotation(db, intfields_data):
    sql = (
        IntFields.objects.all()
        .annotate(intnum_col=Coalesce("intnum", 0))
        .annotate(is_zero=Case(When(Q(intnum_col=0), then=True), default=False))
        .values("id", "intnum_col", "is_zero")
        .sql(parameters_inline=True)
    )

    expected_sql = (
        'SELECT "id" "id",COALESCE("intnum",0) "intnum_col",CASE WHEN '
        'COALESCE("intnum",0)=0 THEN true ELSE false END "is_zero" FROM "intfields"'
    )
    assert sql == expected_sql


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_case_when_in_group_by(db, intfields_data):
    sql = (
        IntFields.objects.all()
        .annotate(is_zero=Case(When(Q(intnum=0), then=True), default=False))
        .annotate(count=Count("id"))
        .group_by("is_zero")
        .values("is_zero", "count")
        .sql(parameters_inline=True)
    )

    # A column that holds no NULL: a dialect may count the rows instead of reading it.
    counted_sql = "*" if IntFields.get_connection().dialect.name == "postgresql" else '"id"'
    expected_sql = (
        'SELECT CASE WHEN "intnum"=0 THEN true ELSE false END '
        f'"is_zero",COUNT({counted_sql}) "count" FROM "intfields" GROUP BY "is_zero"'
    )
    assert sql == expected_sql


@pytest.mark.asyncio
async def test_unknown_field_in_when_annotation(db, intfields_data):
    with pytest.raises(FieldError, match="Unknown filter param 'unknown'.+"):
        IntFields.objects.all().annotate(intnum_col=Coalesce("intnum", 0)).annotate(
            is_zero=Case(When(Q(unknown=0), then="1"), default="2")
        ).sql(parameters_inline=True)


@pytest.mark.asyncio
async def test_when_condition_on_related_field_joins(db):
    """A When() condition referencing a related field must actually join that field's table -
    dropping the join produced SQL that referenced the joined alias without ever joining it."""
    tournament = await Tournament.objects.create(name="t1")
    event = await Event.objects.create(name="e1", tournament=tournament)

    qs = Event.objects.all().annotate(is_t1=Case(When(tournament__name="t1", then="yes"), default="no"))
    result = await qs.values("event_id", "is_t1")
    assert result == [{"event_id": event.event_id, "is_t1": "yes"}]


@pytest.mark.asyncio
async def test_case_with_aggregate_and_plain_field_branches_forces_group_by(db):
    """A Case/When mixing a real aggregate branch (Count) with a plain-field branch must still
    be flagged as an aggregate expression, so the queryset's auto-GROUP-BY fires. Previously,
    the unanimous vote across branches silently disabled auto-GROUP-BY whenever any branch (here,
    the plain-field default) was not itself an aggregate, collapsing all rows into one group with
    a meaningless value on SQLite and raising a GROUP BY error on Postgres."""
    await IntFields.objects.create(intnum=0)
    await IntFields.objects.create(intnum=0)
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=1)

    category = Case(When(Q(intnum=0), then=Count("id")), default=F("intnum"))
    sql = IntFields.objects.all().values("intnum").annotate(category=category).sql(parameters_inline=True)
    assert "GROUP BY" in sql

    result = await IntFields.objects.all().values("intnum").annotate(category=category)
    assert sorted(result, key=lambda row: row["intnum"]) == [
        {"intnum": 0, "category": 2},
        {"intnum": 1, "category": 1},
    ]


@pytest.mark.asyncio
async def test_case_without_any_aggregate_branch_does_not_force_group_by(db, intfields_data):
    """A Case/When with no aggregate branches at all must still report not-aggregate, so it does
    not spuriously trigger GROUP BY - the reverse case of the mixed-branch bug above."""
    category = Case(When(intnum__gte=8, then="big"), default="default")
    sql = IntFields.objects.all().annotate(category=category).values("intnum", "category").sql(parameters_inline=True)
    assert "GROUP BY" not in sql


# A Case()/When() whose branches are ALL bare literals (no column reference, no other typed
# context anywhere in the expression) has no context for Postgres to infer a type from and used
# to silently default the whole CASE to text - fine on sqlite (untyped), but on Postgres it broke
# asyncpg's bind (DataError: expected str, got <type>) and, on rust_pg, silently round-tripped
# the value through text, coming back as the WRONG PYTHON TYPE (str instead of int/Decimal/etc.)
# with no error at all. These exercise the real bound-parameter execution path (not
# .sql(parameters_inline=True), which bypasses the parameterizer and never bind-executes anything).
@pytest.mark.asyncio
async def test_case_int_branches_execute_with_correct_type(db, intfields_data):
    category = Case(When(intnum__gte=8, then=100), default=200)
    result = await IntFields.objects.filter(intnum=9).annotate(category=category).values("category")
    assert result == [{"category": 100}]
    assert isinstance(result[0]["category"], int)


# The remaining branch types below need requires_features(dialect="postgresql") - sqlite has no
# native Decimal/UUID/date/datetime/bool column types, so a raw, untyped .values() annotation
# (there's no Field to run from_db_value() through - it's a bare CASE expression, not a real
# column) comes back as whatever sqlite's own driver hands over for an unconverted value (text for
# Decimal/UUID/date/datetime, 0/1 for bool) regardless of this fix - that's sqlite's own
# pre-existing, unrelated behavior, not something this fix changes or could fix. Postgres has real
# native types for all of these and returns them correctly typed once the bind itself succeeds,
# which is exactly what this fix makes possible.
@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_case_decimal_branches_execute_with_correct_type(db, intfields_data):
    category = Case(When(intnum__gte=8, then=Decimal("12.50")), default=Decimal("0.00"))
    result = await IntFields.objects.filter(intnum=9).annotate(category=category).values("category")
    assert result == [{"category": Decimal("12.50")}]
    assert isinstance(result[0]["category"], Decimal)


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_case_uuid_branches_execute_with_correct_type(db, intfields_data):
    tag = uuid.uuid4()
    category = Case(When(intnum__gte=8, then=tag), default=uuid.uuid4())
    result = await IntFields.objects.filter(intnum=9).annotate(category=category).values("category")
    assert result == [{"category": tag}]
    assert isinstance(result[0]["category"], uuid.UUID)


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_case_date_branches_execute_with_correct_type(db, intfields_data):
    the_date = date(2024, 10, 27)
    category = Case(When(intnum__gte=8, then=the_date), default=date(2000, 1, 1))
    result = await IntFields.objects.filter(intnum=9).annotate(category=category).values("category")
    assert result == [{"category": the_date}]
    assert isinstance(result[0]["category"], date)


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_case_datetime_branches_execute_with_correct_type(db, intfields_data):
    the_datetime = datetime(2024, 10, 27, 12, 30, tzinfo=timezone.utc)
    category = Case(When(intnum__gte=8, then=the_datetime), default=datetime(2000, 1, 1, tzinfo=timezone.utc))
    result = await IntFields.objects.filter(intnum=9).annotate(category=category).values("category")
    assert result == [{"category": the_datetime}]
    assert isinstance(result[0]["category"], datetime)


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_case_bool_branches_still_execute_with_correct_type(db, intfields_data):
    """Regression guard for the already-fixed bool case, run through the same real
    bound-parameter path as the sibling types above rather than re-testing it in isolation."""
    category = Case(When(intnum__gte=8, then=True), default=False)
    result = await IntFields.objects.filter(intnum=9).annotate(category=category).values("category")
    assert result == [{"category": True}]
    assert isinstance(result[0]["category"], bool)


@pytest.mark.asyncio
async def test_case_when_then_referencing_a_field_decodes_through_that_fields_own_type(db):
    """Case.get_result() used to never set an output_field on its own ExpressionResult at all -
    unconditionally, regardless of branch types - so QuerySet._get_annotate() never had anything
    to decode the annotation's result through (getattr(annotation, "populate_field_object", False)
    was also never set on Case at all). A branch referencing a real field whose from_db_value()
    does real work (a JSONField decoding its stored text back into a dict) came back as the raw,
    undecoded driver value instead."""
    obj = await JSONFields.objects.create(data={"fallback": True}, data_null={"y": 2})

    branch = Case(When(id=obj.id, then=F("data_null")), default=F("data"))
    result = await JSONFields.objects.filter(id=obj.id).annotate(x=branch).values("x")
    assert result == [{"x": {"y": 2}}]
    assert isinstance(result[0]["x"], dict)


@pytest.mark.asyncio
async def test_max_of_case_with_a_fractional_default_keeps_the_fraction(db):
    """Case took the result type of its first branch (the IntField of F("intnum")) and truncated
    a fractional literal default when decoding (2.5 -> 2)."""
    await IntFields.objects.create(intnum=1)
    branch = Case(When(intnum__gt=100, then=F("intnum")), default=Value(2.5))
    assert (await IntFields.objects.all().aggregate(top=Max(branch)))["top"] == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_sum_of_case_mixing_a_field_and_a_fractional_default_keeps_the_fraction(db):
    await IntFields.objects.create(intnum=1)
    await IntFields.objects.create(intnum=5)
    branch = Case(When(intnum=1, then=F("intnum")), default=Value(0.5))
    assert (await IntFields.objects.all().aggregate(total=Sum(branch)))["total"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_case_with_a_fractional_bare_default_keeps_the_fraction_in_annotate(db):
    await IntFields.objects.create(intnum=1)
    branch = Case(When(intnum__gt=100, then=F("intnum")), default=2.5)
    result = await IntFields.objects.all().annotate(x=branch).values_list("x", flat=True)
    assert result == [pytest.approx(2.5)]


@pytest.mark.asyncio
async def test_case_with_an_int_default_still_decodes_through_the_field(db):
    await IntFields.objects.create(intnum=1)
    branch = Case(When(intnum__gt=100, then=F("intnum")), default=Value(7))
    result = await IntFields.objects.all().aggregate(top=Max(branch))
    assert result == {"top": 7}
    assert isinstance(result["top"], int)


@pytest.mark.asyncio
async def test_repeated_case_aggregate_with_a_different_default_type(db):
    await IntFields.objects.create(intnum=1)
    for default, expected in ((7, 7), (2.5, 2.5), (8, 8), (0.25, 1)):
        branch = Case(When(intnum__gt=100, then=F("intnum")), default=Value(default))
        result = await IntFields.objects.all().aggregate(top=Max(branch))
        assert result["top"] == pytest.approx(default)


@pytest.mark.asyncio
async def test_case_value_wrapped_literals_keep_their_type(db):
    for value in (1, 2, 3):
        await IntFields.objects.create(intnum=value)
    flags = Case(When(intnum__gt=1, then=Value(True)), default=Value(False))
    amounts = Case(When(intnum__gt=1, then=Value(Decimal("1.50"))), default=Value(Decimal("0")))
    mixed_scale_amounts = Case(When(intnum=1, then=Value(Decimal("1.5"))), default=Value(Decimal("1.25")))
    rows = await (
        IntFields.objects.annotate(flag=flags, amount=amounts, mixed=mixed_scale_amounts)
        .order_by("intnum")
        .values_list("flag", "amount", "mixed")
    )
    assert rows == [
        (False, Decimal("0.00"), Decimal("1.50")),
        (True, Decimal("1.50"), Decimal("1.25")),
        (True, Decimal("1.50"), Decimal("1.25")),
    ]
    assert all(type(flag) is bool for flag, _, _ in rows)
    assert all(type(amount) is Decimal for _, amount, _ in rows)
    counted = await IntFields.objects.all().aggregate(
        total=Sum(Case(When(intnum__gt=1, then=Value(1)), default=Value(0)))
    )
    assert counted == {"total": 2}
    assert type(counted["total"]) is int


@pytest.mark.asyncio
async def test_case_value_wrapped_date_literals_keep_their_type(db):
    await IntFields.objects.create(intnum=1)
    await DateFields.objects.create(date=date(2020, 1, 1))
    days = Case(When(intnum=1, then=Value(date(2020, 1, 1))), default=Value(date(2021, 1, 1)))
    assert await IntFields.objects.annotate(day=days).values_list("day", flat=True) == [date(2020, 1, 1)]


@pytest.mark.asyncio
async def test_when_negate_negates_all_conditions_together(db):
    await IntFields.objects.create(intnum=1, intnum_null=1)
    await IntFields.objects.create(intnum=1, intnum_null=2)
    await IntFields.objects.create(intnum=2, intnum_null=1)
    negated = Case(When(negate=True, intnum=1, intnum_null=1, then=1), default=0)
    negated_q_objects = Case(When(Q(intnum=1), Q(intnum_null=1), negate=True, then=1), default=0)
    rows = (
        await IntFields.objects.annotate(kwargs_flag=negated, q_flag=negated_q_objects)
        .order_by("id")
        .values_list("kwargs_flag", "q_flag")
    )
    assert rows == [(0, 0), (1, 1), (1, 1)]
    assert await IntFields.objects.filter(~Q(intnum=1, intnum_null=1)).count() == 2
