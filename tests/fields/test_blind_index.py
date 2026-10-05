"""EncryptedTextField(blind_index=True): the HMAC of the plaintext in its own column, written with the
field on every write path, equality and membership filters through it, uniqueness through it - and
what it refuses."""

from __future__ import annotations

import pytest

from hare import Model, fields
from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import ConfigurationError, FieldError, IntegrityError
from hare.fields.encrypted import BlindIndexField, EncryptedTextField
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.query.expressions import F
from hare.transactions import Transactions
from tests.fields.models_blind_index import BlindIndexedCustomer

SECRET = "blind-index-test-secret"


@pytest.fixture(autouse=True)
def configured_keys():
    FieldEncryption.configure(SECRET)
    yield
    FieldEncryption.reset()


async def fetch_raw_rows() -> list[dict]:
    return await Connections.get("models").execute_dicts(
        'SELECT "id", "email", "email_blind_index", "phone_number", "phone_number_blind_index" '
        'FROM "blind_indexed_customer" ORDER BY "id"'
    )


async def get_ids(**kwargs) -> list[int]:
    return list(await BlindIndexedCustomer.objects.filter(**kwargs).order_by("id").values_list("id", flat=True))


async def create_customers() -> None:
    await BlindIndexedCustomer.objects.create(id=1, email="ann@example.com", phone="+100")
    await BlindIndexedCustomer.objects.create(id=2, email="bob@example.com", phone="+200")
    await BlindIndexedCustomer.objects.create(id=3, email="cat@example.com")


def test_the_model_gets_a_blind_index_field_per_field():
    meta = BlindIndexedCustomer._meta
    assert meta.blind_index_fields == {
        "email": "email_blind_index",
        "phone": "phone_blind_index",
        "backup_email": "backup_email_blind_index",
    }
    assert isinstance(meta.fields_map["email_blind_index"], BlindIndexField)
    assert meta.fields_map["email_blind_index"].unique is True
    assert meta.fields_map["phone_blind_index"].source_field == "phone_number_blind_index"
    assert meta.fields_map["phone_blind_index"].index is True
    assert "note_blind_index" not in meta.fields_map
    assert "email_blind_index" in meta.sensitive_fields


@pytest.mark.asyncio
async def test_the_index_is_the_hmac_of_the_plaintext(db_blind_index):
    await create_customers()
    rows = await fetch_raw_rows()
    assert rows[0]["email"] != "ann@example.com"
    assert rows[0]["email_blind_index"] == FieldEncryption.get_blind_index("ann@example.com")
    assert rows[0]["phone_number_blind_index"] == FieldEncryption.get_blind_index("+100")
    assert rows[2]["phone_number_blind_index"] is None
    customer = await BlindIndexedCustomer.objects.get(id=1)
    assert customer.email == "ann@example.com"


@pytest.mark.asyncio
async def test_equality_and_membership_filters(db_blind_index):
    await create_customers()
    assert await get_ids(email="bob@example.com") == [2]
    assert await get_ids(email__not="bob@example.com") == [1, 3]
    assert await get_ids(email__in=["ann@example.com", "cat@example.com", "nobody@example.com"]) == [1, 3]
    assert await get_ids(email__not_in=["ann@example.com"]) == [2, 3]
    assert await get_ids(phone="+200") == [2]
    assert await get_ids(phone__isnull=True) == [3]
    assert (await BlindIndexedCustomer.objects.get(email="cat@example.com")).id == 3
    for email, expected_ids in (("ann@example.com", [1]), ("bob@example.com", [2]), ("x@example.com", [])):
        assert await get_ids(email=email) == expected_ids


