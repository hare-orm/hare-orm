from decimal import Decimal

import pytest

from hare import fields
from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError, FieldError, ValidationError
from hare.query.expressions import F
from hare.query.functions import Avg, Max, Min, Sum
from tests import testmodels


def test_max_digits_empty():
    with pytest.raises(
        TypeError,
        match="missing 2 required positional arguments: 'max_digits' and 'decimal_places'",
    ):
        fields.DecimalField()  # pylint: disable=E1120


def test_decimal_places_empty():
    with pytest.raises(TypeError, match="missing 1 required positional argument: 'decimal_places'"):
        fields.DecimalField(max_digits=1)  # pylint: disable=E1120


def test_max_fields_bad():
    with pytest.raises(ConfigurationError, match="'max_digits' must be >= 1"):
        fields.DecimalField(max_digits=0, decimal_places=2)


def test_decimal_places_bad():
    with pytest.raises(ConfigurationError, match="'decimal_places' must be >= 0"):
        fields.DecimalField(max_digits=2, decimal_places=-1)


def test_decimal_places_exceeding_max_digits_bad():
    """DecimalField.__init__ validated max_digits>=1 and decimal_places>=0 independently, but
    never that decimal_places <= max_digits - DECIMAL(3,5) (fewer total digits than its own
    fractional digits) is nonsensical, undefined-behavior SQL."""
    with pytest.raises(ConfigurationError, match="'decimal_places' must be <= 'max_digits'"):
        fields.DecimalField(max_digits=3, decimal_places=5)


def test_to_db_value_wraps_type_coercion_failure():
    """DecimalField doesn't override to_db_value - uses the base Field.to_db_value's own
    `self.field_type(value)` coercion, i.e. `Decimal(value)`. A malformed string raises
    decimal.InvalidOperation, NOT ValueError/TypeError (InvalidOperation's own MRO is
    ArithmeticError, not ValueError) - a DIFFERENT exception type than IntField's own coercion
    failure (test_int.py's test_base_field_to_db_value_wraps_type_coercion_failure), needing its
    own coverage to confirm the base fix's except clause actually catches it too."""
    with pytest.raises(ValidationError):
        fields.DecimalField(max_digits=10, decimal_places=2).to_db_value("not-a-decimal", None)


def test_to_python_value_wraps_type_coercion_failure():
    """from_db_value() - the path a value read straight from the DB goes through - had the
    exact same unwrapped Decimal(value) coercion as to_db_value() above (test_to_db_value_wraps_
    type_coercion_failure), but was never fixed alongside it."""
    with pytest.raises(ValidationError):
        fields.DecimalField(max_digits=10, decimal_places=2).from_db_value("not-a-decimal")


def test_max_digits_enforced_on_construction():
    """max_digits was only checked at field-declaration time (max_digits >= 1 etc.), never
    against actual values - a value with more significant digits than max_digits used to sail
    through from_db_value/to_db_value with zero errors."""
    field = fields.DecimalField(max_digits=3, decimal_places=0)
    with pytest.raises(ValidationError, match="more than max_digits=3"):
        field.to_db_value(field.from_db_value("1234567"), None)


def test_max_digits_enforced_after_quantize_pads_decimal_places():
    """decimal_places is enforced via quantize() (which pads/rounds to exactly decimal_places),
    but that alone doesn't stop the WHOLE part from exceeding max_digits - decimal_places=2 padding
    onto an already-too-large whole part must still be caught by max_digits."""
    field = fields.DecimalField(max_digits=5, decimal_places=2)
    with pytest.raises(ValidationError, match="more than max_digits=5"):
        field.to_db_value(field.from_db_value("1234.5"), None)


def test_max_digits_allows_value_within_bounds():
    field = fields.DecimalField(max_digits=5, decimal_places=2)
    value = field.from_db_value("123.45")
    assert field.to_db_value(value, None) == Decimal("123.45")


