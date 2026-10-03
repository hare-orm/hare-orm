"""Changing the field-encryption key without losing data: values are read with the new or a previous
key and written with the new one, reencrypt_fields() writes every stored value with the new key,
after which the previous key can go."""

from __future__ import annotations

import pytest

from hare.core.connections import Connections
from hare.exceptions import (
    ConfigurationError,
    DecryptionError,
    QueryError,
)
from hare.fields.encryption import FieldEncryption, configure_field_encryption, reencrypt_fields
from tests.fields.models_encrypted import EncryptedOwner, EncryptedRecord

OLD_SECRET = "old-field-encryption-secret"
NEW_SECRET = "new-field-encryption-secret"


@pytest.fixture(autouse=True)
def forget_the_keys():
    yield
    FieldEncryption.reset()


async def fetch_raw_secrets() -> list[str]:
    rows = await Connections.get("models").execute_dicts('SELECT "secret" FROM "encrypted_record" ORDER BY "id"')
    return [row["secret"] for row in rows]


@pytest.mark.asyncio
async def test_the_key_changes_without_losing_data(db_encrypted_fields):
    configure_field_encryption(OLD_SECRET)
    for record_id in range(1, 4):
        await EncryptedRecord.objects.create(
            id=record_id, secret=f"secret {record_id}", config={"token": f"token {record_id}"}
        )
    old_tokens = await fetch_raw_secrets()

    configure_field_encryption(NEW_SECRET, previous_keys=[OLD_SECRET])
    assert (await EncryptedRecord.objects.get(id=1)).secret == "secret 1"
    await EncryptedRecord.objects.create(id=4, secret="secret 4")

    assert await reencrypt_fields(EncryptedRecord, EncryptedOwner, batch_size=2) == 4
    new_tokens = await fetch_raw_secrets()
    assert all(new_token != old_token for new_token, old_token in zip(new_tokens, old_tokens, strict=False))

    configure_field_encryption(NEW_SECRET)
    records = list(await EncryptedRecord.objects.all().order_by("id"))
    assert [record.secret for record in records] == [f"secret {record_id}" for record_id in range(1, 5)]
    assert [record.config for record in records[:3]] == [{"token": f"token {record_id}"} for record_id in range(1, 4)]

    configure_field_encryption(OLD_SECRET)
    with pytest.raises(DecryptionError, match="different key"):
        await EncryptedRecord.objects.get(id=1)


def test_wrong_keys_and_batch_sizes_are_refused():
    with pytest.raises(ConfigurationError, match="sequence of secrets"):
        configure_field_encryption(NEW_SECRET, previous_keys=OLD_SECRET)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="non-empty string secrets"):
        configure_field_encryption(NEW_SECRET, previous_keys=[""])


@pytest.mark.asyncio
async def test_reencrypt_needs_a_positive_batch_size(db_encrypted_fields):
    configure_field_encryption(NEW_SECRET)
    with pytest.raises(QueryError, match="positive int batch_size"):
        await reencrypt_fields(EncryptedRecord, batch_size=0)
