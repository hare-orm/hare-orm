import enum
from decimal import Decimal
from enum import StrEnum

import pytest
import pytest_asyncio

from hare.exceptions import FieldError
from tests.testmodels import (
    BooleanFields,
    CharFields,
    CharFkRelatedModel,
    CharPkModel,
    DecimalFields,
)


class MyEnum(StrEnum):
    moo = "moo"


class MyStrEnum(StrEnum):
    moo = "moo"


class PlainStrMixinEnum(str, enum.Enum):
    """A `(str, Enum)` mix-in, unlike StrEnum above - `str(PlainStrMixinEnum.moo)` renders
    "PlainStrMixinEnum.moo" (Python 3.12+'s own Enum.__str__), not "moo", the exact shape that
    used to leak into filter SQL as a literal wrong value."""

    moo = "moo"


# --- CharFields tests ---


@pytest_asyncio.fixture
async def char_fields_data(db):
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="baa", char_null="baa")
    await CharFields.objects.create(char="oink")


@pytest.mark.asyncio
async def test_char_field_bad_param(db, char_fields_data):
    with pytest.raises(FieldError, match=r"CharFields.objects.filter\(charup=...\): CharFields has no field 'charup'"):
        await CharFields.objects.filter(charup="moo")


@pytest.mark.asyncio
async def test_char_field_equal(db, char_fields_data):
    assert set(await CharFields.objects.filter(char="moo").values_list("char", flat=True)) == {"moo"}


@pytest.mark.asyncio
async def test_char_field_enum(db, char_fields_data):
    assert set(await CharFields.objects.filter(char=MyEnum.moo).values_list("char", flat=True)) == {"moo"}
    assert set(await CharFields.objects.filter(char=MyStrEnum.moo).values_list("char", flat=True)) == {"moo"}


@pytest.mark.asyncio
async def test_char_field_plain_str_enum_mixin(db):
    """A `(str, enum.Enum)` value used to render as the literal text "PlainStrMixinEnum.moo"
    (ValueWrapper.get_formatted_value's `isinstance(value, str)` branch matched before its own
    `isinstance(value, Enum)` branch ever got a chance to unwrap `.value`) - `=`/`__not` must
    compare against the enum's actual value ("moo"), same as the already-working StrEnum case."""
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="baa")

    assert (await CharFields.objects.get(char=PlainStrMixinEnum.moo)).char == "moo"
    assert set(await CharFields.objects.filter(char=PlainStrMixinEnum.moo).values_list("char", flat=True)) == {"moo"}
    assert set(await CharFields.objects.filter(char__not=PlainStrMixinEnum.moo).values_list("char", flat=True)) == {
        "baa"
    }


@pytest.mark.asyncio
async def test_char_field_plain_str_enum_mixin_string_lookups(db, char_fields_data):
    """string_encoder-backed lookups (iexact/contains/startswith/posix_regex/...) used to embed
    the same wrong "PlainStrMixinEnum.moo" text (string_encoder's own unconditional `str(value)`)
    instead of the enum's actual value."""
    assert set(await CharFields.objects.filter(char__iexact=PlainStrMixinEnum.moo).values_list("char", flat=True)) == {
        "moo"
    }
    assert set(
        await CharFields.objects.filter(char__contains=PlainStrMixinEnum.moo).values_list("char", flat=True)
    ) == {"moo"}
    assert set(
        await CharFields.objects.filter(char__startswith=PlainStrMixinEnum.moo).values_list("char", flat=True)
    ) == {"moo"}


@pytest.mark.asyncio
async def test_char_field_not(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__not="moo").values_list("char", flat=True)) == {
        "baa",
        "oink",
    }


@pytest.mark.asyncio
async def test_char_field_in(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__in=["moo", "baa"]).values_list("char", flat=True)) == {
        "moo",
        "baa",
    }


@pytest.mark.asyncio
async def test_char_field_in_empty(db, char_fields_data):
    assert await CharFields.objects.filter(char__in=[]).values_list("char", flat=True) == []


@pytest.mark.asyncio
async def test_char_field_not_in(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__not_in=["moo", "baa"]).values_list("char", flat=True)) == {"oink"}


@pytest.mark.asyncio
async def test_char_field_not_in_empty(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__not_in=[]).values_list("char", flat=True)) == {
        "oink",
        "moo",
        "baa",
    }


@pytest.mark.asyncio
async def test_char_field_isnull(db, char_fields_data):
    assert set(await CharFields.objects.filter(char_null__isnull=True).values_list("char", flat=True)) == {
        "moo",
        "oink",
    }
    assert set(await CharFields.objects.filter(char_null__isnull=False).values_list("char", flat=True)) == {"baa"}