def test_max_digits_reaches_pydantic_constraints():
    """DecimalField didn't override `constraints` (base Field.constraints returns {}), so
    max_digits/decimal_places never reached the generated pydantic schema either."""
    field = fields.DecimalField(max_digits=6, decimal_places=2)
    assert field.constraints == {"max_digits": 6, "decimal_places": 2}


@pytest.mark.asyncio
async def test_create_rejects_value_exceeding_max_digits(db):
    with pytest.raises(ValidationError, match="more than max_digits"):
        await testmodels.DecimalFields.objects.create(decimal=Decimal("123456789012345.6789"), decimal_nodec=1)


@pytest.mark.asyncio
async def test_empty(db):
    """Now raises ValidationError, not IntegrityError - MaxDigitsValidator (like CharField's
    MaxLengthValidator/IntField's MinValueValidator/MaxValueValidator, see their own None checks)
    rejects None before the value ever reaches the DB, same convention as CharFields' own
    test_empty in tests/fields/test_char.py."""
    with pytest.raises(ValidationError):
        await testmodels.DecimalFields.objects.create()


@pytest.mark.asyncio
async def test_create(db):
    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.23456"), decimal_nodec=18.7)
    obj = await testmodels.DecimalFields.objects.get(id=obj0.id)
    assert obj.decimal == Decimal("1.2346")
    assert obj.decimal_nodec == 19
    assert obj.decimal_null is None
    await obj.save()
    obj2 = await testmodels.DecimalFields.objects.get(id=obj.id)
    assert obj == obj2


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_reading_malformed_raw_value_raises_validation_error(db):
    """Garbage written to the column outside the ORM (hand-written SQL, a since-changed column
    type) used to surface as a raw decimal.InvalidOperation on read, not the framework's own
    catchable ValidationError - and, with the optional rust.hydrate accelerator installed, its
    own bucket-6 fast path called Decimal(value).quantize(...) directly and let the raw error
    through even after from_db_value itself was fixed, since it never calls from_db_value on
    its happy path. This exercises whichever path (Rust fast path or pure Python) is active.

    Sqlite-only: the column is a real NUMERIC on Postgres, which rejects a non-numeric literal
    at the raw UPDATE itself, so this specific malformed-on-read scenario can't be constructed
    there through plain SQL."""
    from hare.core.connections import Connections

    obj = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.2345"), decimal_nodec=1)
    alias = next(iter(Connections.current().db_config))
    connection = Connections.get(alias)
    # execute(), not execute_script() - executescript() implicitly commits, which would
    # escape the db fixture's transaction-rollback isolation and leak this corrupted row into
    # every other test in this file.
    await connection.execute("UPDATE decimalfields SET decimal = 'not-a-decimal' WHERE id = ?", [obj.id])
    with pytest.raises(ValidationError):
        await testmodels.DecimalFields.objects.get(id=obj.id)


@pytest.mark.asyncio
async def test_update(db):
    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.23456"), decimal_nodec=18.7)
    await testmodels.DecimalFields.objects.filter(id=obj0.id).update(decimal=Decimal("2.345"))
    obj = await testmodels.DecimalFields.objects.get(id=obj0.id)
    assert obj.decimal == Decimal("2.345")
    assert obj.decimal_nodec == 19
    assert obj.decimal_null is None


@pytest.mark.asyncio
async def test_update_with_float_value_does_not_lose_quantization(db):
    """QuerySet.update() calls to_db_value() directly, bypassing from_db_value() entirely - an
    unquantized Decimal(5.1) inherits float's own binary-representation noise (50+ digits),
    which used to make MaxDigitsValidator spuriously reject a value well within max_digits."""
    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.filter(id=obj0.id).update(decimal=5.1)
    obj = await testmodels.DecimalFields.objects.get(id=obj0.id)
    assert obj.decimal == Decimal("5.1000")


@pytest.mark.asyncio
async def test_direct_assignment_with_float_value_does_not_lose_quantization(db):
    """A plain `obj.field = value` bypasses from_db_value() the same way .update() does -
    save() then reaches to_db_value() directly with the same unquantized-float risk."""
    obj = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.0"), decimal_nodec=1)
    obj.decimal = 5.1
    await obj.save()
    refreshed = await testmodels.DecimalFields.objects.get(id=obj.id)
    assert refreshed.decimal == Decimal("5.1000")


