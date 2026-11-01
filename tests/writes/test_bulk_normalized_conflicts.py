import pytest
import pytest_asyncio

from hare import fields
from hare.contrib.test import hare_test_context, requires_features
from hare.instrumentation.declarations import RowsChanged
from hare.instrumentation.enums import RowOperation
from hare.instrumentation.observers.observers import Observers
from hare.models import Model
from tests.utils.database_under_test import DatabaseUnderTest


class Contact(Model):
    id = fields.IntField(primary_key=True)
    email = fields.EmailField(unique=True, db_default="default@example.com")
    label = fields.CharField(max_length=30)


class LowercaseContact(Model):
    id = fields.IntField(primary_key=True)
    email = fields.EmailField(unique=True, lowercase=True, db_default="default@example.com")
    label = fields.CharField(max_length=30)


@pytest_asyncio.fixture(params=[Contact, LowercaseContact], ids=["preserve local case", "lowercase address"])
async def db_contacts(request):
    if not DatabaseUnderTest.get_dialect().features.supports_insert_conflicts:
        pytest.skip("The dialect has no INSERT ... ON CONFLICT")
    async with hare_test_context([__name__], db_url=DatabaseUnderTest.get_url()):
        yield request.param


@requires_features(supports_insert_conflicts=True, supports_returning=True)
@pytest.mark.asyncio
async def test_an_upsert_reports_an_updated_normalized_conflict_key(db_contacts):
    contact_model = db_contacts
    original = await contact_model.objects.create(id=1, email="Ann@example.com", label="before")
    changes = []
    listener = Observers.observe(RowsChanged, changes.append)
    try:
        changed = contact_model(id=2, email="Ann@EXAMPLE.COM", label="after")
        await contact_model.objects.bulk_create(
            [changed], update_fields=["label"], on_conflict=["email"], returning=True
        )
    finally:
        Observers.unobserve(RowsChanged, listener)

    assert [(change.operation, change.pks) for change in changes] == [(RowOperation.UPDATE, (original.pk,))]
    assert changed.pk == original.pk
    assert (await contact_model.objects.get(pk=original.pk)).label == "after"


@pytest.mark.parametrize("returning", [False, True])
@requires_features(supports_insert_conflicts=True, supports_returning=True)
@pytest.mark.asyncio
async def test_repeated_normalized_conflict_keys_are_written_in_order(db_contacts, returning):
    contact_model = db_contacts
    first = contact_model(id=1, email="Ann@EXAMPLE.COM", label="first")
    middle = contact_model(id=2, email="Bob@example.com", label="middle")
    last = contact_model(id=3, email="Ann@example.com", label="last")

    written = await contact_model.objects.bulk_create(
        [first, middle, last], update_fields=["label"], on_conflict=["email"], returning=returning
    )

    assert written == [first, middle, last]
    assert await contact_model.objects.count() == 2
    assert (await contact_model.objects.get(email="Ann@example.com")).label == "last"
    assert all(contact._saved_in_db for contact in written)
    if returning:
        assert [contact.pk for contact in written] == [1, 2, 1]


@requires_features(supports_insert_conflicts=True, supports_returning=True)
@pytest.mark.asyncio
async def test_local_part_case_follows_the_email_fields_lowercase_option(db_contacts):
    contact_model = db_contacts
    first = contact_model(id=1, email="Ann@example.com", label="first")
    last = contact_model(id=2, email="ann@example.com", label="last")

    await contact_model.objects.bulk_create(
        [first, last], update_fields=["label"], on_conflict=["email"], returning=True
    )

    if contact_model is Contact:
        assert await contact_model.objects.count() == 2
        assert [first.pk, last.pk] == [1, 2]
    else:
        assert await contact_model.objects.count() == 1
        assert [first.pk, last.pk] == [1, 1]
    assert (await contact_model.objects.get(email="ann@example.com")).label == "last"


@requires_features(supports_insert_conflicts=True, supports_returning=True)
@pytest.mark.asyncio
async def test_a_conflict_key_left_to_its_database_default_is_not_normalized_as_a_value(db_contacts):
    contact_model = db_contacts
    contact = contact_model(id=1, label="default")

    await contact_model.objects.bulk_create([contact], update_fields=["label"], on_conflict=["email"], returning=True)

    assert contact.email == "default@example.com"
    assert contact._saved_in_db


@requires_features(supports_insert_conflicts=True, supports_returning=True)
@pytest.mark.asyncio
async def test_repeated_database_default_conflict_keys_report_inserts_and_updates(db_contacts):
    contact_model = db_contacts
    first = contact_model(id=1, label="first")
    last = contact_model(id=2, label="last")
    changes = []
    listener = Observers.observe(RowsChanged, changes.append)
    try:
        await contact_model.objects.bulk_create(
            [first, last], update_fields=["label"], on_conflict=["email"], returning=True
        )
        assert [contact.pk for contact in [first, last]] == [1, 1]
        changes.clear()
        later = contact_model(id=3, label="later")
        await contact_model.objects.bulk_create(
            [later], update_fields=["label"], on_conflict=["email"], returning=True
        )
    finally:
        Observers.unobserve(RowsChanged, listener)

    assert [(change.operation, change.pks) for change in changes] == [(RowOperation.UPDATE, (1,))]
    assert later.pk == 1
    assert (await contact_model.objects.get(pk=1)).label == "later"