@pytest.mark.asyncio
async def test_char_field_not_isnull(db, char_fields_data):
    assert set(await CharFields.objects.filter(char_null__not_isnull=True).values_list("char", flat=True)) == {"baa"}
    assert set(await CharFields.objects.filter(char_null__not_isnull=False).values_list("char", flat=True)) == {
        "moo",
        "oink",
    }


@pytest.mark.asyncio
async def test_char_field_gte(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__gte="moo").values_list("char", flat=True)) == {
        "moo",
        "oink",
    }


@pytest.mark.asyncio
async def test_char_field_lte(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__lte="moo").values_list("char", flat=True)) == {
        "moo",
        "baa",
    }


@pytest.mark.asyncio
async def test_char_field_gt(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__gt="moo").values_list("char", flat=True)) == {"oink"}


@pytest.mark.asyncio
async def test_char_field_lt(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__lt="moo").values_list("char", flat=True)) == {"baa"}


@pytest.mark.asyncio
async def test_char_field_contains(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__contains="o").values_list("char", flat=True)) == {
        "moo",
        "oink",
    }


@pytest.mark.asyncio
async def test_char_field_startswith(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__startswith="m").values_list("char", flat=True)) == {"moo"}
    assert set(await CharFields.objects.filter(char__startswith="s").values_list("char", flat=True)) == set()


@pytest.mark.asyncio
async def test_char_field_endswith(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__endswith="o").values_list("char", flat=True)) == {"moo"}
    assert set(await CharFields.objects.filter(char__endswith="s").values_list("char", flat=True)) == set()


@pytest.mark.asyncio
async def test_char_field_icontains(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__icontains="oO").values_list("char", flat=True)) == {"moo"}
    assert set(await CharFields.objects.filter(char__icontains="Oo").values_list("char", flat=True)) == {"moo"}


@pytest.mark.asyncio
async def test_string_lookup_none_value_raises(db, char_fields_data):
    """None used to silently become the literal string "None" (string_encoder called
    str(None) unconditionally) - a filter for an unset/optional search parameter that happened
    to be None would match any row containing that literal substring instead of raising or
    matching nothing. None can't express IS NULL through a LIKE-family lookup, so it's rejected
    outright - use __isnull=/__not_isnull= for that. `__iexact=None` means `__isnull=True`, like
    Django."""
    from hare.exceptions import UnSupportedError

    for lookup in ("contains", "icontains", "startswith", "istartswith", "endswith", "iendswith"):
        with pytest.raises(UnSupportedError):
            await CharFields.objects.filter(**{f"char__{lookup}": None})


@pytest.mark.asyncio
async def test_none_of_a_not_null_field(db, char_fields_data):
    """`field__not=None` is `field__isnull=False`, like `field=None` is `field__isnull=True` - on a
    field that can't be NULL it raised a ValidationError ("required") instead of matching every row."""
    from hare.query.expressions import Q

    total = await CharFields.objects.count()
    assert await CharFields.objects.filter(char__not=None).count() == total
    assert await CharFields.objects.filter(char=None).count() == 0
    assert await CharFields.objects.exclude(char__not=None).count() == 0
    assert await CharFields.objects.filter(~Q(char__not=None)).count() == 0
    assert await CharFields.objects.filter(char_null__not=None).count() == (
        total - await CharFields.objects.filter(char_null__isnull=True).count()
    )


@pytest.mark.asyncio
async def test_char_field_iexact(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__iexact="MoO").values_list("char", flat=True)) == {"moo"}


@pytest.mark.asyncio
async def test_char_field_iexact_with_f_expression_compares_the_other_column(db):
    """`char__iexact=F("char_null")` used to run `Upper(str(value))` on the ALREADY-RESOLVED
    Term `value` was passed as (F("char_null") resolves to a Field term before reaching
    insensitive_exact()) - str(Term) rendered something like "char_null" wrapped in the Term's
    own repr, so a row whose `char` column happened to equal that literal text matched instead of
    comparing against the OTHER column's actual value, and a genuine case-insensitive match
    across the two columns (e.g. char='B', char_null='b') never matched at all."""
    from hare.query.expressions import F

    await CharFields.objects.create(char="B", char_null="b")
    await CharFields.objects.create(char="B", char_null="different")

    matching = await CharFields.objects.filter(char__iexact=F("char_null")).values_list("char_null", flat=True)
    assert list(matching) == ["b"]


@pytest.mark.asyncio
async def test_char_field_contains_and_startswith_and_endswith_take_an_f_expression(db, char_fields_data):
    """contains()/startswith()/endswith() (and their i... variants) take an F()/expression value -
    the pattern is built in SQL; a row whose value is NULL matches none of them."""
    from hare.query.expressions import F

    for lookup in ("contains", "icontains", "startswith", "istartswith", "endswith", "iendswith"):
        matched = await CharFields.objects.filter(**{f"char__{lookup}": F("char")}).count()
        assert matched == await CharFields.objects.count()
        null_rows = await CharFields.objects.filter(char_null=None).count()
        assert await CharFields.objects.filter(**{f"char__{lookup}": F("char_null")}).count() <= (
            await CharFields.objects.count() - null_rows
        )


