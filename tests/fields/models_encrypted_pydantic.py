from pydantic import BaseModel

from hare import fields
from hare.models import Model


class EncryptedWebhookSettings(BaseModel):
    url: str
    retries: int = 3


class EncryptedSettingsRecord(Model):
    id = fields.IntField(primary_key=True)
    settings = fields.EncryptedJSONField(field_type=EncryptedWebhookSettings, null=True)

    class Meta:
        table = "encrypted_settings_record"


class EncryptedTypedDocumentRecord(Model):
    id = fields.IntField(primary_key=True)
    encrypted_keys = fields.EncryptedJSONField(null=True, encrypt_keys=True)
    settings_list = fields.EncryptedJSONField(null=True, field_type=list[EncryptedWebhookSettings], encrypt_keys=True)

    class Meta:
        table = "encrypted_typed_document_record"
