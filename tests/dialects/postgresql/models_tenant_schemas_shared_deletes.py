from __future__ import annotations

from hare import Model, fields


class SharedClient(Model):
    id = fields.IntField(primary_key=True, generated=False)
    name = fields.CharField(max_length=20)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "shared_client"
        soft_delete_field = "deleted_at"


class TenantBill(Model):
    id = fields.IntField(primary_key=True, generated=False)
    client = fields.ForeignKeyField("models.SharedClient", related_name="bills", null=True)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        table = "tenant_bill"
        tenant_schema = True
        soft_delete_field = "deleted_at"


class TenantGuard(Model):
    id = fields.IntField(primary_key=True, generated=False)
    client = fields.ForeignKeyField("models.SharedClient", related_name="guards", null=True, on_delete=fields.PROTECT)

    class Meta:
        table = "tenant_guard"
        tenant_schema = True


class SharedAccount(Model):
    id = fields.IntField(primary_key=True, generated=False)

    class Meta:
        table = "shared_account"


class SharedLedger(Model):
    id = fields.IntField(primary_key=True, generated=False)
    account = fields.ForeignKeyField("models.SharedAccount", related_name="ledgers")

    class Meta:
        table = "shared_ledger"


class TenantEntry(Model):
    id = fields.IntField(primary_key=True, generated=False)
    ledger = fields.ForeignKeyField("models.SharedLedger", related_name="entries")
    account = fields.ForeignKeyField("models.SharedAccount", related_name="guarded_entries", on_delete=fields.PROTECT)

    class Meta:
        table = "tenant_entry"
        tenant_schema = True


class TenantNoteBook(Model):
    id = fields.IntField(primary_key=True, generated=False)
    accounts = fields.ManyToManyField("models.SharedAccount", related_name="note_books")

    class Meta:
        table = "tenant_note_book"
        tenant_schema = True