@pytest.mark.asyncio
async def test_filter(db):
    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.23456"), decimal_nodec=18.7)
    obj = await testmodels.DecimalFields.objects.filter(decimal=Decimal("1.2346")).first()
    assert obj == obj0
    obj = await testmodels.DecimalFields.objects.annotate(d=F("decimal")).filter(d=Decimal("1.2346")).first()
    assert obj == obj0
    objs = await testmodels.DecimalFields.objects.filter(decimal_nodec__gt=2).all()
    assert obj in objs
    objs = await testmodels.DecimalFields.objects.filter(decimal_nodec__lt=100).all()
    assert obj in objs


@pytest.mark.asyncio
async def test_f_expression_update(db):
    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.23456"), decimal_nodec=18.7)
    await type(obj0).objects.filter(id=obj0.id).update(decimal=F("decimal") + Decimal("1"))
    obj1 = await testmodels.DecimalFields.objects.get(id=obj0.id)
    assert obj1.decimal == Decimal("2.2346")
    await type(obj0).objects.filter(id=obj0.id).update(decimal=Decimal("1") - F("decimal"))
    obj1 = await testmodels.DecimalFields.objects.get(id=obj0.id)
    assert obj1.decimal == Decimal("-1.2346")


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_f_expression_update_quantizes_stored_value_on_sqlite(db):
    """SQLite stores a DecimalField as text and runs F()-expression arithmetic on it as
    double-precision CAST(... AS NUMERIC) - the raw arithmetic result used to be written back to
    the column with more digits than decimal_places, instead of being quantized the same way a
    plain (non-expression) write already is. A plain model read already re-quantizes on the way
    out regardless (from_db_value()), masking the stale column value - this checks the actual
    stored text directly."""
    from hare.core.connections import Connections

    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("0.1000"), decimal_nodec=1)
    await type(obj0).objects.filter(id=obj0.id).update(decimal=F("decimal") + Decimal("0.00007"))
    alias = next(iter(Connections.current().db_config))
    connection = Connections.get(alias)
    _, rows = await connection.execute("SELECT decimal FROM decimalfields WHERE id = ?", [obj0.id])
    assert Decimal(dict(rows[0])["decimal"]) == Decimal("0.1001")


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_f_expression_save_quantizes_stored_value_on_sqlite(db):
    """Same gap as the QuerySet.update() case above, for the single-row Model.save() path
    (instance.field = F(...); await instance.save()) - checks the actual stored text. The
    in-memory instance attribute stays the unresolved F() expression, same as save()'s existing
    contract for any Expression-valued field (only refresh_from_db() pulls the real value back)."""
    from hare.core.connections import Connections

    obj = await testmodels.DecimalFields.objects.create(decimal=Decimal("0.1000"), decimal_nodec=1)
    obj.decimal = F("decimal") + Decimal("0.00007")
    await obj.save()
    alias = next(iter(Connections.current().db_config))
    connection = Connections.get(alias)
    _, rows = await connection.execute("SELECT decimal FROM decimalfields WHERE id = ?", [obj.id])
    assert Decimal(dict(rows[0])["decimal"]) == Decimal("0.1001")


@pytest.mark.asyncio
async def test_values(db):
    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.23456"), decimal_nodec=18.7)
    values = await testmodels.DecimalFields.objects.get(id=obj0.id).values("decimal", "decimal_nodec")
    assert values["decimal"] == Decimal("1.2346")
    assert values["decimal_nodec"] == 19


@pytest.mark.asyncio
async def test_values_list(db):
    obj0 = await testmodels.DecimalFields.objects.create(decimal=Decimal("1.23456"), decimal_nodec=18.7)
    values = await testmodels.DecimalFields.objects.get(id=obj0.id).values_list("decimal", "decimal_nodec")
    assert list(values) == [Decimal("1.2346"), 19]


