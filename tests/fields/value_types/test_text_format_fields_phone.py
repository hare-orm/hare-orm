"""PhoneField with phonenumbers - a number read in any form, of the region of ``region=`` or international:
written in E.164 form on every write path, filter values converted the same way."""

import pytest

from hare import fields
from tests.fields.value_types.models_text_formats_phone import RegionalContact
from tests.utils.database_under_test import DatabaseUnderTest

REQUIRES = ("phone",)


@pytest.mark.asyncio
async def test_every_write_path_normalizes_a_regional_number(db_text_formats):
    contact = await RegionalContact.objects.create(id=1, us_phone="(650) 253-0000")
    await contact.refresh_from_db()
    assert contact.us_phone == "+16502530000"
    await RegionalContact.objects.bulk_create([RegionalContact(id=2, us_phone="650-253-0002"), RegionalContact(id=3)])
    contact.us_phone = "650-253-0001"
    await contact.save()
    await RegionalContact.objects.filter(id=3).update(us_phone="(650) 253-0003")
    others = await RegionalContact.objects.filter(id__in=[2, 3]).order_by("id")
    for other in others:
        other.us_phone = f"(650) 253-000{other.id + 2}"
    await RegionalContact.objects.bulk_update(others, fields=["us_phone"])
    assert await RegionalContact.objects.order_by("id").values_list("id", "us_phone") == [
        (1, "+16502530001"),
        (2, "+16502530004"),
        (3, "+16502530005"),
    ]


@pytest.mark.asyncio
async def test_a_regional_filter_value_is_converted_like_a_written_one(db_text_formats):
    await RegionalContact.objects.create(id=1, us_phone="+16502530000", phone="+442079460958")
    await RegionalContact.objects.create(id=2)
    assert await RegionalContact.objects.filter(phone__in=["+44 20 7946 0958"]).values_list("id", flat=True) == [1]
    assert await RegionalContact.objects.filter(us_phone="(650) 253-0000").values_list("id", flat=True) == [1]
    assert await RegionalContact.objects.filter(us_phone__in=["650-253-0000"]).values_list("id", flat=True) == [1]


@pytest.mark.asyncio
async def test_bulk_create_with_copy_normalizes_a_regional_number(db_text_formats):
    if not DatabaseUnderTest.get_dialect().features.supports_copy:
        pytest.skip("The database has no COPY bulk-load protocol")
    await RegionalContact.objects.bulk_create([RegionalContact(id=1, us_phone="(650) 253-0000")], use_copy=True)
    assert await RegionalContact.objects.values_list("us_phone", flat=True).get(id=1) == "+16502530000"


@pytest.mark.database_independent
def test_constraints_leave_the_form_of_a_number_to_phonenumbers():
    assert fields.PhoneField().constraints == {"max_length": 16}


@pytest.mark.database_independent
def test_deconstruct_writes_the_region():
    assert fields.PhoneField(region="US", null=True).deconstruct()[2] == {"region": "US", "null": True}