@pytest.mark.asyncio
async def test_char_field_istartswith(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__istartswith="m").values_list("char", flat=True)) == {"moo"}
    assert set(await CharFields.objects.filter(char__istartswith="M").values_list("char", flat=True)) == {"moo"}


@pytest.mark.asyncio
async def test_char_field_iendswith(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__iendswith="oO").values_list("char", flat=True)) == {"moo"}
    assert set(await CharFields.objects.filter(char__iendswith="Oo").values_list("char", flat=True)) == {"moo"}


@pytest_asyncio.fixture
async def mixed_case_char_fields_data(db):
    await CharFields.objects.create(char="hello123")
    await CharFields.objects.create(char="HELLO123")


@pytest.mark.asyncio
async def test_char_field_contains_is_case_sensitive(db, mixed_case_char_fields_data):
    """SQLite's own LIKE is ASCII case-INSENSITIVE by default, unlike Postgres's LIKE - this must
    behave identically (case-sensitively) on both dialects for the same filter and data."""
    assert set(await CharFields.objects.filter(char__contains="HELLO").values_list("char", flat=True)) == {"HELLO123"}


@pytest.mark.asyncio
async def test_char_field_startswith_is_case_sensitive(db, mixed_case_char_fields_data):
    assert set(await CharFields.objects.filter(char__startswith="HELLO").values_list("char", flat=True)) == {
        "HELLO123"
    }


@pytest.mark.asyncio
async def test_char_field_endswith_is_case_sensitive(db, mixed_case_char_fields_data):
    assert set(await CharFields.objects.filter(char__endswith="LO123").values_list("char", flat=True)) == {"HELLO123"}


@pytest.mark.asyncio
async def test_char_field_icontains_stays_case_insensitive(db, mixed_case_char_fields_data):
    assert set(await CharFields.objects.filter(char__icontains="hello").values_list("char", flat=True)) == {
        "hello123",
        "HELLO123",
    }


@pytest.mark.asyncio
async def test_char_field_istartswith_stays_case_insensitive(db, mixed_case_char_fields_data):
    assert set(await CharFields.objects.filter(char__istartswith="hello").values_list("char", flat=True)) == {
        "hello123",
        "HELLO123",
    }


@pytest.mark.asyncio
async def test_char_field_iendswith_stays_case_insensitive(db, mixed_case_char_fields_data):
    assert set(await CharFields.objects.filter(char__iendswith="lo123").values_list("char", flat=True)) == {
        "hello123",
        "HELLO123",
    }


@pytest.mark.asyncio
async def test_char_field_sorting(db, char_fields_data):
    assert await CharFields.objects.all().order_by("char").values_list("char", flat=True) == [
        "baa",
        "moo",
        "oink",
    ]


# --- BooleanFields tests ---


@pytest_asyncio.fixture
async def boolean_fields_data(db):
    await BooleanFields.objects.create(boolean=True)
    await BooleanFields.objects.create(boolean=False)
    await BooleanFields.objects.create(boolean=True, boolean_null=True)
    await BooleanFields.objects.create(boolean=False, boolean_null=True)
    await BooleanFields.objects.create(boolean=True, boolean_null=False)
    await BooleanFields.objects.create(boolean=False, boolean_null=False)


@pytest.mark.asyncio
async def test_boolean_field_equal_true(db, boolean_fields_data):
    assert set(await BooleanFields.objects.filter(boolean=True).values_list("boolean", "boolean_null")) == {
        (True, None),
        (True, True),
        (True, False),
    }


@pytest.mark.asyncio
async def test_boolean_field_equal_false(db, boolean_fields_data):
    assert set(await BooleanFields.objects.filter(boolean=False).values_list("boolean", "boolean_null")) == {
        (False, None),
        (False, True),
        (False, False),
    }


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="boolean_field_equal_true2"),
        pytest.param(False, id="boolean_field_equal_false2"),
        pytest.param(None, id="boolean_field_equal_null"),
    ],
)
@pytest.mark.asyncio
async def test_boolean_field_equal_value(db, boolean_fields_data, value):
    assert set(await BooleanFields.objects.filter(boolean_null=value).values_list("boolean", "boolean_null")) == {
        (False, value),
        (True, value),
    }


# --- DecimalFields tests ---


@pytest_asyncio.fixture
async def decimal_fields_data(db):
    await DecimalFields.objects.create(decimal="1.2345", decimal_nodec=1)
    await DecimalFields.objects.create(decimal="2.34567", decimal_nodec=1)
    await DecimalFields.objects.create(decimal="2.300", decimal_nodec=1)
    await DecimalFields.objects.create(decimal="023.0", decimal_nodec=1)
    await DecimalFields.objects.create(decimal="0.230", decimal_nodec=1)