@pytest.mark.asyncio
async def test_order_by(db):
    await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("9.99"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("27.27"), decimal_nodec=1)
    values = await testmodels.DecimalFields.objects.all().order_by("decimal").values_list("decimal", flat=True)
    assert values == [Decimal("0"), Decimal("9.99"), Decimal("27.27")]


@pytest.mark.asyncio
async def test_aggregate_sum(db):
    await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("9.99"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("27.27"), decimal_nodec=1)
    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(sum_decimal=Sum("decimal"))
        .values("sum_decimal")
    )
    assert values[0] == {"sum_decimal": Decimal("37.26")}


@pytest.mark.asyncio
async def test_aggregate_sum_with_f_expression(db):
    await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("9.99"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("27.27"), decimal_nodec=1)
    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(sum_decimal=Sum(F("decimal")))
        .values("sum_decimal")
    )
    assert values[0] == {"sum_decimal": Decimal("37.26")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(sum_decimal=Sum(F("decimal") + 1))
        .values("sum_decimal")
    )
    assert values[0] == {"sum_decimal": Decimal("40.26")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(sum_decimal=Sum(F("decimal") + F("decimal")))
        .values("sum_decimal")
    )
    assert values[0] == {"sum_decimal": Decimal("74.52")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(sum_decimal=Sum(F("decimal") + F("decimal_nodec")))
        .values("sum_decimal")
    )
    assert values[0] == {"sum_decimal": Decimal("40.2600")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(sum_decimal=Sum(F("decimal") + F("decimal_null")))
        .values("sum_decimal")
    )
    assert values[0] == {"sum_decimal": None}


@pytest.mark.asyncio
async def test_aggregate_sum_no_exist_field_with_f_expression(db):
    with pytest.raises(
        FieldError,
        match="There is no non-virtual field not_exist on Model DecimalFields",
    ):
        await testmodels.DecimalFields.objects.all().annotate(sum_decimal=Sum(F("not_exist"))).values("sum_decimal")


@pytest.mark.asyncio
async def test_aggregate_sum_of_a_decimal_and_an_integer_column_is_a_decimal(db):
    """An integer column combines with a Decimal column into a Decimal, on either side (Django)."""
    await testmodels.DecimalFields.objects.create(id=1, decimal=Decimal("1.2500"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(id=2, decimal=Decimal("0.5000"), decimal_nodec=1)

    right = await testmodels.DecimalFields.objects.all().aggregate(total=Sum(F("decimal") + F("id")))
    left = await testmodels.DecimalFields.objects.all().aggregate(total=Sum(F("id") + F("decimal")))
    assert right["total"] == left["total"] == Decimal("4.75")
    assert isinstance(right["total"], Decimal)


@pytest.mark.asyncio
async def test_aggregate_avg(db):
    await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("9.99"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("27.27"), decimal_nodec=1)
    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(avg_decimal=Avg("decimal"))
        .values("avg_decimal")
    )
    assert values[0] == {"avg_decimal": Decimal("12.42")}


@pytest.mark.asyncio
async def test_aggregate_avg_with_f_expression(db):
    await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("9.99"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("27.27"), decimal_nodec=1)
    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(avg_decimal=Avg(F("decimal")))
        .values("avg_decimal")
    )
    assert values[0] == {"avg_decimal": Decimal("12.42")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(avg_decimal=Avg(F("decimal") + 1))
        .values("avg_decimal")
    )
    assert values[0] == {"avg_decimal": Decimal("13.42")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(avg_decimal=Avg(F("decimal") + F("decimal")))
        .values("avg_decimal")
    )
    assert values[0] == {"avg_decimal": Decimal("24.84")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(avg_decimal=Avg(F("decimal") + F("decimal_nodec")))
        .values("avg_decimal")
    )
    assert values[0] == {"avg_decimal": Decimal("13.4200")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(avg_decimal=Avg(F("decimal") + F("decimal_null")))
        .values("avg_decimal")
    )
    assert values[0] == {"avg_decimal": None}


