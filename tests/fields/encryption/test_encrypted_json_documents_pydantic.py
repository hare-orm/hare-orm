"""An EncryptedJSONField declaring a pydantic type stores its documents as one without it does: any root
restored into the type, the keys encrypted with ``encrypt_keys=True``, ciphertext copied from a field storing
it the same way."""

import pytest

from hare.query.expressions import F
from tests.fields.encryption.test_encrypted_json_documents import (
    decrypt_stored_document,
    fetch_raw_document,
)
from tests.fields.models_encrypted_pydantic import EncryptedTypedDocumentRecord, EncryptedWebhookSettings

#: The table of EncryptedTypedDocumentRecord.
TYPED_DOCUMENT_TABLE = "encrypted_typed_document_record"


pytestmark = pytest.mark.usefixtures("field_encryption_key")


@pytest.mark.asyncio
async def test_a_declared_type_of_any_root_is_restored(db_encrypted_fields):
    settings = [EncryptedWebhookSettings(url="https://a.example", retries=1), {"url": "https://b.example"}]
    record = await EncryptedTypedDocumentRecord.objects.create(id=1, settings_list=settings)
    fetched = await EncryptedTypedDocumentRecord.objects.get(id=record.id)
    assert fetched.settings_list == [
        EncryptedWebhookSettings(url="https://a.example", retries=1),
        EncryptedWebhookSettings(url="https://b.example", retries=3),
    ]
    stored = await fetch_raw_document(record.id, "settings_list", TYPED_DOCUMENT_TABLE)
    assert all(set(item) and all(key.startswith("gAAAAA") for key in item) for item in stored)


@pytest.mark.asyncio
async def test_ciphertext_is_copied_into_a_declared_type_field_storing_it_the_same_way(db_encrypted_fields):
    await EncryptedTypedDocumentRecord.objects.create(id=1, encrypted_keys={"b": "c"})
    await EncryptedTypedDocumentRecord.objects.filter(id=1).update(settings_list=F("encrypted_keys"))
    stored = await fetch_raw_document(1, "settings_list", TYPED_DOCUMENT_TABLE)
    assert decrypt_stored_document(stored, decrypts_keys=True) == {"b": "c"}
