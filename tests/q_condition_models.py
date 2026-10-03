from decimal import Decimal

from hare import fields
from hare.ddl.constraints import CheckConstraint, UniqueConstraint
from hare.ddl.indexes import PartialIndex
from hare.models import Model
from hare.query.expressions import Q


class Holder(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=20)


class Batch(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=20, db_index=True)
    qty = fields.IntField()
    price = fields.DecimalField(max_digits=10, decimal_places=2, null=True)
    holder = fields.ForeignKeyField("models.Holder", related_name="batches", null=True)

    class Meta:
        table = "q_batch"
        indexes = [PartialIndex(fields=["name"], condition=Q(price__gt=Decimal("1")) | Q(qty__lt=3))]
        constraints = [
            UniqueConstraint(fields=("name", "holder")),
            CheckConstraint(check=Q(qty__gte=0) & ~Q(name=""), name="q_batch_valid"),
            UniqueConstraint(fields=("qty",), condition=Q(price__isnull=False) & Q(qty__gt=100), name="q_batch_big"),
        ]