@pytest.mark.asyncio
async def test_aggregate_max(db):
    await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("9.99"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("27.27"), decimal_nodec=1)
    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(max_decimal=Max("decimal"))
        .values("max_decimal")
    )
    assert values[0] == {"max_decimal": Decimal("27.27")}


@pytest.mark.asyncio
async def test_aggregate_max_with_f_expression(db):
    await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("9.99"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.create(decimal=Decimal("27.27"), decimal_nodec=1)
    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(max_decimal=Max(F("decimal")))
        .values("max_decimal")
    )
    assert values[0] == {"max_decimal": Decimal("27.27")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(max_decimal=Max(F("decimal") + 1))
        .values("max_decimal")
    )
    assert values[0] == {"max_decimal": Decimal("28.27")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(max_decimal=Max(F("decimal") + F("decimal")))
        .values("max_decimal")
    )
    assert values[0] == {"max_decimal": Decimal("54.54")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(max_decimal=Max(F("decimal") + F("decimal_nodec")))
        .values("max_decimal")
    )
    assert values[0] == {"max_decimal": Decimal("28.2700")}

    values = (
        await testmodels.DecimalFields.objects.all()
        .values("decimal_nodec")
        .annotate(max_decimal=Max(F("decimal") + F("decimal_null")))
        .values("max_decimal")
    )
    assert values[0] == {"max_decimal": None}


@pytest.mark.asyncio
async def test_whole_number_decimal_same_repr_created_vs_reread(db):
    """from_db_value used to do .quantize().normalize() - .normalize() strips trailing zeros
    (500.0000 -> 5E+2) for a FRESHLY-CONSTRUCTED in-memory instance (this path runs during
    construction), whereas a value natively read from the DB (keeps_native_db_values) never goes
    through this method at all and keeps the column's scale as-is - a divergence in how the same
    number is represented between "just created" and "reread from the DB"."""
    created = await testmodels.DecimalFields.objects.create(decimal=Decimal(500), decimal_nodec=Decimal(1))
    reread = await testmodels.DecimalFields.objects.get(id=created.id)
    assert str(created.decimal) == str(reread.decimal)
    assert str(created.decimal) == "500.0000"


@pytest.mark.asyncio
async def test_max_digits_above_28_quantizes_instead_of_raising(db):
    """Decimal.quantize() checks its result's digit count against a Context's own precision, not
    against DecimalField's own max_digits - relying on the ambient decimal.getcontext() (28
    significant digits by default) used to make max_digits above 28 silently unusable, even though
    Postgres NUMERIC itself supports far higher precision."""
    value = Decimal("99999999999999999999.999999999999999999")  # 38 significant digits
    created = await testmodels.HighPrecisionDecimalFields.objects.create(big=value)
    reread = await testmodels.HighPrecisionDecimalFields.objects.get(id=created.id)
    assert created.big == value
    assert reread.big == value


@pytest.mark.asyncio
@requires_features(dialect="postgresql")
async def test_decimal_beyond_28_significant_digits_round_trips_exactly(db):
    """rust_pg's decimal conversion was limited to ~28 significant digits (trailing zeros included):
    an ordinary value of a DecimalField with a large decimal_places failed to save, and a longer
    NUMERIC read back silently rounded. Both drivers now write and read it exactly."""
    from hare.core.connections import Connections

    for value in (Decimal("111111111111.111111111111000000"), Decimal("1000000000"), Decimal("-0.000000000000000001")):
        created = await testmodels.HighPrecisionDecimalFields.objects.create(big=value)
        reread = await testmodels.HighPrecisionDecimalFields.objects.get(id=created.id)
        assert reread.big == value
        assert await testmodels.HighPrecisionDecimalFields.objects.filter(id=created.id, big=value).count() == 1

    rows = await Connections.get("models").execute_dicts(
        "SELECT '1.123456789012345678901234567890'::numeric AS long_fraction, 1e30::numeric AS big, "
        "'1e-40'::numeric AS tiny"
    )
    assert rows[0]["long_fraction"] == Decimal("1.123456789012345678901234567890")
    assert str(rows[0]["long_fraction"]) == "1.123456789012345678901234567890"
    assert rows[0]["big"] == Decimal("1e30")
    assert rows[0]["tiny"] == Decimal("1e-40")


