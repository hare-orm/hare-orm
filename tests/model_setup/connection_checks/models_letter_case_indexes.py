from hare import Model, fields
from hare.ddl.constraints import UniqueConstraint
from hare.ddl.indexes import Index


class CaseIndexA(Model):
    id = fields.IntField(primary_key=True)
    code = fields.IntField()

    class Meta:
        constraints = [UniqueConstraint(fields=("code",), name="Uq_N6")]


class CaseIndexB(Model):
    id = fields.IntField(primary_key=True)
    code = fields.IntField()

    class Meta:
        indexes = [Index(fields=("code",), name="uq_n6")]