@pytest.mark.asyncio
async def test_every_write_path_keeps_the_index(db_blind_index):
    await create_customers()
    customer = await BlindIndexedCustomer.objects.get(id=1)
    customer.email = "ann@new.example.com"
    await customer.save(update_fields=["email"])
    assert await get_ids(email="ann@new.example.com") == [1]
    customer.phone = "+111"
    await customer.save(changed_only=True)
    assert await get_ids(phone="+111") == [1]
    await BlindIndexedCustomer.objects.filter(id=2).update(email="bob@new.example.com")
    assert await get_ids(email="bob@new.example.com") == [2]
    await BlindIndexedCustomer.objects.filter(id=3).update(backup_email=F("email"))
    assert await get_ids(backup_email="cat@example.com") == [3]
    customers = await BlindIndexedCustomer.objects.filter(id__in=[1, 2]).order_by("id")
    customers[0].email, customers[1].email = "first@example.com", "second@example.com"
    await BlindIndexedCustomer.objects.bulk_update(customers, fields=["email"])
    assert await get_ids(email__in=["first@example.com", "second@example.com"]) == [1, 2]
    await BlindIndexedCustomer.objects.bulk_create(
        [BlindIndexedCustomer(id=4, email="dan@example.com"), BlindIndexedCustomer(id=5, email="eve@example.com")]
    )
    assert await get_ids(email="eve@example.com") == [5]
    assert customers[0].email_blind_index == FieldEncryption.get_blind_index("first@example.com")


@pytest.mark.asyncio
@requires_features(supports_unique_constraints=True, supports_transactions=True)
async def test_uniqueness_and_upsert_through_the_index(db_blind_index):
    await create_customers()
    with pytest.raises(IntegrityError):
        async with Transactions.atomic("models"):
            await BlindIndexedCustomer.objects.create(id=9, email="ann@example.com")
    await BlindIndexedCustomer.objects.bulk_create(
        [BlindIndexedCustomer(id=10, email="ann@example.com", name="Ann again")],
        on_conflict=["email"],
        update_fields=["name"],
    )
    assert await BlindIndexedCustomer.objects.filter(email="ann@example.com").values_list("id", "name") == [
        (1, "Ann again")
    ]


@pytest.mark.asyncio
async def test_reencrypt_fields_rewrites_the_index_with_a_new_key(db_blind_index):
    await create_customers()
    FieldEncryption.configure("another-secret", previous_keys=[SECRET])
    assert await get_ids(email="ann@example.com") == []
    assert await FieldEncryption.reencrypt(BlindIndexedCustomer) == 3
    assert await get_ids(email="ann@example.com") == [1]
    FieldEncryption.configure("third-secret", previous_keys=["another-secret"], blind_index_key="stable")
    await FieldEncryption.reencrypt(BlindIndexedCustomer)
    FieldEncryption.configure("fourth-secret", previous_keys=["third-secret"], blind_index_key="stable")
    assert await get_ids(email="ann@example.com") == [1]


@pytest.mark.asyncio
async def test_what_a_blind_index_refuses(db_blind_index):
    await create_customers()
    with pytest.raises(FieldError, match="encrypted"):
        await get_ids(email__startswith="ann")
    with pytest.raises(FieldError, match="encrypted"):
        await get_ids(email=F("backup_email"))
    with pytest.raises(FieldError, match="is a blind index"):
        await BlindIndexedCustomer.objects.filter(id=1).update(email_blind_index="x")
    with pytest.raises(FieldError, match="F\\(\\) only of a field with one"):
        await BlindIndexedCustomer.objects.filter(id=1).update(email=F("note"))
    with pytest.raises(FieldError, match="encrypted"):
        await get_ids(note="anything")


def test_wrong_declarations_are_refused():
    with pytest.raises(ConfigurationError, match="blind_index must be a bool"):
        EncryptedTextField(blind_index="yes")
    with pytest.raises(ConfigurationError, match="can't be indexed"):
        EncryptedTextField(unique=True)
    with pytest.raises(ConfigurationError, match="non-empty string blind_index_key"):
        FieldEncryption.configure(SECRET, blind_index_key="")
    with pytest.raises(ConfigurationError, match="is the name of another attribute"):

        class TakenBlindIndexName(Model):
            id = fields.IntField(primary_key=True)
            email = EncryptedTextField(blind_index=True)
            email_blind_index = fields.CharField(max_length=10)

            class Meta:
                app = "models"
                abstract = True


def test_the_fields_deconstruct_for_a_migration():
    meta = BlindIndexedCustomer._meta
    assert meta.fields_map["email"].deconstruct()[2] == {"blind_index": True, "unique": True}
    path, _args, kwargs = meta.fields_map["phone_blind_index"].deconstruct()
    assert path.endswith("BlindIndexField")
    assert kwargs["blind_index_source_field_name"] == "phone"
    assert kwargs["source_field"] == "phone_number_blind_index"