@pytest.mark.asyncio
async def test_rust_pg_unsupported_bind_parameter_type_raises_operational_error(db):
    """rust/pg/src/value.rs's Value::extract() falls through to "unsupported parameter type" for
    any Python value none of its known branches (bool/int/float/str/datetime/date/time/bytes/
    uuid.UUID/Decimal/Range/list/dict) recognize - e.g. a bare Python set, reachable in practice
    whenever a custom Field.to_db_value() forgets to convert something exotic to a primitive
    before it reaches the driver. This used to raise a bare, unwrapped pyo3 PyTypeError from
    inside the driver, bypassing RustPgClient._translate_exceptions entirely and reaching the
    caller as an undocumented exception type. Now routed through the same
    DriverError -> to_pyerr() -> pg.ConversionError path, surfacing as OperationalError."""
    from hare.core.connections import Connections
    from hare.exceptions import OperationalError

    if not type(Connections.get("models")).__module__.startswith("hare.dialects.postgresql.drivers.rust_pg"):
        pytest.skip("this exact raw-pyo3-exception bug is specific to rust_pg's own bind-parameter conversion")
    db_client = Connections.get("models")
    with pytest.raises(OperationalError):
        await db_client.execute_dicts("SELECT $1", [{1, 2, 3}])


@pytest.mark.asyncio
async def test_max_digits_at_or_below_28_keeps_previous_behavior(db):
    """The dedicated quantize Context floors at 28 (decimal's own default precision), so a field
    whose max_digits never exceeded that keeps behaving exactly as before this fix."""
    with pytest.raises(ValidationError):
        await testmodels.DecimalFields.objects.create(decimal=Decimal("1" * 30), decimal_nodec=1)


HIGH_PRECISION_LOWER = Decimal("1234567890123.456700000000000001")
HIGH_PRECISION_UPPER = Decimal("1234567890123.456700000000000002")


async def create_high_precision_pair() -> tuple[testmodels.HighPrecisionDecimalFields, ...]:
    lower = await testmodels.HighPrecisionDecimalFields.objects.create(big=HIGH_PRECISION_LOWER)
    upper = await testmodels.HighPrecisionDecimalFields.objects.create(big=HIGH_PRECISION_UPPER)
    return lower, upper


async def filtered_ids(**filters) -> list[int]:
    return sorted(await testmodels.HighPrecisionDecimalFields.objects.filter(**filters).values_list("id", flat=True))


@pytest.mark.asyncio
async def test_high_precision_comparisons_are_exact(db):
    """SQLite used to compare a DecimalField as CAST(... AS NUMERIC) - a double keeping only ~15
    significant digits, so two values differing past that compared equal."""
    lower, upper = await create_high_precision_pair()

    assert await filtered_ids(big=HIGH_PRECISION_LOWER) == [lower.id]
    assert await filtered_ids(big__gt=HIGH_PRECISION_LOWER) == [upper.id]
    assert await filtered_ids(big__lt=HIGH_PRECISION_UPPER) == [lower.id]
    assert await filtered_ids(big__gte=HIGH_PRECISION_UPPER) == [upper.id]
    assert await filtered_ids(big__in=[HIGH_PRECISION_LOWER]) == [lower.id]
    assert await filtered_ids(big__not_in=[HIGH_PRECISION_LOWER]) == [upper.id]
    assert await filtered_ids(big__not=HIGH_PRECISION_LOWER) == [upper.id]
    assert await filtered_ids(big__range=(HIGH_PRECISION_LOWER, HIGH_PRECISION_LOWER)) == [lower.id]


@pytest.mark.asyncio
async def test_high_precision_large_in_list_is_exact(db):
    """A long __in list is bound as one JSON array on SQLite - still compared exactly."""
    lower, _upper = await create_high_precision_pair()
    values = [HIGH_PRECISION_LOWER] + [Decimal(index) for index in range(1200)]

    assert await filtered_ids(big__in=values) == [lower.id]