@pytest.mark.asyncio
async def test_decimal_field_sorting(db, decimal_fields_data):
    assert await DecimalFields.objects.all().order_by("decimal").values_list("decimal", flat=True) == [
        Decimal("0.23"),
        Decimal("1.2345"),
        Decimal("2.3"),
        Decimal("2.3457"),
        Decimal("23"),
    ]


@pytest.mark.asyncio
async def test_decimal_field_gt(db, decimal_fields_data):
    assert await DecimalFields.objects.filter(decimal__gt=Decimal("1.2345")).order_by("decimal").values_list(
        "decimal", flat=True
    ) == [Decimal("2.3"), Decimal("2.3457"), Decimal("23")]


@pytest.mark.asyncio
async def test_decimal_field_between_and(db, decimal_fields_data):
    assert await DecimalFields.objects.filter(decimal__range=(Decimal("1.2344"), Decimal("1.2346"))).values_list(
        "decimal", flat=True
    ) == [Decimal("1.2345")]


@pytest.mark.asyncio
async def test_decimal_field_in(db, decimal_fields_data):
    assert await DecimalFields.objects.filter(decimal__in=[Decimal("1.2345"), Decimal("1000")]).values_list(
        "decimal", flat=True
    ) == [Decimal("1.2345")]


# --- CharPkModel / CharFkRelatedModel tests ---


@pytest_asyncio.fixture
async def char_fk_data(db):
    model1 = await CharPkModel.objects.create(id=17)
    model2 = await CharPkModel.objects.create(id=12)
    await CharPkModel.objects.create(id=2001)
    await CharFkRelatedModel.objects.create(model=model1)
    await CharFkRelatedModel.objects.create(model=model1)
    await CharFkRelatedModel.objects.create(model=model2)


@pytest.mark.asyncio
async def test_char_fk_bad_param(db, char_fk_data):
    with pytest.raises(
        FieldError, match=r"CharPkModel.objects.filter\(bad_param=...\): CharPkModel has no field 'bad_param'"
    ):
        await CharPkModel.objects.filter(bad_param="moo")


@pytest.mark.asyncio
async def test_char_fk_equal(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id=2001).values_list("id", flat=True)) == {"2001"}


@pytest.mark.asyncio
async def test_char_fk_not(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__not=2001).values_list("id", flat=True)) == {"17", "12"}


@pytest.mark.asyncio
async def test_char_fk_in(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__in=[17, 12]).values_list("id", flat=True)) == {
        "17",
        "12",
    }


@pytest.mark.asyncio
async def test_char_fk_in_empty(db, char_fk_data):
    assert await CharPkModel.objects.filter(id__in=[]).values_list("id", flat=True) == []


@pytest.mark.asyncio
async def test_char_fk_not_in(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__not_in=[17, 12]).values_list("id", flat=True)) == {"2001"}


@pytest.mark.asyncio
async def test_char_fk_not_in_empty(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__not_in=[]).values_list("id", flat=True)) == {
        "17",
        "12",
        "2001",
    }


@pytest.mark.asyncio
async def test_char_fk_isnull(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(children__isnull=True).values_list("id", flat=True)) == {"2001"}
    assert await CharPkModel.objects.filter(children__isnull=False).order_by("id").values_list("id", flat=True) == [
        "12",
        "17",
        "17",
    ]


@pytest.mark.asyncio
async def test_char_fk_not_isnull(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(children__not_isnull=True).values_list("id", flat=True)) == {
        "17",
        "12",
    }
    assert set(await CharPkModel.objects.filter(children__not_isnull=False).values_list("id", flat=True)) == {"2001"}


@pytest.mark.asyncio
async def test_char_fk_gte(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__gte=17).values_list("id", flat=True)) == {"17", "2001"}


@pytest.mark.asyncio
async def test_char_fk_lte(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__lte=17).values_list("id", flat=True)) == {"12", "17"}


@pytest.mark.asyncio
async def test_char_fk_gt(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__gt=17).values_list("id", flat=True)) == {"2001"}


@pytest.mark.asyncio
async def test_char_fk_lt(db, char_fk_data):
    assert set(await CharPkModel.objects.filter(id__lt=17).values_list("id", flat=True)) == {"12"}


@pytest.mark.asyncio
async def test_char_fk_sorting(db, char_fk_data):
    assert await CharPkModel.objects.all().order_by("id").values_list("id", flat=True) == [
        "12",
        "17",
        "2001",
    ]
    assert await CharPkModel.objects.all().order_by("-id").values_list("id", flat=True) == [
        "2001",
        "17",
        "12",
    ]
