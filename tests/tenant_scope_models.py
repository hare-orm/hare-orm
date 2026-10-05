"""Models for tests/test_tenant_scope_values.py - models split by different tenant fields with
different value sets: a text field, an integer field, a foreign key's column, a composite primary
key, an abstract base."""

from hare import fields
from hare.models import Model


class ScopeShop(Model):
    """Not tenant-scoped - the target of ScopeShopOrder's tenant relation."""

    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20)


class ScopeCustomer(Model):
    id = fields.IntField(primary_key=True)
    region = fields.CharField(max_length=10)
    name = fields.CharField(max_length=20)
    orders: fields.ReverseRelation["ScopeOrder"]

    class Meta:
        tenant_field = "region"


class ScopeTag(Model):
    id = fields.IntField(primary_key=True)
    store = fields.CharField(max_length=10)
    name = fields.CharField(max_length=20)
    orders: fields.ManyToManyRelation["ScopeOrder"]

    class Meta:
        tenant_field = "store"


class ScopeLabel(Model):
    id = fields.IntField(primary_key=True)
    group = fields.CharField(max_length=10)
    name = fields.CharField(max_length=20)

    class Meta:
        tenant_field = "group"


class ScopeOrder(Model):
    id = fields.IntField(primary_key=True)
    store = fields.CharField(max_length=10)
    code = fields.CharField(max_length=20, unique=True, null=True)
    number = fields.IntField(default=0)
    customer: fields.ForeignKeyNullableRelation[ScopeCustomer] = fields.ForeignKeyField(
        "models.ScopeCustomer", related_name="orders", null=True, on_delete=fields.SET_NULL
    )
    deleted_at = fields.DatetimeField(null=True)
    lines: fields.ReverseRelation["ScopeOrderLine"]
    tags: fields.ManyToManyRelation[ScopeTag] = fields.ManyToManyField("models.ScopeTag", related_name="orders")
    labels: fields.ManyToManyRelation[ScopeLabel] = fields.ManyToManyField(
        "models.ScopeLabel", related_name="orders", through="models.ScopeOrderLabel"
    )

    class Meta:
        tenant_field = "store"
        soft_delete_field = "deleted_at"


class ScopeOrderLabel(Model):
    """A through model with a tenant field of its own."""

    id = fields.IntField(primary_key=True)
    store = fields.CharField(max_length=10)
    scopeorder: fields.ForeignKeyRelation[ScopeOrder] = fields.ForeignKeyField(
        "models.ScopeOrder", related_name="order_labels", on_delete=fields.CASCADE
    )
    scopelabel: fields.ForeignKeyRelation[ScopeLabel] = fields.ForeignKeyField(
        "models.ScopeLabel", related_name="order_labels", on_delete=fields.CASCADE
    )

    class Meta:
        tenant_field = "store"


class ScopeOrderLine(Model):
    id = fields.IntField(primary_key=True)
    store = fields.CharField(max_length=10)
    order: fields.ForeignKeyRelation[ScopeOrder] = fields.ForeignKeyField(
        "models.ScopeOrder", related_name="lines", on_delete=fields.CASCADE
    )
    quantity = fields.IntField(default=1)
    deleted_at = fields.DatetimeField(null=True)

    class Meta:
        tenant_field = "store"
        soft_delete_field = "deleted_at"


class ScopeSalesSummary(Model):
    id = fields.IntField(primary_key=True)
    store_id = fields.IntField()
    total = fields.IntField(default=0)

    class Meta:
        tenant_field = "store_id"


class ScopeFaq(Model):
    id = fields.IntField(primary_key=True)
    group = fields.CharField(max_length=10)
    question = fields.CharField(max_length=50)

    class Meta:
        tenant_field = "group"


class ScopeShopOrder(Model):
    """Meta.tenant_field is the column of a foreign key."""

    id = fields.IntField(primary_key=True)
    shop: fields.ForeignKeyRelation[ScopeShop] = fields.ForeignKeyField("models.ScopeShop", related_name="orders")
    title = fields.CharField(max_length=20)

    class Meta:
        tenant_field = "shop_id"


class ScopePair(Model):
    """A composite primary key holding the tenant field."""

    store = fields.CharField(max_length=10)
    number = fields.IntField()
    title = fields.CharField(max_length=20, default="")
    pk = fields.CompositePrimaryKey("store", "number")
    notes: fields.ReverseRelation["ScopePairNote"]

    class Meta:
        tenant_field = "store"


class ScopePairNote(Model):
    id = fields.IntField(primary_key=True)
    group = fields.CharField(max_length=10)
    pair: fields.ForeignKeyRelation[ScopePair] = fields.ForeignKeyField(
        "models.ScopePair", related_name="notes", on_delete=fields.CASCADE
    )
    text = fields.CharField(max_length=20, default="")

    class Meta:
        tenant_field = "group"


class ScopeDocument(Model):
    store = fields.CharField(max_length=10)
    title = fields.CharField(max_length=20)

    class Meta:
        abstract = True
        tenant_field = "store"


class ScopeInvoice(ScopeDocument):
    pass


class ScopeReceipt(ScopeDocument):
    pass