@pytest.mark.asyncio
async def test_high_precision_comparison_with_another_column_is_exact(db):
    lower, upper = await create_high_precision_pair()
    await testmodels.HighPrecisionDecimalFields.objects.filter(id=upper.id).update(big=F("big"))

    assert await filtered_ids(big=F("big")) == [lower.id, upper.id]
    assert (await testmodels.HighPrecisionDecimalFields.objects.get(id=upper.id)).big == HIGH_PRECISION_UPPER


@pytest.mark.asyncio
async def test_high_precision_ordering_and_min_max_are_exact(db):
    lower, upper = await create_high_precision_pair()
    model = testmodels.HighPrecisionDecimalFields

    assert await model.objects.all().order_by("big").values_list("id", flat=True) == [lower.id, upper.id]
    assert await model.objects.all().order_by("-big").values_list("id", flat=True) == [upper.id, lower.id]
    aggregated = await model.objects.all().aggregate(largest=Max("big"), smallest=Min("big"))
    assert aggregated == {"largest": HIGH_PRECISION_UPPER, "smallest": HIGH_PRECISION_LOWER}


@pytest.mark.asyncio
async def test_high_precision_plain_f_copy_keeps_every_digit(db):
    """A plain F() read or copy used to go through SQLite's double, rounding to ~17 digits."""
    lower, _upper = await create_high_precision_pair()
    model = testmodels.HighPrecisionDecimalFields

    assert await model.objects.filter(id=lower.id).annotate(copy=F("big")).values_list("copy", flat=True) == [
        HIGH_PRECISION_LOWER
    ]
    assert (await model.objects.filter(id=lower.id).annotate(copy=F("big")).first()).copy == HIGH_PRECISION_LOWER
    await model.objects.filter(id=lower.id).update(big=F("big"))
    assert (await model.objects.get(id=lower.id)).big == HIGH_PRECISION_LOWER


@pytest.mark.asyncio
async def test_high_precision_annotation_comparisons_are_exact(db):
    lower, upper = await create_high_precision_pair()
    model = testmodels.HighPrecisionDecimalFields

    copies = model.objects.annotate(copy=F("big"))
    assert await copies.filter(copy=HIGH_PRECISION_LOWER).values_list("id", flat=True) == [lower.id]
    assert await copies.filter(copy__gt=HIGH_PRECISION_LOWER).values_list("id", flat=True) == [upper.id]
    largest = (
        await model.objects.all().annotate(largest=Max("big")).filter(largest=HIGH_PRECISION_UPPER).values("largest")
    )
    assert largest == [{"largest": HIGH_PRECISION_UPPER}]


@pytest.mark.asyncio
async def test_decimal_text_lookups_keep_the_column_scale(db):
    """SQLite matched LIKE-family lookups against the double's own text (`2.0`), not the
    column's decimal text with its full scale (`2.0000`) Postgres matches against."""
    model = testmodels.DecimalFields
    whole = await model.objects.create(decimal=Decimal("2"), decimal_nodec=1)
    fraction = await model.objects.create(decimal=Decimal("10.25"), decimal_nodec=1)

    async def matching_ids(**filters) -> list[int]:
        return sorted(await model.objects.filter(**filters).values_list("id", flat=True))

    assert await matching_ids(decimal__startswith="2.") == [whole.id]
    assert await matching_ids(decimal__endswith="00") == [whole.id, fraction.id]
    assert await matching_ids(decimal__contains=".0000") == [whole.id]
    assert await matching_ids(decimal__iexact="2.0000") == [whole.id]
    assert await matching_ids(decimal__iexact="2") == []


