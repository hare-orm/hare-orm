import re

from pydantic import BaseModel

from hare import fields
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.fields.validators import MinLengthValidator, RegexValidator
from hare.models import Model


class EncryptedOwner(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=64)

    class Meta:
        table = "encrypted_owner"


class EncryptedRecord(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=64, default="")
    secret = fields.EncryptedTextField(null=True)
    config = fields.EncryptedJSONField(null=True)
    api_token = fields.CharField(max_length=128, null=True, sensitive=True)
    public_note = fields.EncryptedTextField(null=True, sensitive=False)
    owner: fields.ForeignKeyNullableRelation[EncryptedOwner] = fields.ForeignKeyField(
        "models.EncryptedOwner", null=True, related_name="records", sensitive=True
    )

    class Meta:
        table = "encrypted_record"


class EncryptedRecordAttachment(Model):
    id = fields.IntField(primary_key=True)
    record: fields.ForeignKeyRelation[EncryptedRecord] = fields.ForeignKeyField(
        "models.EncryptedRecord", related_name=False
    )

    class Meta:
        table = "encrypted_record_attachment"


class SensitiveGeneratedRecord(Model):
    id = fields.IntField(primary_key=True)
    api_token = fields.CharField(max_length=128, sensitive=True)
    api_token_upper = fields.GeneratedField(
        expression=RawSQLTerm("UPPER(api_token)"), output_field=fields.CharField(max_length=128, sensitive=True)
    )
    api_token_length = fields.GeneratedField(
        expression=RawSQLTerm("LENGTH(api_token)"), output_field=fields.IntField(sensitive=True), sensitive=False
    )

    class Meta:
        table = "sensitive_generated_record"


class EncryptedWebhookSettings(BaseModel):
    url: str
    retries: int = 3


class EncryptedTypedRecord(Model):
    id = fields.IntField(primary_key=True)
    settings = fields.EncryptedJSONField(field_type=EncryptedWebhookSettings, null=True)
    api_key = fields.EncryptedTextField(null=True, validators=[MinLengthValidator(32)])
    pin = fields.EncryptedTextField(null=True, validators=[RegexValidator(r"^\d{4}$", re.IGNORECASE)])

    class Meta:
        table = "encrypted_typed_record"


class EncryptedDocumentRecord(Model):
    id = fields.IntField(primary_key=True)
    plain_keys = fields.EncryptedJSONField(null=True)
    encrypted_keys = fields.EncryptedJSONField(null=True, encrypt_keys=True)
    settings_list = fields.EncryptedJSONField(null=True, field_type=list[EncryptedWebhookSettings], encrypt_keys=True)

    class Meta:
        table = "encrypted_document_record"
