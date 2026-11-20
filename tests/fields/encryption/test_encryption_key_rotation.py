"""Changing the field-encryption key without losing data: values are read with the new or a previous
key and written with the new one, FieldEncryption.reencrypt() writes every stored value with the new key,
after which the previous key can go."""

from __future__ import annotations

import pytest

from hare.contrib.test import requires_features
from hare.core.connections.connections import Connections
from hare.exceptions import (
    ConfigurationError,
    DecryptionError,
    QueryError,
)
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.models.tenancy.tenancy import Tenancy
from hare.query.statements.write.bulk.bulk_update_query import BulkUpdateQuery
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


@pytest.mark.requires("encryption")
@pytest.mark.asyncio
async def test_the_key_changes_without_losing_data(db_encrypted_fields):
    FieldEncryption.configure(OLD_SECRET)
    for record_id in range(1, 4):
        await EncryptedRecord.objects.create(
            id=record_id, secret=f"secret {record_id}", config={"token": f"token {record_id}"}
        )
    old_tokens = await fetch_raw_secrets()

    FieldEncryption.configure(NEW_SECRET, previous_keys=[OLD_SECRET])
    assert (await EncryptedRecord.objects.get(id=1)).secret == "secret 1"
    await EncryptedRecord.objects.create(id=4, secret="secret 4")

    assert await FieldEncryption.reencrypt(EncryptedRecord, EncryptedOwner, batch_size=2) == 4
    new_tokens = await fetch_raw_secrets()
    assert all(new_token != old_token for new_token, old_token in zip(new_tokens, old_tokens, strict=False))

    FieldEncryption.configure(NEW_SECRET)
    records = list(await EncryptedRecord.objects.all().order_by("id"))
    assert [record.secret for record in records] == [f"secret {record_id}" for record_id in range(1, 5)]
    assert [record.config for record in records[:3]] == [{"token": f"token {record_id}"} for record_id in range(1, 4)]

    FieldEncryption.configure(OLD_SECRET)
    with pytest.raises(DecryptionError, match="different key"):
        await EncryptedRecord.objects.get(id=1)


@requires_features(supports_transactions=True)
@pytest.mark.requires("encryption")
@pytest.mark.asyncio
async def test_rotation_keeps_completed_batches_and_can_continue_after_failure(db_encrypted_fields, monkeypatch):
    FieldEncryption.configure(OLD_SECRET)
    for record_id in (1, 2, 3):
        await EncryptedRecord.objects.create(id=record_id, secret=f"secret {record_id}")
    before = await fetch_raw_secrets()
    FieldEncryption.configure(NEW_SECRET, previous_keys=[OLD_SECRET])
    write_batch = BulkUpdateQuery._execute_bound
    written = 0

    async def fail_after_writing_the_second_batch(query):
        nonlocal written
        result = await write_batch(query)
        written += 1
        if written == 2:
            raise RuntimeError("second rotation batch failed")
        return result

    with Tenancy.scope(7):
        active_scope = Tenancy.current.get()
        with monkeypatch.context() as patch:
            patch.setattr(BulkUpdateQuery, "_execute_bound", fail_after_writing_the_second_batch)
            with pytest.raises(RuntimeError, match="second rotation batch failed"):
                await FieldEncryption.reencrypt(EncryptedRecord, batch_size=1)
        assert Tenancy.current.get() is active_scope
        after = await fetch_raw_secrets()
        assert after[0] != before[0]
        assert after[1:] == before[1:]
        assert await FieldEncryption.reencrypt(EncryptedRecord, batch_size=1) == 3
    FieldEncryption.configure(NEW_SECRET)
    assert await EncryptedRecord.objects.order_by("id").values_list("secret", flat=True) == [
        "secret 1",
        "secret 2",
        "secret 3",
    ]


@pytest.mark.database_independent
def test_wrong_keys_and_batch_sizes_are_refused():
    with pytest.raises(ConfigurationError, match="sequence of secrets"):
        FieldEncryption.configure(NEW_SECRET, previous_keys=OLD_SECRET)  # type: ignore[arg-type]
    with pytest.raises(ConfigurationError, match="non-empty string secrets"):
        FieldEncryption.configure(NEW_SECRET, previous_keys=[""])


@pytest.mark.requires("encryption")
@pytest.mark.asyncio
async def test_reencrypt_needs_a_positive_batch_size(db_encrypted_fields):
    FieldEncryption.configure(NEW_SECRET)
    with pytest.raises(QueryError, match="positive int batch_size"):
        await FieldEncryption.reencrypt(EncryptedRecord, batch_size=0)
