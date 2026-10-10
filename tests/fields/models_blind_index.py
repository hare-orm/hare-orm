from __future__ import annotations

from hare import Model, fields
from hare.fields.encrypted import EncryptedTextField


class BlindIndexedCustomer(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, default="")
    email = EncryptedTextField(blind_index=True, unique=True)
    phone = EncryptedTextField(blind_index=True, null=True, source_field="phone_number")
    backup_email = EncryptedTextField(blind_index=True, null=True)
    note = EncryptedTextField(null=True)

    class Meta:
        table = "blind_indexed_customer"
        track_dirty_fields = True
