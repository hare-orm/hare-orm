import datetime

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import ValidationError
from tests.fields.subclass_models import (
    Contact,
    ContactTypeEnum,
    DateMarkerModel,
    RaceParticipant,
    RacePlacingEnum,
    XorMaskedModel,
)
from tests.utils.database_under_test import DatabaseUnderTest


async def create_participants():
    """Helper to create race participants for tests."""
    test1 = await RaceParticipant.objects.create(
        first_name="Alex",
        place=RacePlacingEnum.FIRST,
        predicted_place=RacePlacingEnum.THIRD,
    )
    test2 = await RaceParticipant.objects.create(
        first_name="Ben",
        place=RacePlacingEnum.SECOND,
        predicted_place=RacePlacingEnum.FIRST,
    )
    test3 = await RaceParticipant.objects.create(first_name="Chris", place=RacePlacingEnum.THIRD)
    test4 = await RaceParticipant.objects.create(first_name="Bill")

    return test1, test2, test3, test4


@pytest.mark.asyncio
async def test_enum_field_create(db_subclass_fields):
    """Asserts that the new field is saved properly."""
    test1, _, _, _ = await create_participants()
    assert test1 in await RaceParticipant.objects.all()
    assert test1.place == RacePlacingEnum.FIRST


@pytest.mark.asyncio
async def test_enum_field_update(db_subclass_fields):
    """Asserts that the new field can be updated correctly."""
    test1, _, _, _ = await create_participants()
    test1.place = RacePlacingEnum.SECOND
    await test1.save()

    tied_second = await RaceParticipant.objects.filter(place=RacePlacingEnum.SECOND)

    assert test1 in tied_second
    assert len(tied_second) == 2


@pytest.mark.asyncio
async def test_enum_field_filter(db_subclass_fields):
    """Assert that filters correctly select the enums."""
    await create_participants()

    first_place = await RaceParticipant.objects.filter(place=RacePlacingEnum.FIRST).first()
    second_place = await RaceParticipant.objects.filter(place=RacePlacingEnum.SECOND).first()

    assert first_place.place == RacePlacingEnum.FIRST
    assert second_place.place == RacePlacingEnum.SECOND


@pytest.mark.asyncio
async def test_enum_field_delete(db_subclass_fields):
    """Assert that delete correctly removes the right participant by their place."""
    await create_participants()
    await RaceParticipant.objects.filter(place=RacePlacingEnum.FIRST).delete()
    assert await RaceParticipant.objects.all().count() == 3


@pytest.mark.asyncio
async def test_enum_field_default(db_subclass_fields):
    """Test that default enum value is applied correctly."""
    _, _, _, test4 = await create_participants()
    assert test4.place == RacePlacingEnum.DNF


@pytest.mark.asyncio
async def test_enum_field_null(db_subclass_fields):
    """Assert that filtering by None selects the records which are null."""
    _, _, test3, test4 = await create_participants()

    no_predictions = await RaceParticipant.objects.filter(predicted_place__isnull=True)

    assert test3 in no_predictions
    assert test4 in no_predictions


@pytest.mark.asyncio
async def test_update_with_int_enum_value(db_subclass_fields):
    """Test updating with integer enum value."""
    contact = await Contact.objects.create()
    contact.type = ContactTypeEnum.home
    await contact.save()
    assert (await Contact.objects.get(id=contact.id)).type == ContactTypeEnum.home


@pytest.mark.asyncio
async def test_exception_on_invalid_data_type_in_int_field(db_subclass_fields):
    """Test that invalid data types raise appropriate exceptions.

    IntEnumField.to_db_value() (tests/fields/subclass_fields.py) calls self.validate(value)
    BEFORE its own isinstance(value, self.enum_type) check - IntField now wires
    MinValueValidator/MaxValueValidator into every instance's validators, so a non-numeric value
    is now caught there first, as ValidationError, before that isinstance check is ever reached.
    """
    contact = await Contact.objects.create()

    contact.type = "not_int"
    with pytest.raises((TypeError, ValueError, ValidationError)):
        await contact.save()


@pytest.mark.asyncio
async def test_date_field_subclass_to_python_value_override_runs_on_native_driver(db_subclass_fields):
    """MarkerDateField.from_db_value() (tests/fields/subclass_fields.py) records every call it
    receives - fetching must add a new entry, proving the override actually ran during hydration
    rather than being silently skipped by DateField's own keeps_native_db_values=True. That flag
    is only correct for DateField's OWN from_db_value; a subclass overriding it must not inherit
    the shortcut just because it's still a DateField subclass. datetime.date is only in DB_NATIVE
    on Postgres (not SQLite - see hare.dialects.base.executor.base.BaseExecutor and
    hare.dialects.sqlite.executor.SqliteExecutor), so this bug can only manifest there."""
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("datetime.date is only DB_NATIVE on Postgres - this bug can't manifest on sqlite")

    field = DateMarkerModel._meta.fields_map["event_date"]
    created = await DateMarkerModel.objects.create(event_date=datetime.date(2020, 1, 1))
    field.hydration_log.clear()  # Model.__init__() already logged one call during construction.

    fetched = await DateMarkerModel.objects.get(id=created.id)

    assert fetched.event_date == datetime.date(2020, 1, 1)
    assert field.hydration_log == [datetime.date(2020, 1, 1)]


@pytest.mark.asyncio
async def test_xor_masked_field_is_not_double_encoded_on_construction_and_save(db_subclass_fields):
    """A custom field whose to_db_value()/from_db_value() apply the SAME asymmetric transform
    both ways (XorMaskedField) used to have its freshly assigned value run through
    from_db_value() - the DECODE half - by the default to_python(). That
    masked Model(value="hello") in memory right after construction, and then to_db_value()
    masked it a SECOND time on save(), writing the plaintext "hello" to the DB instead of the
    masked "HELLO" - a real data leak."""
    created = XorMaskedModel(value="hello")
    # Not masked in memory right after construction - a fresh value is a literal, not
    # DB-shaped data to decode.
    assert created.value == "hello"

    await created.save()

    reread = await XorMaskedModel.objects.get(id=created.id)
    assert reread.value == "hello"  # from_db_value() unmasks a real DB-read value correctly

    found = await XorMaskedModel.objects.filter(value="hello").first()
    assert found is not None
    assert found.id == created.id


@requires_features(dialect="sqlite")
@pytest.mark.asyncio
async def test_xor_masked_field_stores_ciphertext_not_plaintext(db_subclass_fields):
    """Checks the actual stored column value directly - a plain model read already re-masks it
    back to "hello" on the way out (from_db_value()), which alone can't tell apart "the DB
    genuinely holds ciphertext" from "the DB holds plaintext and got masked twice on read"."""
    created = await XorMaskedModel.objects.create(value="hello")

    alias = next(iter(Connections.current().db_config))
    connection = Connections.get(alias)
    _, rows = await connection.execute("SELECT value FROM xormaskedmodel WHERE id = ?", [created.id])
    assert dict(rows[0])["value"] == "HELLO"  # masked ciphertext, never the plaintext