@pytest.mark.asyncio
async def test_float_text_lookups_use_the_postgres_text(db):
    """Postgres writes a whole float as `2` - SQLite used to match against `2.0`."""
    model = testmodels.FloatFields
    whole = await model.objects.create(floatnum=2.0)
    await model.objects.create(floatnum=10.25)

    async def matching_ids(**filters) -> list[int]:
        return sorted(await model.objects.filter(**filters).values_list("id", flat=True))

    assert await matching_ids(floatnum__iexact="2") == [whole.id]
    assert await matching_ids(floatnum__iexact="2.0") == []
    assert await matching_ids(floatnum__endswith=".0") == []


@pytest.mark.asyncio
async def test_negative_zero_decimal_reads_back_as_zero(db):
    """A numeric column has no negative zero - Postgres reads -0.00 back as 0.00."""
    obj = await testmodels.DecimalFields.objects.create(decimal=Decimal("-0.00001"), decimal_nodec=1)

    assert str(obj.decimal) == "0.0000"
    assert str((await testmodels.DecimalFields.objects.get(id=obj.id)).decimal) == "0.0000"


@pytest.mark.asyncio
async def test_invalid_decimal_is_a_validation_error_on_construction(db):
    with pytest.raises(ValidationError, match="is not a valid decimal number"):
        await testmodels.DecimalFields.objects.create(decimal="abc", decimal_nodec=1)
    with pytest.raises(ValidationError, match="is not a valid decimal number"):
        await testmodels.DecimalFields.objects.create(decimal=[1], decimal_nodec=1)


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_decimal_is_stored_as_fixed_point_text_on_sqlite(db):
    """SQLite stores a DecimalField as text - in fixed-point notation, the text Postgres prints."""
    from hare.core.connections import Connections

    small = await testmodels.HighPrecisionDecimalFields.objects.create(big=Decimal("1E-18"))
    alias = next(iter(Connections.current().db_config))
    connection = Connections.get(alias)
    _, rows = await connection.execute("SELECT big FROM highprecisiondecimalfields WHERE id = ?", [small.id])
    assert dict(rows[0])["big"] == "0.000000000000000001"


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_f_expression_update_rewrites_stored_text_to_the_column_scale_on_sqlite(db):
    """An arithmetic result stored as `3.5` is rewritten as `3.5000`, the text a plain write stores."""
    from hare.core.connections import Connections

    obj = await testmodels.DecimalFields.objects.create(decimal=Decimal("2"), decimal_nodec=1)
    await testmodels.DecimalFields.objects.filter(id=obj.id).update(decimal=F("decimal") + Decimal("1.5"))
    alias = next(iter(Connections.current().db_config))
    connection = Connections.get(alias)
    _, rows = await connection.execute("SELECT decimal FROM decimalfields WHERE id = ?", [obj.id])
    assert dict(rows[0])["decimal"] == "3.5000"


@pytest.mark.asyncio
async def test_float_lookup_value_means_its_shortest_repr(db):
    """create(decimal=0.1) stores 0.1000 - filter(decimal=0.1) finds it, as a numeric column
    compares a float on Postgres, not the float's exact binary value 0.1000000000000000055..."""
    row = await testmodels.DecimalFields.objects.create(decimal=0.1, decimal_nodec=1)

    assert await testmodels.DecimalFields.objects.filter(decimal=0.1).values_list("id", flat=True) == [row.id]
    assert await testmodels.DecimalFields.objects.filter(decimal__in=[0.1, 7.5]).values_list("id", flat=True) == [
        row.id
    ]
    assert await testmodels.DecimalFields.objects.filter(decimal__gte=0.1, decimal__lte=0.1).count() == 1


@pytest.mark.asyncio
async def test_whole_decimal_reads_back_without_an_exponent(db):
    """asyncpg decodes 100000 in a numeric(18,0) column as Decimal('1.0E+5')."""
    row = await testmodels.DecimalFields.objects.create(decimal=Decimal("0"), decimal_nodec=10**5)

    fetched = await testmodels.DecimalFields.objects.get(id=row.id)
    assert str(fetched.decimal_nodec) == "100000"
    assert (
        str((await testmodels.DecimalFields.objects.filter(id=row.id).values_list("decimal_nodec", flat=True))[0])
        == "100000"
    )
