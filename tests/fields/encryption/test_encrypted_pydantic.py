"""An EncryptedJSONField declaring a pydantic type: values are validated into the type, read back as it, and
stored encrypted."""

import json

import pytest

from hare.core.connections.connections import Connections
from hare.exceptions import ValidationError
from hare.fields.encrypted.field_encryption import FieldEncryption
from tests.fields.models_encrypted_pydantic import EncryptedSettingsRecord, EncryptedWebhookSettings

REQUIRES = ("encryption", "pydantic")

SECRET_KEY = "test-field-encryption-secret"


@pytest.fixture(autouse=True)
def field_encryption_key():
    FieldEncryption.configure(SECRET_KEY)
    yield
    FieldEncryption.reset()


@pytest.mark.asyncio
async def test_json_field_type_round_trip(db_encrypted_fields):
    from_dict = await EncryptedSettingsRecord.objects.create(settings={"url": "https://secret.example"})
    from_model = await EncryptedSettingsRecord.objects.create(
        settings=EncryptedWebhookSettings(url="https://other.example")
    )
    assert from_dict.settings == EncryptedWebhookSettings(url="https://secret.example")
    fetched = await EncryptedSettingsRecord.objects.get(id=from_model.id)
    assert fetched.settings == EncryptedWebhookSettings(url="https://other.example", retries=3)
    await EncryptedSettingsRecord.objects.filter(id=from_dict.id).update(
        settings={"url": "https://updated.example", "retries": 5}
    )
    assert await EncryptedSettingsRecord.objects.filter(id=from_dict.id).values_list("settings", flat=True) == [
        EncryptedWebhookSettings(url="https://updated.example", retries=5)
    ]
    rows = await Connections.get("models").execute_dicts("SELECT settings FROM encrypted_settings_record")
    for row in rows:
        raw_settings = row["settings"] if isinstance(row["settings"], dict) else json.loads(row["settings"])
        assert "example" not in raw_settings["url"]
    with pytest.raises(ValidationError):
        await EncryptedSettingsRecord.objects.create(settings={"retries": 1})
